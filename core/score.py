"""综合评分模块（V1.3 新增）。

只对「下载测速成功」的 IP 评分，评分范围 0~100：

    TCP 延迟  25%   （越低越好）
    HTTP 延迟 20%   （越低越好）
    下载速度  45%   （越高越好）
    稳定性    10%   （第一阶段用下载成功兜底，详见下方说明）

设计要点：
1. 所有指标先做「归一化」再加权，避免某个极端 IP 把其他 IP 全部压成低分；
2. 归一化使用对数 / 分段方式，而不是简单线性（简单线性会被一个超快 IP 垄断）；
3. 评分公式集中在本文件，GUI 只调用，不写死公式。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from core.tcp_tester import TestResult

# ------------------------------------------------------------------
# 权重配置（总和必须等于 1.0）
# ------------------------------------------------------------------
WEIGHT_TCP_LATENCY = 0.25
WEIGHT_HTTP_LATENCY = 0.20
WEIGHT_DOWNLOAD_SPEED = 0.45
WEIGHT_STABILITY = 0.10

# 归一化参考值（可按需要调整，不需要改算法结构）
TCP_LATENCY_GOOD_MS = 50.0      # TCP 延迟 <= 50ms 视为满分
TCP_LATENCY_BAD_MS = 1000.0     # TCP 延迟 >= 1000ms 视为 0 分
HTTP_LATENCY_GOOD_MS = 100.0    # HTTP 延迟 <= 100ms 视为满分
HTTP_LATENCY_BAD_MS = 3000.0    # HTTP 延迟 >= 3000ms 视为 0 分
SPEED_GOOD_BPS = 5 * 1024 * 1024.0    # 速度 >= 5 MB/s 视为满分
SPEED_BAD_BPS = 10 * 1024.0           # 速度 <= 10 KB/s 视为 0 分


@dataclass
class ScoredResult:
    """带评分的测速结果。"""

    result: TestResult
    score: int                     # 0~100 的整数分
    ranked: bool = True            # 是否参与排名（下载失败的不参与）


def _clamp01(value: float) -> float:
    """把数值限制在 0~1 之间。"""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def normalize_lower_better(value_ms: Optional[float], good: float, bad: float) -> float:
    """「越低越好」类指标的归一化，返回 0~1（1 = 最好）。

    使用对数刻度：延迟从 50ms 涨到 100ms 的「恶化感」和从 500ms 涨到 1000ms 类似，
    线性刻度会让高延迟段的区分度太差。
    """
    if value_ms is None or value_ms <= 0:
        return 0.0
    value_ms = float(value_ms)
    good = max(float(good), 1.0)
    bad = max(float(bad), good + 1.0)
    if value_ms <= good:
        return 1.0
    if value_ms >= bad:
        return 0.0
    log_value = math.log(value_ms)
    log_good = math.log(good)
    log_bad = math.log(bad)
    return _clamp01((log_bad - log_value) / (log_bad - log_good))


def normalize_higher_better(value_bps: Optional[float], good: float, bad: float) -> float:
    """「越高越好」类指标的归一化，返回 0~1（1 = 最好）。

    同样使用对数刻度：10KB/s→100KB/s 的提升感和 1MB/s→10MB/s 类似。
    """
    if value_bps is None or value_bps <= 0:
        return 0.0
    value_bps = float(value_bps)
    bad = max(float(bad), 1.0)
    good = max(float(good), bad + 1.0)
    if value_bps >= good:
        return 1.0
    if value_bps <= bad:
        return 0.0
    log_value = math.log(value_bps)
    log_bad = math.log(bad)
    log_good = math.log(good)
    return _clamp01((log_value - log_bad) / (log_good - log_bad))


def compute_score(result: TestResult) -> Optional[int]:
    """计算单个 IP 的综合评分。

    返回 None 表示「不参与评分」（下载失败 / 没有下载数据）。
    """
    # 基本门槛：整个测速成功，并且下载确实测过且有速度
    if not result.success:
        return None
    if result.download_speed_bps is None or result.download_speed_bps <= 0:
        return None

    tcp_score = normalize_lower_better(
        float(result.latency) if result.latency is not None else None,
        TCP_LATENCY_GOOD_MS,
        TCP_LATENCY_BAD_MS,
    )
    http_score = normalize_lower_better(
        float(result.http_latency) if result.http_latency is not None else None,
        HTTP_LATENCY_GOOD_MS,
        HTTP_LATENCY_BAD_MS,
    )
    speed_score = normalize_higher_better(
        result.download_speed_bps,
        SPEED_GOOD_BPS,
        SPEED_BAD_BPS,
    )

    # 稳定性：本阶段没有多次重测数据，用「HTTP 拿到正常状态码」兜底，
    # 即 HTTP 2xx/3xx 记 1.0，其他情况记 0.5。将来多次重测后可替换成真实稳定率。
    stability_score = 1.0 if (result.http_status is not None and 200 <= result.http_status < 400) else 0.5

    total = (
        tcp_score * WEIGHT_TCP_LATENCY
        + http_score * WEIGHT_HTTP_LATENCY
        + speed_score * WEIGHT_DOWNLOAD_SPEED
        + stability_score * WEIGHT_STABILITY
    )
    # 四舍五入成 0~100 的整数分
    return int(round(_clamp01(total) * 100))


def score_results(results: List[TestResult]) -> List[ScoredResult]:
    """给一批结果评分。

    返回与输入等长的列表（顺序不变）：
    - 下载成功的 IP 有 score（0~100），ranked=True；
    - 其他 IP score 为 None，ranked=False（不参与排名，但仍会显示在表格里）。
    """
    scored: List[ScoredResult] = []
    for item in results:
        score = compute_score(item)
        scored.append(ScoredResult(result=item, score=score, ranked=score is not None))
    return scored