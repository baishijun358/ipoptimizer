"""排名模块（V1.3 新增）。

排序规则（按用户要求）：
1. 成功（可评分）的 IP 优先，失败 IP 永远排在后面；
2. 成功 IP 之间按综合评分从高到低；
3. 评分相同 → 下载速度高的优先；
4. 仍然相同 → TCP 延迟低的优先；
5. 最后按 IP 文本兜底，保证排序稳定。

筛选条件（最低速度 / 最大 TCP 延迟）也在这里实现：
只有满足条件的 IP 才进入最终排名。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from core.score import ScoredResult, score_results
from core.stability import (
    FINAL_SCORE_BASE_WEIGHT,
    FINAL_SCORE_STABILITY_WEIGHT,
    compute_stability,
)
from core.tcp_tester import TestResult

# 筛选选项
SPEED_FILTER_OPTIONS = (
    ("不限", None),
    ("0.1 MB/s", int(0.1 * 1024 * 1024)),
    ("0.5 MB/s", int(0.5 * 1024 * 1024)),
    ("1 MB/s", 1 * 1024 * 1024),
    ("5 MB/s", 5 * 1024 * 1024),
)

LATENCY_FILTER_OPTIONS = (
    ("不限", None),
    ("100ms", 100),
    ("200ms", 200),
    ("300ms", 300),
    ("500ms", 500),
)

# TOP 数量选项与默认值
TOP_OPTIONS = (10, 20, 50, 100)
DEFAULT_TOP_N = 100


@dataclass
class RankEntry:
    """排名后的一条记录（带最终名次）。"""

    rank: int                 # 从 1 开始的最终名次
    result: TestResult
    score: Optional[int]      # 综合评分；失败 IP 为 None


def passes_filters(
    result: TestResult,
    min_speed_bps: Optional[int] = None,
    max_tcp_latency_ms: Optional[int] = None,
) -> bool:
    """判断一个结果是否满足筛选条件。

    只有「可评分」（下载成功）的 IP 才有可能通过筛选。
    """
    if result.download_speed_bps is None:
        return False
    if min_speed_bps is not None and result.download_speed_bps < min_speed_bps:
        return False
    if max_tcp_latency_ms is not None:
        if result.latency is None or result.latency > max_tcp_latency_ms:
            return False
    return True


def _sort_key(scored: ScoredResult):
    """排序键：见模块 docstring 的 5 条规则。"""
    result = scored.result
    score = scored.score if scored.score is not None else -1
    speed = result.download_speed_bps if result.download_speed_bps is not None else -1.0
    tcp_latency = result.latency if result.latency is not None else 10**9
    return (-score, -speed, tcp_latency, result.ip)


def build_ranking(
    results: Sequence[TestResult],
    min_speed_bps: Optional[int] = None,
    max_tcp_latency_ms: Optional[int] = None,
    top_n: Optional[int] = DEFAULT_TOP_N,
) -> List[RankEntry]:
    """生成最终排名列表。

    参数：
        results：全部测速结果（含失败 IP）
        min_speed_bps：最低下载速度筛选（None = 不限）
        max_tcp_latency_ms：最大 TCP 延迟筛选（None = 不限）
        top_n：只保留前 N 名（None = 全部保留）
    返回：
        带 rank 的列表（rank 从 1 开始）
    """
    scored = score_results(list(results))

    # 1) 先拆成「可排名」和「不可排名」两组
    ranked_pool = [item for item in scored if item.ranked]
    unranked_pool = [item for item in scored if not item.ranked]

    # 2) 对可排名组应用筛选条件
    ranked_pool = [
        item for item in ranked_pool
        if passes_filters(item.result, min_speed_bps, max_tcp_latency_ms)
    ]

    # 3) 排序：成功 IP 评分降序在前；失败 IP 按原有规则排在最后
    ranked_pool.sort(key=_sort_key)
    unranked_pool.sort(key=_sort_key)

    # 4) 编名次并截取 TOP N
    entries: List[RankEntry] = []
    rank = 1
    for item in ranked_pool:
        if top_n is not None and rank > top_n:
            break
        entries.append(RankEntry(rank=rank, result=item.result, score=item.score))
        rank += 1

    # 失败 IP 追加在有效排名之后（不占名次语义，但仍要显示）
    for item in unranked_pool:
        entries.append(RankEntry(rank=0, result=item.result, score=None))

    return entries


# ======================================================================
# V1.4：最终排名（稳定性复测之后）
# ======================================================================
@dataclass
class FinalEntry:
    """带稳定性与最终评分的排名记录。"""

    rank: int                          # 最终名次（从 1 开始）
    result: TestResult                 # 复测时使用的参考结果（V1.3 排名里的那条）
    score: int                         # V1.3 综合评分
    stability_score: int               # 稳定性评分（0~100）
    final_score: int                   # 最终评分 = 评分 × 0.9 + 稳定性 × 0.1
    data: "object" = None              # 对应的 StabilityData（core.stability.StabilityData）


def compute_final_score(score: int, stability_score: int) -> int:
    """最终评分 = V1.3综合评分 × 0.9 + 稳定性评分 × 0.1（四舍五入，.5 进位）。

    用 math.floor(total + 0.5) 而不是内置 round()：
    Python 的 round() 是银行家舍入，80.5 会被舍成 80，与「四舍五入」直觉不符。
    """
    total = (score * FINAL_SCORE_BASE_WEIGHT
             + stability_score * FINAL_SCORE_STABILITY_WEIGHT)
    return int(math.floor(total + 0.5))


def build_final_ranking(
    ranking: Sequence[RankEntry],
    stability_map: Dict[str, "object"],
    top_n: Optional[int] = None,
) -> List[FinalEntry]:
    """生成 V1.4 最终排名。

    参数：
        ranking：V1.3 的排名结果（build_ranking 的返回值）
        stability_map：{ip: StabilityData}，多轮复测的汇总数据
        top_n：只保留前 N 名（None = 全部）
    排序优先级：
        1. 最终评分（高 → 低）
        2. 平均下载速度（高 → 低）
        3. 平均 TCP 延迟（低 → 高）
        4. IP 文本（保证排序稳定）
    只包含「有复测数据」的 IP：没有复测数据的 IP 仍保留 V1.3 排名显示。
    """
    scored: List[FinalEntry] = []
    for entry in ranking:
        if entry.score is None or entry.rank <= 0:
            continue
        data = stability_map.get(entry.result.ip)
        if data is None or data.rounds == 0:
            continue
        stability_score = compute_stability(data)
        final = compute_final_score(entry.score, stability_score)
        scored.append(FinalEntry(
            rank=0, result=entry.result, score=entry.score,
            stability_score=stability_score, final_score=final, data=data,
        ))

    def sort_key(item: FinalEntry):
        data = item.data
        avg_speed = data.avg_download_speed if data.avg_download_speed is not None else -1.0
        avg_tcp = data.avg_tcp_latency if data.avg_tcp_latency is not None else 10**9
        return (-item.final_score, -avg_speed, avg_tcp, item.result.ip)

    scored.sort(key=sort_key)
    entries: List[FinalEntry] = []
    for rank, item in enumerate(scored, start=1):
        if top_n is not None and rank > top_n:
            break
        item.rank = rank
        entries.append(item)
    return entries