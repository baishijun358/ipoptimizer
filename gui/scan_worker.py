"""测速线程。

为什么要单独开线程？
    测速是耗时的网络操作，如果直接在 GUI 主线程里执行，窗口会“未响应”。
    所以这里用 QThread 在后台线程里跑 asyncio 事件循环，
    并通过 Qt 信号把结果和进度安全地传回主线程更新界面。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional, Sequence

from PySide6.QtCore import QThread, Signal

from core.ip_loader import IPEntry
from core.scanner import (
    DEFAULT_DOWNLOAD_BYTES,
    DEFAULT_DOWNLOAD_TIMEOUT_MS,
    DEFAULT_HTTP_TIMEOUT_MS,
    Scanner,
    ScanSummary,
    StageStats,
)
from core.tcp_tester import TestResult
from utils.logger import get_logger

logger: logging.Logger = get_logger()

# 进度信号的最小发送间隔（秒）：避免上千个信号把界面刷爆
PROGRESS_MIN_INTERVAL_SEC = 0.1


class ScanWorker(QThread):
    """在子线程中执行并发测速。"""

    # 每测完一个 IP 发出一次结果（object 用于跨线程传递 Python 对象）
    result_ready = Signal(object)
    # 进度：已测试, 成功, 失败, 总数
    progress_changed = Signal(int, int, int, int)
    # 各阶段统计（V1.2）：StageStats 快照对象
    stage_changed = Signal(object)
    # 结束：汇总信息, 耗时（秒）
    scan_finished = Signal(object, float)
    # 出错：中文错误信息
    scan_failed = Signal(str)

    def __init__(
        self,
        entries: Sequence[IPEntry],
        port: int,
        concurrency: int,
        timeout_ms: int,
        http_enabled: bool = True,
        http_timeout_ms: int = DEFAULT_HTTP_TIMEOUT_MS,
        download_enabled: bool = True,
        download_bytes: int = DEFAULT_DOWNLOAD_BYTES,
        download_timeout_ms: int = DEFAULT_DOWNLOAD_TIMEOUT_MS,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._entries: List[IPEntry] = list(entries)
        # 三级测试的参数全部交给 Scanner 统一管理（含范围保护）
        self._scanner = Scanner(
            port=port,
            concurrency=concurrency,
            timeout_ms=timeout_ms,
            http_enabled=http_enabled,
            http_timeout_ms=http_timeout_ms,
            download_enabled=download_enabled,
            download_bytes=download_bytes,
            download_timeout_ms=download_timeout_ms,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._start_time = 0.0
        self._last_progress_time = 0.0

    # ------------------------------------------------------------------
    # 线程主体
    # ------------------------------------------------------------------
    def run(self) -> None:  # QThread 入口，运行在子线程中
        self._start_time = time.perf_counter()
        summary = ScanSummary(total=len(self._entries))

        try:
            # 在子线程里创建独立的事件循环，避免和主线程互相影响
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            summary = self._loop.run_until_complete(
                self._scanner.run(
                    self._entries,
                    on_result=self._on_result,
                    on_progress=self._on_progress,
                    on_stage=self._on_stage,
                )
            )
        except Exception as exc:  # 兜底：任何异常都要告诉界面，而不是让程序崩溃
            logger.exception("测速线程发生异常")
            self.scan_failed.emit(f"测速过程中发生错误：{exc}")
            return
        finally:
            self._close_loop()

        self.scan_finished.emit(summary, time.perf_counter() - self._start_time)

    def _close_loop(self) -> None:
        """关闭子线程里的事件循环。"""
        loop = self._loop
        self._loop = None
        if loop is None:
            return
        try:
            loop.close()
        except Exception:
            logger.exception("关闭事件循环失败（已忽略）")

    # ------------------------------------------------------------------
    # 回调：在测速线程中执行，通过信号发给界面
    # ------------------------------------------------------------------
    def _on_result(self, result: TestResult) -> None:
        self.result_ready.emit(result)

    def _on_progress(self, tested: int, success: int, failed: int, total: int) -> None:
        now = time.perf_counter()
        # 限流：最多每 0.1 秒发一次进度；最后一个 IP 一定要上报
        if tested < total and now - self._last_progress_time < PROGRESS_MIN_INTERVAL_SEC:
            return
        self._last_progress_time = now
        self.progress_changed.emit(tested, success, failed, total)

    def _on_stage(self, stats: StageStats) -> None:
        """收到各阶段统计快照（限流由 core/scanner.py 负责），直接发给界面。"""
        self.stage_changed.emit(stats)

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        """由 GUI 主线程调用：请求停止测速（不强制结束线程）。"""
        logger.info("收到停止测速请求")
        self._scanner.request_stop()

    @property
    def elapsed_seconds(self) -> float:
        """已经运行的秒数（用于界面显示用时）。"""
        if not self._start_time:
            return 0.0
        return time.perf_counter() - self._start_time