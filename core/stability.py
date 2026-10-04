"""稳定性计算模块（V1.4 新增）。

对同一个 IP 的多次复测结果进行汇总，计算稳定性分数（0~100）。

设计要点：
1. 稳定性不能只看一次测试：同一个 IP 测 N 次，统计成功率与波动；
2. 成功率占 60 分（TCP / HTTP / 下载各 20 分）；
3. 波动占 40 分（TCP 延迟波动 20 分 + 下载速度波动 20 分）：
   用「变异系数 CV = 标准差 / 平均值」衡量波动，CV 越小越稳定；
4. 本模块只做计算，不做网络请求；网络请求仍然复用 V1.3 的 Scanner。

最终评分（V1.4）：
    最终评分 = V1.3综合评分 × 0.9 + 稳定性评分 × 0.1
V1.3 的评分公式（core/score.py）不做任何修改。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List

from core.tcp_tester import TestResult

# ------------------------------------------------------------------
# 稳定性评分权重（按分数累加到 100）
# ------------------------------------------------------------------
WEIGHT_TCP_RATE = 20.0        # TCP 成功率
WEIGHT_HTTP_RATE = 20.0       # HTTP 成功率
WEIGHT_DOWNLOAD_RATE = 20.0   # 下载成功率
WEIGHT_TCP_STEADY = 20.0      # TCP 延迟稳定（波动小）
WEIGHT_SPEED_STEADY = 20.0    # 下载速度稳定（波动小）

# 波动（变异系数 CV = 标准差 / 平均值）的满分/零分阈值
TCP_CV_GOOD = 0.15            # TCP CV <= 15% 算稳定（例如 80/82/81/85/83）
TCP_CV_BAD = 0.60             # TCP CV >= 60% 算剧烈波动（例如 70/400/90/800/100）
SPEED_CV_GOOD = 0.20          # 速度 CV <= 20% 算稳定
SPEED_CV_BAD = 1.00           # 速度 CV >= 100% 算剧烈波动

# 最终评分权重（V1.4）：最终评分 = V1.3评分 × 0.9 + 稳定性 × 0.1
FINAL_SCORE_BASE_WEIGHT = 0.9
FINAL_SCORE_STABILITY_WEIGHT = 0.1


@dataclass
class StabilityData:
    """一个 IP 的多轮复测原始数据与统计结果。"""

    ip: str
    port: int = 443
    rounds: int = 0                      # 已经执行的复测轮数

    # ---- 原始数据（每轮一条）----
    tcp_latencies: List[int] = field(default_factory=list)
    http_latencies: List[int] = field(default_factory=list)
    download_speeds: List[float] = field(default_factory=list)

    # ---- 各阶段尝试/成功计数 ----
    tcp_total: int = 0
    tcp_success: int = 0
    http_total: int = 0
    http_success: int = 0
    download_total: int = 0
    download_success: int = 0

    # ---- 成功率 ----
    @property
    def tcp_success_rate(self) -> float:
        return self.tcp_success / self.tcp_total if self.tcp_total else 0.0

    @property
    def http_success_rate(self) -> float:
        return self.http_success / self.http_total if self.http_total else 0.0

    @property
    def download_success_rate(self) -> float:
        return self.download_success / self.download_total if self.download_total else 0.0

    # ---- 延迟 / 速度统计 ----
    @property
    def avg_tcp_latency(self):
        return sum(self.tcp_latencies) / len(self.tcp_latencies) if self.tcp_latencies else None

    @property
    def min_tcp_latency(self):
        return min(self.tcp_latencies) if self.tcp_latencies else None

    @property
    def max_tcp_latency(self):
        return max(self.tcp_latencies) if self.tcp_latencies else None

    @property
    def avg_http_latency(self):
        return sum(self.http_latencies) / len(self.http_latencies) if self.http_latencies else None

    @property
    def avg_download_speed(self):
        return sum(self.download_speeds) / len(self.download_speeds) if self.download_speeds else None

    @property
    def max_download_speed(self):
        return max(self.download_speeds) if self.download_speeds else None

    @property
    def min_download_speed(self):
        return min(self.download_speeds) if self.download_speeds else None

    # ---- 波动（变异系数）----
    @property
    def tcp_cv(self):
        return _cv(self.tcp_latencies)

    @property
    def speed_cv(self):
        return _cv(self.download_speeds)

    # ---- 汇总 ----
    @property
    def total_tests(self) -> int:
        """总测试次数 = TCP + HTTP + 下载 三个阶段的总尝试次数。"""
        return self.tcp_total + self.http_total + self.download_total

    @property
    def total_success(self) -> int:
        return self.tcp_success + self.http_success + self.download_success

    @property
    def total_failed(self) -> int:
        return self.total_tests - self.total_success


def _cv(values: List[float]):
    """计算变异系数 CV = 标准差 / 平均值。

    样本不足 2 个时无法衡量波动，返回 None；平均值为 0 时也返回 None。
    """
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    if mean <= 0:
        return None
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance) / mean


def _cv_score(values: List[float], good: float, bad: float) -> float:
    """把波动换算成 0~1 的稳定性分（1 = 完全稳定）。

    三种情况要分开处理（V1.4 修正）：
    - 一个样本都没有（例如 TCP 从来没通过）：没有任何稳定证据 → 0 分，
      不能让「全程失败」的 IP 靠波动项白拿分数；
    - 只有一个样本（复测只有 1 轮就停止了）：无法判断波动 → 给满分，不误伤；
    - 两个以上样本：按变异系数 CV 换算（CV 越小越稳定）。
    """
    if not values:
        return 0.0
    cv = _cv(values)
    if cv is None:
        return 1.0
    if cv <= good:
        return 1.0
    if cv >= bad:
        return 0.0
    log_cv = math.log(cv)
    log_good = math.log(good)
    log_bad = math.log(bad)
    return (log_bad - log_cv) / (log_bad - log_good)


def add_round_result(data: StabilityData, result: TestResult) -> None:
    """把一轮复测结果（TestResult）累加进 StabilityData。"""
    data.rounds += 1
    data.tcp_total += 1

    if not result.success:
        return  # 本轮 TCP 失败：HTTP / 下载都不会执行

    data.tcp_success += 1
    if result.latency is not None:
        data.tcp_latencies.append(result.latency)

    if not result.http_tested:
        return
    data.http_total += 1
    if not result.http_success:
        return  # HTTP 失败的轮不会执行下载
    data.http_success += 1
    if result.http_latency is not None:
        data.http_latencies.append(result.http_latency)

    if not result.download_tested:
        return
    data.download_total += 1
    if result.download_success and result.download_speed_bps:
        data.download_success += 1
        data.download_speeds.append(result.download_speed_bps)


def compute_stability(data: StabilityData) -> int:
    """计算稳定性分数（0~100）。

    组成：TCP成功率 20 + HTTP成功率 20 + 下载成功率 20
         + TCP延迟稳定性 20 + 下载速度稳定性 20
    """
    rate_part = (
        data.tcp_success_rate * WEIGHT_TCP_RATE
        + data.http_success_rate * WEIGHT_HTTP_RATE
        + data.download_success_rate * WEIGHT_DOWNLOAD_RATE
    )
    steady_part = (
        _cv_score(data.tcp_latencies, TCP_CV_GOOD, TCP_CV_BAD) * WEIGHT_TCP_STEADY
        + _cv_score(data.download_speeds, SPEED_CV_GOOD, SPEED_CV_BAD) * WEIGHT_SPEED_STEADY
    )
    return int(round(max(0.0, min(100.0, rate_part + steady_part))))


def collect_stability(results_by_round: List[List[TestResult]]) -> Dict[str, StabilityData]:
    """把多轮复测结果按 IP 汇总成 StabilityData。

    参数：results_by_round，每轮的测速结果列表（下标 0 是第 1 轮）。
    返回：{ip: StabilityData}
    """
    mapping: Dict[str, StabilityData] = {}
    for round_results in results_by_round:
        for result in round_results:
            data = mapping.get(result.ip)
            if data is None:
                data = StabilityData(ip=result.ip, port=result.port)
                mapping[result.ip] = data
            add_round_result(data, result)
    return mapping