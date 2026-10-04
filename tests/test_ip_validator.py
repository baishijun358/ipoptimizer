"""core/ip_validator.py 的单元测试。

运行方式（在项目根目录执行）：
    python -m unittest discover -s tests -v
也可以单独运行：
    python tests/test_ip_validator.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# 把项目根目录加入模块搜索路径，保证可以直接导入 core 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.ip_loader import IPEntry, parse_text  # noqa: E402
from core.ip_validator import (  # noqa: E402
    REASON_FORMAT,
    REASON_IPV6,
    REASON_LOOPBACK,
    REASON_PRIVATE,
    import_from_text,
    validate_entries,
    validate_ip,
    validate_port,
)


class TestValidateIp(unittest.TestCase):
    """测试单个 IP 的校验规则。"""

    def test_valid_public_ipv4(self) -> None:
        """正常的公网 IPv4 应该通过校验。"""
        for ip in ("104.16.0.1", "1.1.1.1", "8.8.8.8", "172.64.155.209"):
            is_valid, reason = validate_ip(ip)
            self.assertTrue(is_valid, f"{ip} 应该有效，实际原因：{reason}")
            self.assertIsNone(reason)

    def test_invalid_ipv4_format(self) -> None:
        """非法格式（含乱码、越界数字）应该被过滤。"""
        for text in ("999.999.999.999", "hello", "104.16.0", "104.16.0.1.5", "1.2.3.4a"):
            is_valid, reason = validate_ip(text)
            self.assertFalse(is_valid, f"{text} 应该无效")
            self.assertEqual(reason, REASON_FORMAT)

    def test_empty_ip(self) -> None:
        """空 IP 应该被过滤。"""
        is_valid, reason = validate_ip("")
        self.assertFalse(is_valid)
        self.assertIsNotNone(reason)
        is_valid, _ = validate_ip("   ")
        self.assertFalse(is_valid)

    def test_private_ipv4(self) -> None:
        """私有地址应该被过滤。"""
        for ip in ("192.168.1.1", "10.0.0.1", "172.16.0.1"):
            is_valid, reason = validate_ip(ip)
            self.assertFalse(is_valid, f"{ip} 应该无效")
            self.assertEqual(reason, REASON_PRIVATE)

    def test_loopback_ipv4(self) -> None:
        """回环地址应该被过滤。"""
        is_valid, reason = validate_ip("127.0.0.1")
        self.assertFalse(is_valid)
        self.assertEqual(reason, REASON_LOOPBACK)

    def test_link_local_ipv4(self) -> None:
        """链路本地地址（169.254.x.x）应该被过滤。"""
        is_valid, reason = validate_ip("169.254.1.1")
        self.assertFalse(is_valid)
        self.assertIsNotNone(reason)

    def test_multicast_and_unspecified(self) -> None:
        """组播地址、0.0.0.0 应该被过滤。"""
        self.assertFalse(validate_ip("224.0.0.1")[0])
        self.assertFalse(validate_ip("0.0.0.0")[0])

    def test_ipv6_is_filtered(self) -> None:
        """第一阶段只支持 IPv4，IPv6 应该被过滤。"""
        for ip in ("2606:4700::1", "::1", "fe80::1", "2001:db8::1"):
            is_valid, reason = validate_ip(ip)
            self.assertFalse(is_valid, f"{ip} 应该无效")
            self.assertEqual(reason, REASON_IPV6)

    def test_reserved_ipv4(self) -> None:
        """保留地址应该被过滤。"""
        is_valid, reason = validate_ip("240.0.0.1")
        self.assertFalse(is_valid)
        self.assertIsNotNone(reason)


class TestValidatePort(unittest.TestCase):
    """测试端口校验。"""

    def test_port_none_is_allowed(self) -> None:
        """没有写端口时，表示使用界面上的端口，是允许的。"""
        self.assertEqual(validate_port(None), (True, None))

    def test_valid_port(self) -> None:
        self.assertEqual(validate_port(443), (True, None))
        self.assertEqual(validate_port(65535), (True, None))

    def test_invalid_port(self) -> None:
        self.assertFalse(validate_port(0)[0])
        self.assertFalse(validate_port(65536)[0])


class TestValidateEntries(unittest.TestCase):
    """测试批量校验。"""

    def test_batch_validation(self) -> None:
        """混合列表：只保留可用的公网 IPv4。"""
        entries = [
            IPEntry("hello"),
            IPEntry("999.999.999.999"),
            IPEntry("192.168.1.1"),
            IPEntry("127.0.0.1"),
            IPEntry("104.16.0.1"),
            IPEntry("104.16.0.2", 443),
        ]
        result = validate_entries(entries)

        self.assertEqual(result.valid_count, 2)
        self.assertEqual(result.invalid_count, 4)
        self.assertEqual([item.ip for item in result.valid], ["104.16.0.1", "104.16.0.2"])

    def test_empty_entries(self) -> None:
        """空列表不会报错。"""
        result = validate_entries([])
        self.assertEqual(result.valid_count, 0)
        self.assertEqual(result.invalid_count, 0)


class TestImportFromText(unittest.TestCase):
    """测试“读取 + 校验”的完整流程和统计数字。"""

    def test_summary_counts(self) -> None:
        """读取数量 = 有效 IP + 重复数量 + 无效 IP。"""
        text = (
            "hello\n"
            "999.999.999.999\n"
            "192.168.1.1\n"
            "127.0.0.1\n"
            "104.16.0.1\n"
            "104.16.0.1\n"      # 重复
            "104.16.0.2:443\n"
            "\n"                # 空行（不计入）
        )
        outcome = import_from_text(text)
        summary = outcome.summary

        self.assertEqual(summary.total_lines, 7)
        self.assertEqual(summary.duplicate_count, 1)
        self.assertEqual(summary.unique_count, 6)
        self.assertEqual(summary.valid_count, 2)
        self.assertEqual(summary.invalid_count, 4)
        self.assertEqual(
            summary.total_lines,
            summary.valid_count + summary.duplicate_count + summary.invalid_count,
        )

    def test_valid_entries_can_be_used_for_scan(self) -> None:
        """有效 IP 保留用户写的端口。"""
        outcome = import_from_text("104.16.0.1:2053\n104.16.0.2")
        self.assertEqual(outcome.valid_entries[0].port, 2053)
        self.assertIsNone(outcome.valid_entries[1].port)

    def test_summary_text_contains_chinese_labels(self) -> None:
        """统计摘要包含中文标签，方便界面直接显示。"""
        outcome = import_from_text("104.16.0.1")
        text = outcome.summary.to_text()
        self.assertIn("读取数量", text)
        self.assertIn("有效 IP", text)
        self.assertIn("无效 IP", text)

    def test_parse_text_result_reused(self) -> None:
        """导入结果里保留了原始读取信息。"""
        outcome = import_from_text("104.16.0.1\n104.16.0.1")
        self.assertEqual(outcome.load_result.entry_count, 1)
        self.assertEqual(parse_text("104.16.0.1").entry_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)