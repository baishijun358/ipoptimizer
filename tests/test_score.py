"""core/score.py 的单元测试（V1.3）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.score import (  # noqa: E402
    WEIGHT_DOWNLOAD_SPEED,
    WEIGHT_HTTP_LATENCY,
    WEIGHT_STABILITY,
    WEIGHT_TCP_LATENCY,
    compute_score,
    normalize_higher_better,
    normalize_lower_better,
    score_results,
)
from core.tcp_tester import TestResult  # noqa: E402


def make_result(ip="104.16.0.1", success=True, tcp_ms=80, http_ms=200,
                speed_bps=1 * 1024 * 1024, status=200) -> TestResult:
    """构造一个下载成功的标准结果。"""
    return TestResult(
        ip=ip, port=443, latency=tcp_ms, success=success, error=None,
        http_tested=True, http_success=True,
        http_status=status, http_latency=http_ms, http_error=None,
        download_tested=True, download_success=True,
        download_speed_bps=speed_bps, download_bytes=1024 * 1024,
        download_elapsed_ms=1000, download_error=None,
    )


class TestNormalize(unittest.TestCase):
    """归一化函数的基本性质。"""

    def test_lower_better_bounds(self) -> None:
        """低延迟得高分，极高延迟 0 分，None 得 0。"""
        self.assertEqual(normalize_lower_better(50, 50, 1000), 1.0)
        self.assertEqual(normalize_lower_better(1000, 50, 1000), 0.0)
        self.assertEqual(normalize_lower_better(None, 50, 1000), 0.0)

    def test_higher_better_bounds(self) -> None:
        """高速得高分，极低速 0 分，None 得 0。"""
        self.assertEqual(normalize_higher_better(5 * 1024 * 1024, 5 * 1024 * 1024, 10240), 1.0)
        self.assertEqual(normalize_higher_better(10240, 5 * 1024 * 1024, 10240), 0.0)
        self.assertEqual(normalize_higher_better(None, 5 * 1024 * 1024, 10240), 0.0)

    def test_extreme_value_does_not_crush_others(self) -> None:
        """极端高速 IP 满分，但中等速度 IP 仍应有明显区分度（>0.5）。"""
        extreme = normalize_higher_better(100 * 1024 * 1024, 5 * 1024 * 1024, 10240)
        middle = normalize_higher_better(1 * 1024 * 1024, 5 * 1024 * 1024, 10240)
        self.assertEqual(extreme, 1.0)
        self.assertGreater(middle, 0.5)


class TestComputeScore(unittest.TestCase):
    """评分规则。"""

    def test_weights_sum_to_one(self) -> None:
        """权重总和必须等于 1。"""
        total = (WEIGHT_TCP_LATENCY + WEIGHT_HTTP_LATENCY
                 + WEIGHT_DOWNLOAD_SPEED + WEIGHT_STABILITY)
        self.assertAlmostEqual(total, 1.0)

    def test_low_latency_high_speed_gets_high_score(self) -> None:
        """低延迟 + 高速 = 高分（>=80）。"""
        result = make_result(tcp_ms=30, http_ms=80, speed_bps=6 * 1024 * 1024)
        score = compute_score(result)
        self.assertIsNotNone(score)
        self.assertGreaterEqual(score, 80)

    def test_high_latency_low_speed_gets_low_score(self) -> None:
        """高延迟 + 低速 = 低分（<=40）。"""
        result = make_result(tcp_ms=900, http_ms=2500, speed_bps=20 * 1024)
        score = compute_score(result)
        self.assertIsNotNone(score)
        self.assertLessEqual(score, 40)

    def test_score_range(self) -> None:
        """任何可评分结果的分数都必须在 0~100。"""
        for tcp in (10, 100, 500, 1500):
            for speed in (1024, 100 * 1024, 1024 * 1024, 20 * 1024 * 1024):
                score = compute_score(make_result(tcp_ms=tcp, speed_bps=speed))
                self.assertIsNotNone(score)
                self.assertGreaterEqual(score, 0)
                self.assertLessEqual(score, 100)

    def test_failed_ip_not_scored(self) -> None:
        """测速失败的 IP 不能有分数。"""
        failed = TestResult(ip="1.2.3.4", port=443, latency=None,
                            success=False, error="timeout")
        self.assertIsNone(compute_score(failed))

    def test_download_failed_ip_not_scored(self) -> None:
        """下载失败的 IP 不能获得高分（不应参与评分）。"""
        no_download = TestResult(ip="1.2.3.4", port=443, latency=50, success=True,
                                 error=None, http_status=200, http_latency=100,
                                 http_tested=True, http_success=True,
                                 http_error=None,
                                 download_tested=True, download_success=False,
                                 download_speed_bps=None, download_bytes=0,
                                 download_elapsed_ms=None,
                                 download_error="timeout")
        self.assertIsNone(compute_score(no_download))


class TestScoreResults(unittest.TestCase):
    """批量评分。"""

    def test_batch_scoring(self) -> None:
        """成功 IP 有分，失败 IP 无分，且顺序与输入一致。"""
        results = [
            make_result("104.16.0.1", tcp_ms=50, speed_bps=3 * 1024 * 1024),
            TestResult(ip="1.2.3.4", port=443, latency=None, success=False, error="timeout"),
            make_result("104.16.0.2", tcp_ms=200, speed_bps=300 * 1024),
        ]
        scored = score_results(results)
        self.assertEqual(len(scored), 3)
        self.assertIsNotNone(scored[0].score)
        self.assertIsNone(scored[1].score)
        self.assertFalse(scored[1].ranked)
        self.assertTrue(scored[0].ranked)


if __name__ == "__main__":
    unittest.main(verbosity=2)