"""稳定性模块的单元测试（V1.4）。

覆盖三部分：
1. core/stability.py：多轮复测汇总、成功率、波动（CV）、稳定性评分；
2. core/ranking.py 的 V1.4 部分：最终评分公式、最终排名规则；
3. utils/export.py 的 V1.4 部分：稳定 TOP100 TXT / 稳定性 CSV 导出。

全部用例都不依赖真实网络（用构造出来的 TestResult 模拟多轮结果）。
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import utils.export as export_mod  # noqa: E402
from core.ranking import (  # noqa: E402
    build_final_ranking,
    build_ranking,
    compute_final_score,
)
from core.stability import (  # noqa: E402
    StabilityData,
    add_round_result,
    collect_stability,
    compute_stability,
)
from core.tcp_tester import TestResult  # noqa: E402
from utils.export import (  # noqa: E402
    STABLE_CSV_HEADERS,
    ExportError,
    export_stable_csv,
    export_stable_txt,
)


def make_round(
    ip: str = "104.16.0.1",
    tcp_ms: int = 80,
    http_ms: int = 200,
    speed_bps: float = 1 * 1024 * 1024,
    tcp_ok: bool = True,
    http_ok: bool = True,
    download_ok: bool = True,
) -> TestResult:
    """构造一轮复测结果（模拟 Scanner 三级流程的产物）。

    tcp_ok / http_ok / download_ok 用来模拟「某一级失败」：
    任一级失败时，后面的级别不会再执行（与真实 Scanner 行为一致）。
    """
    if not tcp_ok:
        return TestResult(ip=ip, port=443, latency=None, success=False, error="timeout")
    if not http_ok:
        return TestResult(
            ip=ip, port=443, latency=tcp_ms, success=True, error=None,
            http_tested=True, http_success=False, http_error="timeout",
        )
    if not download_ok:
        return TestResult(
            ip=ip, port=443, latency=tcp_ms, success=True, error=None,
            http_tested=True, http_success=True, http_status=200,
            http_latency=http_ms, http_error=None,
            download_tested=True, download_success=False, download_error="下载超时",
        )
    return TestResult(
        ip=ip, port=443, latency=tcp_ms, success=True, error=None,
        http_tested=True, http_success=True,
        http_status=200, http_latency=http_ms, http_error=None,
        download_tested=True, download_success=True,
        download_speed_bps=speed_bps, download_bytes=1024 * 1024,
        download_elapsed_ms=1000, download_error=None,
    )


class TestRoundAccumulation(unittest.TestCase):
    """多轮结果累加：计数、成功率、平均 / 最低 / 最高值。"""

    def test_all_rounds_success(self) -> None:
        """5 轮全部成功：各阶段成功率都是 100%，TCP 延迟统计正确。"""
        data = StabilityData(ip="104.16.0.1")
        for tcp in (80, 82, 81, 85, 83):
            add_round_result(data, make_round(tcp_ms=tcp))

        self.assertEqual(data.rounds, 5)
        self.assertEqual(data.tcp_total, 5)
        self.assertEqual(data.tcp_success, 5)
        self.assertEqual(data.http_total, 5)
        self.assertEqual(data.http_success, 5)
        self.assertEqual(data.download_total, 5)
        self.assertEqual(data.download_success, 5)
        self.assertEqual(data.tcp_success_rate, 1.0)
        self.assertEqual(data.http_success_rate, 1.0)
        self.assertEqual(data.download_success_rate, 1.0)
        self.assertEqual(data.total_tests, 15)
        self.assertEqual(data.total_success, 15)
        self.assertEqual(data.total_failed, 0)
        self.assertEqual(data.min_tcp_latency, 80)
        self.assertEqual(data.max_tcp_latency, 85)
        self.assertAlmostEqual(data.avg_tcp_latency, (80 + 82 + 81 + 85 + 83) / 5)

    def test_tcp_failure_only_counts_tcp(self) -> None:
        """某轮 TCP 失败：只计入 TCP 尝试，HTTP / 下载不会执行。"""
        data = StabilityData(ip="104.16.0.2")
        add_round_result(data, make_round(tcp_ok=False))
        add_round_result(data, make_round())

        self.assertEqual(data.tcp_total, 2)
        self.assertEqual(data.tcp_success, 1)
        self.assertEqual(data.http_total, 1)          # 只有成功那轮才进入 HTTP
        self.assertEqual(data.http_success, 1)
        self.assertEqual(data.download_total, 1)
        self.assertEqual(data.tcp_success_rate, 0.5)
        self.assertEqual(data.http_success_rate, 1.0)  # 失败的轮不参与 HTTP 分母

    def test_http_failure_stops_download(self) -> None:
        """某轮 HTTP 失败：HTTP 记为尝试+失败，下载阶段完全不执行。"""
        data = StabilityData(ip="104.16.0.3")
        add_round_result(data, make_round(http_ok=False))
        add_round_result(data, make_round())

        self.assertEqual(data.http_total, 2)
        self.assertEqual(data.http_success, 1)
        self.assertEqual(data.download_total, 1)
        self.assertEqual(data.download_success, 1)
        self.assertEqual(data.download_success_rate, 1.0)
        self.assertEqual(len(data.http_latencies), 1)

    def test_download_failure_counted(self) -> None:
        """下载失败：下载成功率下降，且没有速度样本。"""
        data = StabilityData(ip="104.16.0.4")
        add_round_result(data, make_round(download_ok=False))
        add_round_result(data, make_round())

        self.assertEqual(data.download_total, 2)
        self.assertEqual(data.download_success, 1)
        self.assertEqual(data.download_success_rate, 0.5)
        self.assertEqual(len(data.download_speeds), 1)

    def test_collect_stability_groups_by_ip(self) -> None:
        """collect_stability 按 IP 汇总多轮结果。"""
        rounds = [
            [make_round(ip="1.1.1.1", tcp_ms=50), make_round(ip="2.2.2.2", tcp_ms=90)],
            [make_round(ip="1.1.1.1", tcp_ms=60)],
        ]
        mapping = collect_stability(rounds)

        self.assertEqual(set(mapping.keys()), {"1.1.1.1", "2.2.2.2"})
        self.assertEqual(mapping["1.1.1.1"].rounds, 2)
        self.assertEqual(mapping["2.2.2.2"].rounds, 1)
        self.assertEqual(mapping["1.1.1.1"].avg_tcp_latency, 55)


class TestVolatility(unittest.TestCase):
    """波动指标（变异系数 CV = 标准差 / 平均值）。"""

    def test_stable_latency_has_low_cv(self) -> None:
        """稳定 IP（80/82/81/85/83）波动很小。"""
        data = StabilityData(ip="a")
        for tcp in (80, 82, 81, 85, 83):
            add_round_result(data, make_round(tcp_ms=tcp))
        self.assertIsNotNone(data.tcp_cv)
        self.assertLess(data.tcp_cv, 0.15)

    def test_volatile_latency_has_high_cv(self) -> None:
        """抖动 IP（70/400/90/800/100）波动很大。"""
        data = StabilityData(ip="b")
        for tcp in (70, 400, 90, 800, 100):
            add_round_result(data, make_round(tcp_ms=tcp))
        self.assertIsNotNone(data.tcp_cv)
        self.assertGreater(data.tcp_cv, 0.60)

    def test_speed_cv(self) -> None:
        """下载速度波动同样用 CV 衡量。"""
        steady = StabilityData(ip="c")
        for speed in (1.00, 1.02, 0.99, 1.01, 1.00):
            add_round_result(steady, make_round(speed_bps=speed * 1024 * 1024))
        jumpy = StabilityData(ip="d")
        for speed in (2.00, 0.20, 1.50, 0.10, 1.80):
            add_round_result(jumpy, make_round(speed_bps=speed * 1024 * 1024))

        self.assertLess(steady.speed_cv, 0.05)
        self.assertGreater(jumpy.speed_cv, 0.60)

    def test_single_round_has_no_cv(self) -> None:
        """只有一轮数据无法判断波动：CV 为 None。"""
        data = StabilityData(ip="e")
        add_round_result(data, make_round())
        self.assertIsNone(data.tcp_cv)
        self.assertIsNone(data.speed_cv)


class TestComputeStability(unittest.TestCase):
    """稳定性评分（0~100）。"""

    def test_perfect_ip_scores_100(self) -> None:
        """每轮都成功 + 延迟/速度稳定 = 满分。"""
        data = StabilityData(ip="stable")
        for tcp in (80, 82, 81, 85, 83):
            add_round_result(data, make_round(tcp_ms=tcp, speed_bps=1024 * 1024))
        self.assertEqual(compute_stability(data), 100)

    def test_volatile_ip_scores_much_lower(self) -> None:
        """抖动 IP 即使某轮很快，稳定性也必须明显更低（用户要求第四条）。"""
        stable = StabilityData(ip="stable")
        volatile = StabilityData(ip="volatile")
        for tcp in (80, 82, 81, 85, 83):
            add_round_result(stable, make_round(tcp_ms=tcp, speed_bps=1024 * 1024))
        for tcp, speed in ((70, 2.0), (400, 0.2), (90, 1.5), (800, 0.1), (100, 1.8)):
            add_round_result(volatile, make_round(tcp_ms=tcp, speed_bps=speed * 1024 * 1024))

        stable_score = compute_stability(stable)
        volatile_score = compute_stability(volatile)
        self.assertEqual(stable_score, 100)
        self.assertLess(volatile_score, stable_score)
        # 波动占 40 分，明显抖动时至少要掉 20 分
        self.assertLessEqual(volatile_score, 80)

    def test_all_failed_ip_scores_zero(self) -> None:
        """一直失败的 IP 稳定性为 0 分。"""
        data = StabilityData(ip="dead")
        for _ in range(5):
            add_round_result(data, make_round(tcp_ok=False))
        self.assertEqual(compute_stability(data), 0)

    def test_score_always_in_range(self) -> None:
        """任何组合下分数都在 0~100 之间。"""
        for tcp_ok in (True, False):
            for http_ok in (True, False):
                data = StabilityData(ip="x")
                for _ in range(3):
                    add_round_result(data, make_round(tcp_ok=tcp_ok, http_ok=http_ok))
                score = compute_stability(data)
                self.assertGreaterEqual(score, 0)
                self.assertLessEqual(score, 100)


class TestFinalScore(unittest.TestCase):
    """最终评分公式：V1.3评分 × 0.9 ＋ 稳定性 × 0.1。"""

    def test_formula(self) -> None:
        self.assertEqual(compute_final_score(90, 0), 81)
        self.assertEqual(compute_final_score(90, 100), 91)
        self.assertEqual(compute_final_score(0, 0), 0)
        self.assertEqual(compute_final_score(100, 100), 100)

    def test_rounding(self) -> None:
        """四舍五入：80 × 0.9 + 85 × 0.1 = 80.5 → 81。"""
        self.assertEqual(compute_final_score(80, 85), 81)

    def test_never_out_of_range(self) -> None:
        """任何输入组合下最终评分都在 0~100 之间。"""
        for score in (0, 50, 100):
            for stability in (0, 50, 100):
                final = compute_final_score(score, stability)
                self.assertGreaterEqual(final, 0)
                self.assertLessEqual(final, 100)


class TestFinalRanking(unittest.TestCase):
    """最终排名：优先最终评分，其次平均速度、平均 TCP 延迟。"""

    @staticmethod
    def _ranking_and_stability():
        """构造 3 个 IP 的 V1.3 排名 + 其中 2 个的复测数据。"""
        results = [
            make_round(ip="1.1.1.1", tcp_ms=60, http_ms=150, speed_bps=3 * 1024 * 1024),
            make_round(ip="2.2.2.2", tcp_ms=80, http_ms=200, speed_bps=2 * 1024 * 1024),
            make_round(ip="3.3.3.3", tcp_ms=100, http_ms=250, speed_bps=1 * 1024 * 1024),
        ]
        ranking = build_ranking(results, top_n=None)

        # 1.1.1.1：每轮都稳定 → 稳定性满分
        stable = StabilityData(ip="1.1.1.1")
        for tcp in (60, 61, 60, 62, 60):
            add_round_result(stable, make_round(ip="1.1.1.1", tcp_ms=tcp, http_ms=150,
                                                speed_bps=3 * 1024 * 1024))
        # 2.2.2.2：延迟与速度大幅抖动 → 稳定性偏低
        jumpy = StabilityData(ip="2.2.2.2")
        for tcp in (70, 500, 90, 900, 100):
            add_round_result(jumpy, make_round(ip="2.2.2.2", tcp_ms=tcp, http_ms=200,
                                               speed_bps=0.3 * 1024 * 1024))
        return ranking, {"1.1.1.1": stable, "2.2.2.2": jumpy}

    def test_sorted_by_final_score(self) -> None:
        """最终排名按最终评分从高到低，名次从 1 开始连续。"""
        ranking, stability = self._ranking_and_stability()
        entries = build_final_ranking(ranking, stability, top_n=None)

        self.assertEqual(len(entries), 2)          # 3.3.3.3 没有复测数据，不进入最终排名
        self.assertEqual([e.rank for e in entries], [1, 2])
        scores = [e.final_score for e in entries]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(entries[0].result.ip, "1.1.1.1")

    def test_volatile_ip_ranked_after_stable(self) -> None:
        """稳定性差的 IP 即使延迟不差，也会被稳定性拉低名次。"""
        ranking, stability = self._ranking_and_stability()
        entries = build_final_ranking(ranking, stability, top_n=None)
        self.assertEqual(entries[-1].result.ip, "2.2.2.2")
        self.assertLess(entries[-1].stability_score, entries[0].stability_score)

    def test_top_n_limit(self) -> None:
        """TOP N 限制：数量不能超过要求的上限。"""
        results = [
            make_round(ip=f"10.0.0.{i}", tcp_ms=60 + i, speed_bps=(3 - i * 0.01) * 1024 * 1024)
            for i in range(1, 121)
        ]
        ranking = build_ranking(results, top_n=None)
        stability = {}
        for i in range(1, 121):
            data = StabilityData(ip=f"10.0.0.{i}")
            for tcp in (60 + i, 61 + i, 62 + i):
                add_round_result(data, make_round(ip=f"10.0.0.{i}", tcp_ms=tcp))
            stability[f"10.0.0.{i}"] = data

        self.assertEqual(len(build_final_ranking(ranking, stability, top_n=100)), 100)
        self.assertEqual(len(build_final_ranking(ranking, stability, top_n=None)), 120)

    def test_ip_without_stability_excluded(self) -> None:
        """没有复测数据的 IP 不进入最终排名（不拿一次结果冒充稳定）。"""
        ranking = build_ranking([make_round(ip="1.1.1.1")], top_n=None)
        self.assertEqual(build_final_ranking(ranking, {}, top_n=None), [])

    def test_tie_break_by_average_speed(self) -> None:
        """最终评分相同时，平均下载速度高的排前面（复测速度决定名次）。"""
        # 第一轮结果完全相同 → V1.3 综合评分相同 → 最终评分也相同
        results = [
            make_round(ip="1.1.1.1", tcp_ms=60, speed_bps=2 * 1024 * 1024),
            make_round(ip="2.2.2.2", tcp_ms=60, speed_bps=2 * 1024 * 1024),
        ]
        ranking = build_ranking(results, top_n=None)
        # 复测速度不同：2.2.2.2 更快
        speeds = {"1.1.1.1": 2.0, "2.2.2.2": 4.0}
        stability = {}
        for ip, speed in speeds.items():
            data = StabilityData(ip=ip)
            for _ in range(3):
                add_round_result(data, make_round(ip=ip, tcp_ms=60,
                                                  speed_bps=speed * 1024 * 1024))
            stability[ip] = data

        entries = build_final_ranking(ranking, stability, top_n=None)
        self.assertEqual(entries[0].final_score, entries[1].final_score)
        self.assertGreater(entries[0].data.avg_download_speed, entries[1].data.avg_download_speed)
        self.assertEqual(entries[0].result.ip, "2.2.2.2")


class StableExportTest(unittest.TestCase):
    """稳定性复测结果的 TXT / CSV 导出（输出目录指向临时目录，不污染 output/）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = export_mod.OUTPUT_DIR
        export_mod.OUTPUT_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        export_mod.OUTPUT_DIR = self._old_dir
        self._tmp.cleanup()

    @staticmethod
    def _entries(count: int = 3):
        """构造 count 个 IP 的最终排名（每个 IP 都有 3 轮复测数据）。"""
        results = [
            make_round(ip=f"104.16.0.{i}", tcp_ms=50 + i, speed_bps=(3 - i * 0.1) * 1024 * 1024)
            for i in range(1, count + 1)
        ]
        ranking = build_ranking(results, top_n=None)
        stability = {}
        for i in range(1, count + 1):
            data = StabilityData(ip=f"104.16.0.{i}")
            for _ in range(3):
                add_round_result(data, make_round(ip=f"104.16.0.{i}", tcp_ms=50 + i))
            stability[f"104.16.0.{i}"] = data
        return build_final_ranking(ranking, stability, top_n=None)

    def test_stable_txt_one_ip_per_line(self) -> None:
        """稳定 TXT：每行一个 IP，不含其他文字。"""
        entries = self._entries()
        path = export_stable_txt(entries, top_n=100)
        lines = path.read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], entries[0].result.ip)
        for line in lines:
            self.assertRegex(line, r"^(\d{1,3}\.){3}\d{1,3}$")
        self.assertTrue(path.name.startswith("IP优选_稳定TOP100_"))

    def test_stable_txt_respects_top_n(self) -> None:
        """稳定 TXT 的条数受 TOP N 限制。"""
        path = export_stable_txt(self._entries(5), top_n=2)
        self.assertEqual(len(path.read_text(encoding="utf-8").strip().splitlines()), 2)

    def test_stable_txt_empty_raises(self) -> None:
        """没有可导出的数据时抛出中文错误提示。"""
        with self.assertRaises(ExportError):
            export_stable_txt([], top_n=100)

    def test_stable_csv_headers_and_rows(self) -> None:
        """稳定性 CSV：表头完整、每行字段与结果一致。"""
        entries = self._entries()
        path = export_stable_csv(entries)
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))

        self.assertEqual(tuple(rows[0]), STABLE_CSV_HEADERS)
        self.assertEqual(len(rows), len(entries) + 1)
        first = rows[1]
        self.assertEqual(first[0], "1")
        self.assertEqual(first[1], entries[0].result.ip)
        self.assertEqual(first[2], "443")
        self.assertEqual(first[-1], str(entries[0].final_score))
        self.assertTrue(first[6].endswith("%"))  # TCP 成功率是百分比文本


if __name__ == "__main__":
    unittest.main(verbosity=2)
