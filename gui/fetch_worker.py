"""自动获取 IP 的后台线程。

与 scan_worker 同样的思路：
网络请求（Cloudflare API）放在 QThread 里执行，Qt 主线程只负责界面，
所以点击「开始获取」后界面不会卡死。

流程：请求 API -> 得到 CIDR -> 随机生成候选 IP -> 过滤 + 去重 -> 通过信号返回。
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Sequence, Set

from PySide6.QtCore import QThread, Signal

from core.cloudflare_source import (
    ApiTestResult,
    CloudflareError,
    DEFAULT_TIMEOUT_SECONDS,
    fetch_ipv4_cidrs,
    test_cloudflare_connection,
)
from core.cidr_generator import (
    MAX_GENERATE_COUNT,
    MIN_GENERATE_COUNT,
    GenerateResult,
    generate_candidates,
)
from utils.logger import get_logger

logger: logging.Logger = get_logger()


class FetchWorker(QThread):
    """在子线程中完成「获取 Cloudflare CIDR + 生成候选 IP」。"""

    # 完成：IP 列表, CIDR 数量, 耗时（秒）, 生成统计（GenerateResult）
    fetch_finished = Signal(object, int, float, object)
    # 失败：中文错误信息
    fetch_failed = Signal(str)

    def __init__(
        self,
        count: int,
        exclude_ips: Optional[Set[str]] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # 数量做安全限制，防止用户输入极端数值
        self._count = max(MIN_GENERATE_COUNT, min(int(count), MAX_GENERATE_COUNT))
        self._exclude_ips: Set[str] = set(exclude_ips or ())
        self._timeout_seconds = timeout_seconds

    def run(self) -> None:  # QThread 入口，运行在子线程中
        start = time.perf_counter()
        try:
            # 1) 请求 Cloudflare 官方 API（失败会抛 CloudflareError）
            fetch_result = fetch_ipv4_cidrs(timeout_seconds=self._timeout_seconds)
        except CloudflareError as exc:
            # 记录真实错误：分类 + 中文提示 + 真实异常 detail（detail 只进日志）
            logger.error(
                "自动获取 IP 失败：%s（category=%s, detail: %s）",
                exc,
                exc.category,
                exc.detail,
            )
            self.fetch_failed.emit(str(exc))
            return
        except Exception:  # 兜底：任何异常都不能让程序崩溃
            logger.exception("自动获取 IP 出现未预期错误")
            self.fetch_failed.emit("自动获取 IP 失败：发生未知错误，详情请查看日志。")
            return

        try:
            # 2) 随机生成候选 IP（自动去重 + 过滤私有/回环等）
            generate_result = generate_candidates(
                fetch_result.cidrs,
                count=self._count,
                exclude_ips=self._exclude_ips,
            )
        except Exception:  # 兜底
            logger.exception("生成候选 IP 出现未预期错误")
            self.fetch_failed.emit("生成候选 IP 失败：发生未知错误，详情请查看日志。")
            return

        ips: List[str] = generate_result.ips
        if not ips:
            self.fetch_failed.emit("没有生成任何有效的候选 IP，请稍后重试或增大数量。")
            return

        elapsed = time.perf_counter() - start
        cidr_count = len(fetch_result.cidrs)
        result = generate_result
        logger.info(
            "自动获取完成：CIDR %s 个，候选 IP %s 个（请求 %s，重复 %s，过滤 %s），耗时 %.2f 秒",
            cidr_count,
            len(ips),
            generate_result.requested_count,
            generate_result.duplicate_count,
            generate_result.invalid_count,
            elapsed,
        )
        self.fetch_finished.emit(ips, cidr_count, elapsed, result)

    @staticmethod
    def is_valid_count(value: int) -> bool:
        """数量是否在安全范围内（供界面校验输入）。"""
        return MIN_GENERATE_COUNT <= value <= MAX_GENERATE_COUNT


class TestConnectionWorker(QThread):
    """「测试Cloudflare连接」的后台线程：只测试 API 连通性，不生成 IP。"""

    # 测试完成：ApiTestResult（成功和失败都通过这个信号返回）
    test_finished = Signal(object)

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, parent=None) -> None:
        super().__init__(parent)
        self._timeout_seconds = timeout_seconds

    def run(self) -> None:  # QThread 入口，运行在子线程中
        try:
            # test_cloudflare_connection 内部已把真实错误写入日志
            result = test_cloudflare_connection(timeout_seconds=self._timeout_seconds)
        except Exception:  # 兜底：任何异常都不能让程序崩溃
            logger.exception("测试 Cloudflare 连接出现未预期错误")
            result = ApiTestResult(ok=False, error="测试失败：发生未知错误，详情请查看日志。")
        self.test_finished.emit(result)