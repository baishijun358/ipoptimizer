"""core/cidr_generator.py 的单元测试。

运行方式（在项目根目录执行）：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ipaddress
import random
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.cidr_generator import (  # noqa: E402
    MAX_GENERATE_COUNT,
    generate_candidates,
    parse_cidrs,
)


class TestParseCidrs(unittest.TestCase):
    """测试 CIDR 解析。"""

    def test_parse_cloudflare_style_cidr(self) -> None:
        """104.16.0.0/13 必须能够正常解析。"""
        networks, skipped = parse_cidrs(["104.16.0.0/13"])
        self.assertEqual(len(skipped), 0)
        self.assertEqual(len(networks), 1)
        self.assertEqual(networks[0], ipaddress.ip_network("104.16.0.0/13"))
        # /13 包含 2^19 个地址
        self.assertEqual(networks[0].num_addresses, 2 ** 19)

    def test_parse_multiple_cidrs(self) -> None:
        """多个网段一起解析。"""
        networks, skipped = parse_cidrs(["104.16.0.0/13", "172.64.0.0/13", "188.114.96.0/20"])
        self.assertEqual(len(networks), 3)
        self.assertEqual(len(skipped), 0)

    def test_parse_ipv6_is_skipped(self) -> None:
        """IPv6 网段会被跳过（第一阶段只支持 IPv4）。"""
        networks, skipped = parse_cidrs(["2606:4700::/32", "104.16.0.0/13"])
        self.assertEqual(len(networks), 1)
        self.assertEqual(skipped, ["2606:4700::/32"])

    def test_parse_invalid_cidr_is_skipped(self) -> None:
        """非法 CIDR 会被跳过，不影响其他网段。"""
        networks, skipped = parse_cidrs(["not-a-cidr", "999.0.0.0/8", "104.16.0.0/13"])
        self.assertEqual(len(networks), 1)
        self.assertEqual(len(skipped), 2)


class TestGenerateCandidates(unittest.TestCase):
    """测试随机生成候选 IP。"""

    def test_generates_requested_count(self) -> None:
        """能够生成指定数量的 IP。"""
        rng = random.Random(42)
        result = generate_candidates(["104.16.0.0/13", "172.64.0.0/13"], count=100, rng=rng)
        self.assertEqual(result.generated_count, 100)
        self.assertEqual(result.requested_count, 100)

    def test_never_exceeds_requested_count(self) -> None:
        """数量不会超过用户设置的数量。"""
        rng = random.Random(7)
        result = generate_candidates(["104.16.0.0/13"], count=50, rng=rng)
        self.assertLessEqual(result.generated_count, 50)

    def test_deduplication(self) -> None:
        """生成的 IP 不会重复。"""
        rng = random.Random(123)
        result = generate_candidates(["104.16.0.0/13", "172.64.0.0/13"], count=200, rng=rng)
        self.assertEqual(len(result.ips), len(set(result.ips)))

    def test_exclude_ips(self) -> None:
        """exclude_ips 里的 IP 不会被再次生成（与现有列表去重）。"""
        rng = random.Random(99)
        # 用只有 8 个地址的小网段 104.16.0.0/29 做确定性测试：
        # 把全部 7 个可用地址都放进排除集合，抽中任何一个都只能算重复。
        existing = {f"104.16.0.{i}" for i in range(1, 8)}
        result = generate_candidates(["104.16.0.0/29"], count=100, exclude_ips=existing, rng=rng)
        self.assertEqual(len(set(result.ips) & existing), 0)
        self.assertEqual(result.generated_count, 0)
        self.assertGreater(result.duplicate_count, 0)  # 撞上已存在 IP 的尝试被统计为重复

    def test_filters_private_network(self) -> None:
        """私有网段中的 IP 全部被过滤。"""
        rng = random.Random(1)
        result = generate_candidates(["10.0.0.0/8"], count=20, rng=rng)
        self.assertEqual(result.generated_count, 0)
        self.assertGreater(result.invalid_count, 0)

    def test_filters_loopback_and_invalid(self) -> None:
        """回环网段也被过滤（127.0.0.0/8 属于私有/回环）。"""
        rng = random.Random(2)
        result = generate_candidates(["127.0.0.0/8"], count=10, rng=rng)
        self.assertEqual(result.generated_count, 0)

    def test_invalid_cidr_yields_nothing(self) -> None:
        """全部是非法 CIDR 时返回空结果且不崩溃。"""
        result = generate_candidates(["not-a-cidr"], count=10)
        self.assertEqual(result.generated_count, 0)
        self.assertEqual(len(result.skipped_cidrs), 1)

    def test_empty_cidr_list(self) -> None:
        """空网段列表返回空结果且不崩溃。"""
        result = generate_candidates([], count=10)
        self.assertEqual(result.generated_count, 0)

    def test_count_clamped_to_safe_range(self) -> None:
        """超大数量会被限制在安全范围内。"""
        rng = random.Random(3)
        result = generate_candidates(["104.16.0.0/13"], count=MAX_GENERATE_COUNT * 10, rng=rng)
        self.assertEqual(result.requested_count, MAX_GENERATE_COUNT)

    def test_generated_ips_are_valid_public(self) -> None:
        """生成的每个 IP 都是合法的公网 IPv4。"""
        from core.ip_validator import validate_ip

        rng = random.Random(2024)
        result = generate_candidates(["104.16.0.0/13", "172.64.0.0/13"], count=150, rng=rng)
        self.assertGreater(result.generated_count, 0)
        for ip in result.ips:
            is_valid, reason = validate_ip(ip)
            self.assertTrue(is_valid, f"{ip} 应该是合法公网 IPv4（原因：{reason}）")


if __name__ == "__main__":
    unittest.main(verbosity=2)