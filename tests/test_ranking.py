"""core/ranking.py 的单元测试（V1.3）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.ranking import (  # noqa: E402
    DEFAULT_TOP_N,
    build_ranking,
    passes_filters,
)
from core.tcp_tester import TestResult  # noqa: E402


def make_result(ip="104.16.0.1", success=True, tcp_ms=80, http_ms=200,
                speed_bps=1 * 1024 * 1024) -> TestResult:
    return TestResult(
        ip=ip, port=443, latency=tcp_ms, success=success, error=None,
        http_tested=True, http_success=True,
        http_status=200, http_latency=http_ms, http_error=None,
        download_tested=True, download_success=True,
        download_speed_bps=speed_bps, download_bytes=1024 * 1024,
        download_elapsed_ms=1000, download_error=None,
    )


def make_failed(ip="9.9.9.9", error="timeout") -> TestResult:
    return TestResult(ip=ip, port=443, latency=None, success=False, error=error)


class TestRankingOrder(unittest.TestCase):
    """排序规则。"""

    def test_failed_come_after_valid(self) -> None:
        """失败 IP 不能排到有效 IP 前面。"""
        results = [make_failed(), make_result("104.16.0.1", tcp_ms=500, speed_bps=100 * 1024)]
        ranking = build_ranking(results, top_n=None)
        self.assertEqual(ranking[0].result.ip, "104.16.0.1")
        self.assertEqual(ranking[0].rank, 1)
        self.assertGreater(len(ranking), 1)
        self.assertEqual(ranking[1].score, None)  # 失败 IP 无评分

    def test_higher_score_first(self) -> None:
        """评分高的在前。"""
        fast = make_result("1.1.1.1", tcp_ms=30, http_ms=60, speed_bps=6 * 1024 * 1024)
        slow = make_result("2.2.2.2", tcp_ms=800, http_ms=2500, speed_bps=15 * 1024)
        ranking = build_ranking([slow, fast], top_n=None)
        self.assertEqual(ranking[0].result.ip, "1.1.1.1")
        self.assertGreater(ranking[0].score, ranking[1].score)

    def test_same_score_speed_tiebreak(self) -> None:
        """评分相同时，下载速度高的优先。"""
        # 构造两个指标差异很小但速度不同的结果
        a = make_result("1.1.1.1", tcp_ms=100, http_ms=300, speed_bps=2 * 1024 * 1024)
        b = make_result("2.2.2.2", tcp_ms=100, http_ms=300, speed_bps=4 * 1024 * 1024)
        ranking = build_ranking([a, b], top_n=None)
        # b 速度高，应排在前面（评分应 >= a）
        self.assertEqual(ranking[0].result.ip, "2.2.2.2")

    def test_rank_starts_at_one(self) -> None:
        """名次从 1 开始连续编号。"""
        results = [make_result(f"104.16.0.{i}", tcp_ms=100 + i * 10,
                               speed_bps=2 * 1024 * 1024 - i * 100 * 1024)
                   for i in range(1, 6)]
        ranking = build_ranking(results, top_n=None)
        self.assertEqual([entry.rank for entry in ranking], [1, 2, 3, 4, 5])


class TestTopN(unittest.TestCase):
    """TOP N 截取。"""

    def _many_results(self, count: int):
        return [make_result(f"104.16.{i // 250}.{i % 250 + 1}",
                            tcp_ms=50 + i, speed_bps=1024 * 1024 + i * 1024)
                for i in range(count)]

    def test_top100_limit(self) -> None:
        """TOP100 数量不能超过 100。"""
        results = self._many_results(300)
        ranking = build_ranking(results, top_n=100)
        valid = [entry for entry in ranking if entry.score is not None]
        self.assertEqual(len(valid), 100)
        self.assertEqual(DEFAULT_TOP_N, 100)

    def test_top10(self) -> None:
        ranking = build_ranking(self._many_results(50), top_n=10)
        valid = [entry for entry in ranking if entry.score is not None]
        self.assertEqual(len(valid), 10)

    def test_fewer_than_top_n(self) -> None:
        """有效 IP 少于 N 时显示全部。"""
        results = self._many_results(7)
        ranking = build_ranking(results, top_n=100)
        valid = [entry for entry in ranking if entry.score is not None]
        self.assertEqual(len(valid), 7)


class TestFilters(unittest.TestCase):
    """筛选条件。"""

    def test_min_speed_filter(self) -> None:
        """低于最低速度的 IP 被过滤。"""
        slow = make_result(speed_bps=50 * 1024)
        fast = make_result(speed_bps=2 * 1024 * 1024)
        self.assertFalse(passes_filters(slow, min_speed_bps=1024 * 1024))
        self.assertTrue(passes_filters(fast, min_speed_bps=1024 * 1024))

    def test_max_latency_filter(self) -> None:
        """超过最大 TCP 延迟的 IP 被过滤。"""
        laggy = make_result(tcp_ms=400)
        quick = make_result(tcp_ms=90)
        self.assertFalse(passes_filters(laggy, max_tcp_latency_ms=300))
        self.assertTrue(passes_filters(quick, max_tcp_latency_ms=300))

    def test_filter_in_ranking(self) -> None:
        """筛选条件在 build_ranking 中生效。"""
        results = [
            make_result("1.1.1.1", tcp_ms=80, speed_bps=2 * 1024 * 1024),
            make_result("2.2.2.2", tcp_ms=80, speed_bps=50 * 1024),  # 低于 0.5MB/s
        ]
        ranking = build_ranking(results, min_speed_bps=int(0.5 * 1024 * 1024), top_n=None)
        valid_ips = [entry.result.ip for entry in ranking if entry.score is not None]
        self.assertEqual(valid_ips, ["1.1.1.1"])

    def test_failed_never_pass_filter(self) -> None:
        """失败 IP 永远不通过筛选。"""
        self.assertFalse(passes_filters(make_failed()))


if __name__ == "__main__":
    unittest.main(verbosity=2)