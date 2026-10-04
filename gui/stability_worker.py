"""稳定性复测线程（V1.4 新增）。

复用 V1.3 的 core.scanner.Scanner：每一轮复测就是用同样的参数
把同一批 TOP IP 重新跑一遍三级测试（TCP → HTTP → 下载）。

为什么要单独开线程？
    与 ScanWorker 相同的原因：多轮网络测速是耗时操作，
    放在 QThread 里执行，GUI 主线程只负责刷新界面，不会卡死。

停止机制：
    1. 停止当前轮：调用当前 Scanner.request_stop()（不再领取新任务，
       等正在执行的连接安全结束）；
    2. 不再开始下一轮。
    已经完成的轮次结果全部保留。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import QThread, Signal

from core.ip_loader import IPEntry
from core.scanner import Scanner
from core.stability import StabilityData, collect_stability
from core.tcp_tester import TestResult
from utils.logger import get_logger

logger: logging.Logger = get_logger()

# 复测轮数 / 并发的安全范围
MIN_ROUNDS = 1
MAX_ROUNDS = 20
MIN_STABILITY_CONCURRENCY = 1
MAX_STABILITY_CONCURRENCY = 100


class StabilityWorker(QThread):
    """在子线程中对 TOP IP 进行多轮复测。"""

    # 每测完一个 IP 发出：轮次, 已完成数(本轮), 本轮总数, 当前IP
    item_progress = Signal(int, int, int, str)
    # 一轮结束：轮次, 该轮结果列表
    round_finished = Signal(int, object)
    # 全部结束：{ip: StabilityData} 汇总, 用户是否中途停止, 用时(秒)
    stability_finished = Signal(object, bool, float)
    # 出错：中文错误信息
    stability_failed = Signal(str)

    def __init__(
        self,
        entries: Sequence[IPEntry],
        rounds: int,
        concurrency: int,
        port: int = 443,
        timeout_ms: int = 1000,
        http_enabled: bool = True,              # V1.4：与主测速的开关保持一致
        http_timeout_ms: int = 3000,
        download_enabled: bool = True,          # V1.4：与主测速的开关保持一致
        download_bytes: int = 1024 * 1024,
        download_timeout_ms: int = 10000,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._entries: List[IPEntry] = list(entries)
        self._rounds = max(MIN_ROUNDS, min(int(rounds), MAX_ROUNDS))
        self._concurrency = max(
            MIN_STABILITY_CONCURRENCY, min(int(concurrency), MAX_STABILITY_CONCURRENCY)
        )
        # 其余参数与主测速保持一致（来自界面设置）
        self._port = port
        self._timeout_ms = timeout_ms
        self._http_timeout_ms = http_timeout_ms
        self._download_bytes = download_bytes
        self._download_timeout_ms = download_timeout_ms
        self._http_enabled = http_enabled
        self._download_enabled = download_enabled

        self._stop_requested = False            # 用户是否请求停止
        self._current_scanner: Optional[Scanner] = None
        self._start_time = 0.0

    # ------------------------------------------------------------------
    # 线程主体
    # ------------------------------------------------------------------
    def run(self) -> None:
        """主循环：一轮一轮地复测，直到全部轮数完成或用户停止。"""
        import asyncio

        self._start_time = _now()
        rounds_results: List[List[TestResult]] = []
        stopped = False
        loop = None

        try:
            # 与 ScanWorker 相同的做法：在子线程里创建独立的事件循环
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            for round_index in range(1, self._rounds + 1):
                if self._stop_requested:
                    break  # 不再开始下一轮

                scanner = Scanner(
                    port=self._port,
                    concurrency=self._concurrency,
                    timeout_ms=self._timeout_ms,
                    http_enabled=self._http_enabled,
                    http_timeout_ms=self._http_timeout_ms,
                    download_enabled=self._download_enabled,
                    download_bytes=self._download_bytes,
                    download_timeout_ms=self._download_timeout_ms,
                )
                self._current_scanner = scanner
                logger.info(
                    "稳定性复测：第 %s/%s 轮开始，目标 %s 个 IP，并发 %s",
                    round_index, self._rounds, len(self._entries), self._concurrency,
                )
                # 完全复用 V1.3 的 Scanner：同样的三级测试、并发与停止机制
                round_results: List[TestResult] = []
                summary = loop.run_until_complete(scanner.run(
                    self._entries,
                    on_result=round_results.append,
                    on_progress=self._make_progress_callback(round_index, round_results),
                ))
                rounds_results.append(round_results)
                self.round_finished.emit(round_index, list(round_results))

                if summary.stopped:
                    stopped = True
                    break
        except Exception as exc:  # 兜底：任何异常都不能让程序崩溃
            logger.exception("稳定性复测线程发生异常")
            self.stability_failed.emit(f"稳定性复测过程中发生错误：{exc}")
            return
        finally:
            self._current_scanner = None
            if loop is not None:
                try:
                    loop.close()
                except Exception:
                    logger.exception("关闭复测事件循环失败（已忽略）")

        stability_map: Dict[str, StabilityData] = collect_stability(rounds_results)
        elapsed = _now() - self._start_time
        logger.info(
            "稳定性复测结束：完成 %s 轮（计划 %s 轮），用户停止=%s，用时 %.1f 秒",
            len(rounds_results), self._rounds, stopped, elapsed,
        )
        self.stability_finished.emit(stability_map, stopped, elapsed)

    def _make_progress_callback(self, round_index: int, round_results: List[TestResult]):
        """生成一轮内部的进度回调（转发成 Qt 信号）。"""
        total = len(self._entries)

        def on_progress(tested: int, _success: int, _failed: int, _total: int) -> None:
            # “当前 IP”显示最近完成的一个（网络探测无法预知下一个）
            current_ip = round_results[-1].ip if round_results else ""
            self.item_progress.emit(round_index, tested, total, current_ip)

        return on_progress

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        """由 GUI 主线程调用：请求停止复测（不强制结束线程）。"""
        logger.info("收到停止稳定性复测请求")
        self._stop_requested = True
        scanner = self._current_scanner
        if scanner is not None:
            # 停止当前轮：不再领取新任务，正在执行的连接安全结束
            scanner.request_stop()

    @property
    def rounds(self) -> int:
        return self._rounds


def _now() -> float:
    import time
    return time.perf_counter()