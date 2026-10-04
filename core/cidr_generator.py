"""CIDR 随机 IP 生成模块。

核心原则：**绝不遍历整个网段**。

一个 /13 网段包含 50 多万个 IP，先全部生成再抽样会浪费大量内存和时间。
这里的做法是：
    1. 对每个 CIDR 随机抽取「网络内偏移量」（整数），而不是生成 IP 列表；
    2. 偏移量 -> IP 只在需要时才计算；
    3. 每抽到一个 IP 立即做合法性检查 + 去重；
    4. 凑够用户要的数量就立刻停止。

这样内存占用始终只有「已抽到的 IP」那么多，与网段总大小无关。
"""

from __future__ import annotations

import ipaddress
import logging
import random
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set, Tuple

from core.ip_validator import validate_ip
from utils.logger import get_logger

logger: logging.Logger = get_logger()

# 数量的安全范围（防止用户输入极端数值）
MIN_GENERATE_COUNT = 1
MAX_GENERATE_COUNT = 65536

# 单个 CIDR 的最大重试倍数：网段太小时避免死循环
MAX_ATTEMPT_MULTIPLIER = 50


@dataclass
class GenerateResult:
    """随机生成候选 IP 的结果。"""

    ips: List[str] = field(default_factory=list)      # 去重后的候选 IP
    requested_count: int = 0                          # 用户想要的数量
    duplicate_count: int = 0                          # 生成过程中遇到的重复数量
    invalid_count: int = 0                            # 被过滤掉的不合格 IP 数量
    skipped_cidrs: List[str] = field(default_factory=list)  # 无法使用的 CIDR

    @property
    def generated_count(self) -> int:
        """实际生成的数量。"""
        return len(self.ips)


def parse_cidrs(cidr_texts: Iterable[str]) -> Tuple[List[ipaddress.IPv4Network], List[str]]:
    """把 CIDR 文本解析成 IPv4Network 对象列表。

    返回 (合法网段列表, 被跳过的非法文本列表)。
    只保留 IPv4；IPv6、格式错误的条目会被跳过并记录。
    """
    networks: List[ipaddress.IPv4Network] = []
    skipped: List[str] = []

    for text in cidr_texts:
        try:
            network = ipaddress.ip_network(text, strict=False)
        except (ValueError, TypeError):
            skipped.append(str(text))
            logger.warning("无法解析的 CIDR：%r", text)
            continue
        if network.version != 4:
            skipped.append(str(text))
            continue
        networks.append(network)

    return networks, skipped


def generate_candidates(
    cidrs: Iterable[str],
    count: int,
    exclude_ips: Optional[Set[str]] = None,
    rng: Optional[random.Random] = None,
) -> GenerateResult:
    """从 CIDR 网段中随机抽取指定数量的候选 IPv4。

    参数：
        cidrs：CIDR 文本列表，例如 ["104.16.0.0/13", "172.64.0.0/13"]
        count：想要的 IP 数量（会自动限制在安全范围内）
        exclude_ips：需要排除的 IP 集合（例如当前列表里已有的 IP），实现「自动去重」
        rng：随机数生成器（测试时可以传入固定种子）
    """
    result = GenerateResult(requested_count=count)
    randomizer = rng or random

    # 1) 数量限制
    wanted = max(MIN_GENERATE_COUNT, min(int(count), MAX_GENERATE_COUNT))
    if wanted != count:
        logger.warning("生成数量已调整为 %s（安全范围 %s-%s）", wanted, MIN_GENERATE_COUNT, MAX_GENERATE_COUNT)
    result.requested_count = wanted
    if wanted == 0 or not cidrs:
        return result

    # 2) 解析 CIDR
    networks, skipped = parse_cidrs(cidrs)
    result.skipped_cidrs = skipped
    if not networks:
        logger.warning("所有 CIDR 都无法解析，无法生成 IP")
        return result

    # 3) 计算每个网段的可分配地址数（自动排除网络地址、广播地址等）
    pool_sizes: List[int] = []
    for network in networks:
        usable = network.num_addresses
        # 小网段时保守处理：至少保留 1 个可抽地址
        pool_sizes.append(max(usable, 1))
    total_pool = sum(pool_sizes)

    excluded = set(exclude_ips or ())
    seen: Set[str] = set()
    max_attempts = wanted * MAX_ATTEMPT_MULTIPLIER  # 总尝试上限，防止死循环

    # 4) 随机抽样：按网段大小加权，抽网段 -> 在该网段内随机取偏移量
    attempts = 0
    while len(seen) < wanted and attempts < max_attempts:
        attempts += 1

        # 按地址数加权随机选一个网段（大网段被抽中的概率大，与真实分布一致）
        picked = randomizer.choices(networks, weights=pool_sizes, k=1)[0]

        # 只生成一个随机偏移量（这就是「不遍历整个网段」的关键）
        offset = randomizer.randrange(picked.num_addresses)
        candidate = picked.network_address + offset
        ip_text = str(candidate)

        # 网络地址（如 104.16.0.0）和广播地址不适合当节点，直接跳过
        if offset == 0:
            result.invalid_count += 1
            continue
        if ip_text in seen or ip_text in excluded:
            result.duplicate_count += 1
            continue

        # 复用现有校验逻辑：过滤私有 / 回环 / 非法等
        is_valid, _reason = validate_ip(ip_text)
        if not is_valid:
            result.invalid_count += 1
            continue

        seen.add(ip_text)
        result.ips.append(ip_text)

    if len(seen) < wanted:
        logger.warning(
            "尝试 %s 次后只生成 %s/%s 个 IP（网段总量可能不足）", attempts, len(seen), wanted
        )

    logger.info(
        "候选 IP 生成完成：请求 %s，生成 %s，重复 %s，过滤 %s，跳过网段 %s",
        result.requested_count,
        result.generated_count,
        result.duplicate_count,
        result.invalid_count,
        len(result.skipped_cidrs),
    )
    return result