"""并发测速模块。

使用 asyncio 实现并发，并且严格限制并发数量：
启动固定数量的工作协程（worker），它们从队列里领取任务。
这样即使有 1000 个 IP，同一时间最多也只有“并发数”个连接在进行。

支持：
1. 开始测速；
2. 统计已测试 / 成功 / 失败数量（通过回调上报进度）；
3. 用户中途停止：不再领取新任务，等待正在执行的任务安全结束后退出。

V1.2 三级测试流水线（每一级都只在上一级成功后才执行）：
    TCP 连接（tcp_ping）
      ↓ TCP 成功 且 开启 HTTP 测试
    HTTP/HTTPS 连通性（http_test）
      ↓ HTTP 成功 且 开启下载测速
    下载速度（download_test）

这样做的好处：
    TCP 不通的 IP 不会浪费 HTTP/下载的超时时间，整体速度快很多。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

from core.http_tester import (
    DEFAULT_HTTP_HOST,
    DownloadResult,
    HttpResult,
    download_test,
    http_test,
)
from core.ip_loader import DEFAULT_PORT, IPEntry
from core.tcp_tester import TestResult, attach_stage_results, tcp_ping
from utils.logger import get_logger

logger: logging.Logger = get_logger()

# 默认参数
DEFAULT_CONCURRENCY = 50
DEFAULT_TIMEOUT_MS = 1000
DEFAULT_HTTP_TIMEOUT_MS = 3000          # HTTP 阶段默认超时（毫秒）
DEFAULT_DOWNLOAD_BYTES = 1024 * 1024    # 下载阶段默认下载量：1 MB
DEFAULT_DOWNLOAD_TIMEOUT_MS = 10000     # 下载阶段默认超时（毫秒）

# 并发数量与超时的安全范围（防止用户输入极端数值）
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 500
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 60000
MIN_HTTP_TIMEOUT_MS = 500
MAX_HTTP_TIMEOUT_MS = 30000
MIN_DOWNLOAD_BYTES = 64 * 1024               # 最小 64 KB
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024        # 最大 20 MB
MIN_DOWNLOAD_TIMEOUT_MS = 1000
MAX_DOWNLOAD_TIMEOUT_MS = 120000

# 回调类型：每完成一个 IP 调用一次 / 进度变化时调用一次
ResultCallback = Callable[[TestResult], None]
ProgressCallback = Callable[[int, int, int, int], None]  # 已测试, 成功, 失败, 总数
StageCallback = Callable[["StageStats"], None]           # 各阶段成功/失败统计

# 阶段统计回调的最小发送间隔（秒）：避免上千次刷新拖慢界面
STAGE_MIN_INTERVAL_SEC = 0.1


@dataclass
class ScanSummary:
    """一轮测速的汇总信息（按阶段分别统计）。"""

    total: int = 0        # 计划测试的总数
    tested: int = 0       # 实际完成的数量
    success: int = 0      # TCP 成功数量（与第一阶段语义一致）
    failed: int = 0       # TCP 失败数量
    stopped: bool = False # 是否被用户中途停止

    # ---- V1.2 新增：HTTP 与下载两个阶段的统计 ----
    http_tested: int = 0      # 实际执行了 HTTP 测试的 IP 数量
    http_success: int = 0     # HTTP 成功数量
    http_failed: int = 0      # HTTP 失败数量
    download_tested: int = 0  # 实际执行了下载测速的 IP 数量
    download_success: int = 0 # 下载成功数量
    download_failed: int = 0  # 下载失败数量

    @property
    def finished(self) -> bool:
        """是否全部测完（没有被中途停止）。"""
        return not self.stopped and self.tested >= self.total


@dataclass(frozen=True)
class StageStats:
    """各阶段统计快照（不可变，方便通过 Qt 信号安全地传给界面线程）。"""

    total: int = 0
    tcp_tested: int = 0
    tcp_success: int = 0
    tcp_failed: int = 0
    http_tested: int = 0
    http_success: int = 0
    http_failed: int = 0
    download_tested: int = 0
    download_success: int = 0
    download_failed: int = 0

    @classmethod
    def from_summary(cls, summary: ScanSummary) -> "StageStats":
        """根据运行中的汇总信息生成一份快照。"""
        return cls(
            total=summary.total,
            tcp_tested=summary.tested,
            tcp_success=summary.success,
            tcp_failed=summary.failed,
            http_tested=summary.http_tested,
            http_success=summary.http_success,
            http_failed=summary.http_failed,
            download_tested=summary.download_tested,
            download_success=summary.download_success,
            download_failed=summary.download_failed,
        )


def sort_results(results: Iterable[TestResult]) -> List[TestResult]:
    """结果排序：成功的按延迟从低到高排在前面，失败的排在后面。"""
    success_results = [item for item in results if item.success]
    failed_results = [item for item in results if not item.success]
    success_results.sort(key=lambda item: item.latency if item.latency is not None else 0)
    return success_results + failed_results


class Scanner:
    """并发测速控制器。"""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        http_enabled: bool = True,
        http_timeout_ms: int = DEFAULT_HTTP_TIMEOUT_MS,
        download_enabled: bool = True,
        download_bytes: int = DEFAULT_DOWNLOAD_BYTES,
        download_timeout_ms: int = DEFAULT_DOWNLOAD_TIMEOUT_MS,
        http_host: str = DEFAULT_HTTP_HOST,
    ) -> None:
        # 第一级：TCP
        self.port = port
        self.concurrency = max(MIN_CONCURRENCY, min(int(concurrency), MAX_CONCURRENCY))
        self.timeout_ms = max(MIN_TIMEOUT_MS, min(int(timeout_ms), MAX_TIMEOUT_MS))

        # 第二级：HTTP（默认开启，与第一阶段相比只是“多做一步”，不会影响 TCP 结果）
        self.http_enabled = bool(http_enabled)
        self.http_timeout_ms = max(
            MIN_HTTP_TIMEOUT_MS, min(int(http_timeout_ms), MAX_HTTP_TIMEOUT_MS)
        )

        # 第三级：下载测速
        self.download_enabled = bool(download_enabled)
        self.download_bytes = max(
            MIN_DOWNLOAD_BYTES, min(int(download_bytes), MAX_DOWNLOAD_BYTES)
        )
        self.download_timeout_ms = max(
            MIN_DOWNLOAD_TIMEOUT_MS, min(int(download_timeout_ms), MAX_DOWNLOAD_TIMEOUT_MS)
        )

        # TLS 的 SNI / HTTP Host 使用的域名
        self.http_host = http_host or DEFAULT_HTTP_HOST

        self._stop_requested = False                          # 是否收到停止请求
        self._stop_event: Optional[asyncio.Event] = None      # asyncio 的停止信号
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # 当前运行的循环，用于跨线程通知
        self._last_stage_time = 0.0                           # 上次上报阶段统计的时间（用于限流）

    # ------------------------------------------------------------------
    # 停止相关
    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        """请求停止测速。

        这个方法是线程安全的，可以从 GUI 主线程调用：
        通过 call_soon_threadsafe 把“设置停止信号”这件事交给测速线程的事件循环执行。
        """
        self._stop_requested = True
        event = self._stop_event
        loop = self._loop

        if event is None:
            return
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(event.set)
                return
            except RuntimeError:
                pass  # 事件循环刚好关闭，忽略即可
        try:
            event.set()
        except Exception:
            pass

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def _is_stopped(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def run(
        self,
        entries: Sequence[IPEntry],
        on_result: Optional[ResultCallback] = None,
        on_progress: Optional[ProgressCallback] = None,
        on_stage: Optional[StageCallback] = None,
    ) -> ScanSummary:
        """开始并发测速，返回汇总信息。

        参数：
            entries：待测速的 IP 列表
            on_result：每测完一个 IP 调用一次（在测速线程中执行）
            on_progress：进度变化时调用（在测速线程中执行）
            on_stage：各阶段（TCP / HTTP / 下载）统计变化时调用（在测速线程中执行）
        """
        entry_list = list(entries)
        summary = ScanSummary(total=len(entry_list))

        if not entry_list:
            logger.warning("测速任务为空，直接返回")
            summary.stopped = True
            return summary

        # 准备任务队列：所有任务先入队，由固定数量的 worker 依次领取
        queue: asyncio.Queue[IPEntry] = asyncio.Queue()
        for entry in entry_list:
            queue.put_nowait(entry)

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        if self._stop_requested:
            self._stop_event.set()

        worker_count = min(self.concurrency, len(entry_list))
        logger.info(
            "开始测速：共 %s 个目标，并发 %s，端口 %s，超时 %sms，"
            "HTTP测试=%s(%sms)，下载测速=%s(%s bytes, %sms)",
            summary.total,
            worker_count,
            self.port,
            self.timeout_ms,
            "开启" if self.http_enabled else "关闭",
            self.http_timeout_ms,
            "开启" if self.download_enabled else "关闭",
            self.download_bytes,
            self.download_timeout_ms,
        )

        workers = [
            asyncio.create_task(self._worker(queue, summary, on_result, on_progress, on_stage))
            for _ in range(worker_count)
        ]

        # return_exceptions=True：即使某个 worker 出错也不会影响其他 worker
        await asyncio.gather(*workers, return_exceptions=True)

        summary.stopped = self._is_stopped() or self._stop_requested

        # 结束时补发一次阶段统计，保证界面显示的数字是最终值
        self._notify_stage(summary, on_stage, force=True)

        logger.info(
            "测速结束：完成 %s/%s，TCP 成功 %s / 失败 %s，"
            "HTTP 成功 %s / 失败 %s，下载成功 %s / 失败 %s，用户停止=%s",
            summary.tested,
            summary.total,
            summary.success,
            summary.failed,
            summary.http_success,
            summary.http_failed,
            summary.download_success,
            summary.download_failed,
            summary.stopped,
        )
        return summary

    async def _worker(
        self,
        queue: "asyncio.Queue[IPEntry]",
        summary: ScanSummary,
        on_result: Optional[ResultCallback],
        on_progress: Optional[ProgressCallback],
        on_stage: Optional[StageCallback],
    ) -> None:
        """工作协程：不停地从队列领取任务，直到队列为空或收到停止请求。

        V1.2：一个 IP 依次经过 TCP → HTTP → 下载 三级测试。
        只有上一级成功才会进入下一级（TCP 不通的 IP 直接出结果，不浪费时间）。
        """
        while True:
            if self._is_stopped():
                return  # 收到停止请求：不再领取新任务

            try:
                entry = queue.get_nowait()
            except asyncio.QueueEmpty:
                return  # 队列已空，任务全部领取完毕

            # 用户没有指定端口时，使用界面里设置的端口
            port = entry.port if entry.port is not None else self.port
            result = await self._test_one_entry(entry.ip, port, summary)

            # 更新第一级（TCP）统计
            summary.tested += 1
            if result.success:
                summary.success += 1
            else:
                summary.failed += 1

            self._safe_call(on_result, result)
            self._safe_call(
                on_progress,
                summary.tested,
                summary.success,
                summary.failed,
                summary.total,
            )
            self._notify_stage(summary, on_stage)

    async def _test_one_entry(self, ip: str, port: int, summary: ScanSummary) -> TestResult:
        """对一个 IP 执行三级测试，并更新 HTTP / 下载阶段的统计。

        返回合并后的完整结果（TestResult）。
        """
        # ---- 第一级：TCP 连接测试 ----
        tcp_result = await tcp_ping(ip, port, timeout_ms=self.timeout_ms)
        if not tcp_result.success:
            # TCP 不通：HTTP 和下载都不执行，直接返回
            return tcp_result

        if not self.http_enabled:
            # 用户在界面上关闭了 HTTP 测试：只保留 TCP 结果（与第一阶段行为一致）
            return tcp_result

        # ---- 第二级：HTTP / HTTPS 连通性测试 ----
        http_result: Optional[HttpResult] = await http_test(
            ip,
            port,
            timeout_ms=self.http_timeout_ms,
            host=self.http_host,
        )
        summary.http_tested += 1
        if http_result.success:
            summary.http_success += 1
        else:
            summary.http_failed += 1

        # ---- 第三级：下载测速（只有 HTTP 成功且用户开启时才执行）----
        download_result: Optional[DownloadResult] = None
        if self.download_enabled and http_result.success:
            download_result = await download_test(
                ip,
                port,
                download_bytes=self.download_bytes,
                timeout_ms=self.download_timeout_ms,
                host=self.http_host,
            )
            summary.download_tested += 1
            if download_result.success:
                summary.download_success += 1
            else:
                summary.download_failed += 1

        # 把三级结果合并成一条完整记录
        return attach_stage_results(tcp_result, http_result, download_result)

    def _notify_stage(
        self,
        summary: ScanSummary,
        on_stage: Optional[StageCallback],
        force: bool = False,
    ) -> None:
        """上报各阶段统计（做了限流，避免刷新太频繁）。"""
        if on_stage is None:
            return

        now = time.perf_counter()
        if not force and now - self._last_stage_time < STAGE_MIN_INTERVAL_SEC:
            return
        self._last_stage_time = now
        self._safe_call(on_stage, StageStats.from_summary(summary))

    @staticmethod
    def _safe_call(callback: Optional[Callable], *args) -> None:
        """安全地调用回调：回调内部出错不能让测速流程中断。"""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("测速回调执行失败（已忽略）")