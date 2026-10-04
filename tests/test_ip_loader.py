"""core/ip_loader.py 的单元测试。

运行方式（在项目根目录执行）：
    python -m unittest discover -s tests -v
也可以单独运行：
    python tests/test_ip_loader.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# 把项目根目录加入模块搜索路径，保证可以直接导入 core 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.ip_loader import (  # noqa: E402  (必须在 sys.path 调整之后再导入)
    DEFAULT_PORT,
    IPEntry,
    IPLoaderError,
    load_from_file,
    parse_address,
    parse_text,
)


class TestParseAddress(unittest.TestCase):
    """测试单行地址解析。"""

    def test_plain_ip(self) -> None:
        """只有 IP 时，端口应为 None（表示使用界面上的端口）。"""
        self.assertEqual(parse_address("104.16.0.1"), ("104.16.0.1", None))

    def test_ip_with_port(self) -> None:
        """支持 IP:端口。"""
        self.assertEqual(parse_address("104.16.0.1:443"), ("104.16.0.1", 443))
        self.assertEqual(parse_address("104.16.0.1:2053"), ("104.16.0.1", 2053))

    def test_ip_with_spaces(self) -> None:
        """首尾空格需要自动去掉。"""
        self.assertEqual(parse_address("  104.16.0.1:443  "), ("104.16.0.1", 443))

    def test_ipv6_with_brackets(self) -> None:
        """[IPv6]:端口 形式可以解析（IPv6 会在校验阶段被过滤）。"""
        self.assertEqual(parse_address("[2606:4700::1]:443"), ("2606:4700::1", 443))

    def test_invalid_port_raises(self) -> None:
        """端口不是数字或超出范围时抛出 ValueError。"""
        with self.assertRaises(ValueError):
            parse_address("104.16.0.1:abc")
        with self.assertRaises(ValueError):
            parse_address("104.16.0.1:70000")
        with self.assertRaises(ValueError):
            parse_address("104.16.0.1:")


class TestParseText(unittest.TestCase):
    """测试多行文本解析。"""

    def test_read_plain_lines(self) -> None:
        """正常的 IP 列表可以被读取。"""
        result = parse_text("104.16.0.1\n104.16.0.2\n104.16.0.3")
        self.assertEqual(result.total_lines, 3)
        self.assertEqual(result.entry_count, 3)
        self.assertEqual([entry.ip for entry in result.entries],
                         ["104.16.0.1", "104.16.0.2", "104.16.0.3"])

    def test_deduplicate(self) -> None:
        """重复的 IP 只保留一条，并统计重复数量。"""
        result = parse_text("104.16.0.1\n104.16.0.1\n104.16.0.2")
        self.assertEqual(result.total_lines, 3)
        self.assertEqual(result.entry_count, 2)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual([entry.ip for entry in result.entries], ["104.16.0.1", "104.16.0.2"])

    def test_skip_blank_lines(self) -> None:
        """空行和只有空格的行不计入读取数量。"""
        result = parse_text("104.16.0.1\n\n   \n104.16.0.2\n")
        self.assertEqual(result.total_lines, 2)
        self.assertEqual(result.entry_count, 2)
        self.assertEqual(result.duplicate_count, 0)
        self.assertEqual(result.format_error_count, 0)

    def test_skip_comment_lines(self) -> None:
        """以 # // ; 开头的注释行会被忽略。"""
        result = parse_text("# 这是注释\n104.16.0.1\n// 注释\n; 注释\n104.16.0.2  # 行内注释")
        self.assertEqual(result.total_lines, 2)
        self.assertEqual([entry.ip for entry in result.entries], ["104.16.0.1", "104.16.0.2"])

    def test_ip_with_port_lines(self) -> None:
        """IP:端口 形式可以提取端口。"""
        result = parse_text("104.16.0.1:443\n104.16.0.2:2053")
        self.assertEqual(result.entries[0].port, 443)
        self.assertEqual(result.entries[1].port, 2053)
        self.assertEqual(result.entries[0].text, "104.16.0.1:443")

    def test_same_ip_different_port_kept(self) -> None:
        """同一个 IP 配不同端口视为两条不同的测试目标。"""
        result = parse_text("104.16.0.1:443\n104.16.0.1:2053")
        self.assertEqual(result.entry_count, 2)
        self.assertEqual(result.duplicate_count, 0)

    def test_invalid_port_counted_as_format_error(self) -> None:
        """端口不合法时记录为格式错误，不影响其他行。"""
        result = parse_text("104.16.0.1:abc\n104.16.0.2")
        self.assertEqual(result.total_lines, 2)
        self.assertEqual(result.entry_count, 1)
        self.assertEqual(result.format_error_count, 1)
        self.assertEqual(result.format_errors[0].line_number, 1)
        self.assertIn("端口", result.format_errors[0].reason)

    def test_invalid_ip_text_is_kept_for_validator(self) -> None:
        """非法 IP 文本由校验模块负责过滤，读取阶段只做解析。"""
        result = parse_text("hello\n999.999.999.999")
        self.assertEqual(result.entry_count, 2)
        self.assertEqual(result.format_error_count, 0)

    def test_empty_text(self) -> None:
        """空文本不会报错。"""
        result = parse_text("")
        self.assertEqual(result.total_lines, 0)
        self.assertEqual(result.entry_count, 0)

    def test_default_port_constant(self) -> None:
        """默认端口常量为 443。"""
        self.assertEqual(DEFAULT_PORT, 443)
        self.assertEqual(IPEntry("104.16.0.1").text, "104.16.0.1")


class TestLoadFromFile(unittest.TestCase):
    """测试文件导入。"""

    def test_load_utf8_file(self) -> None:
        """UTF-8 文件可以正常读取。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "ips.txt"
            file_path.write_text("104.16.0.1\n104.16.0.2:443\n104.16.0.2:443\n", encoding="utf-8")

            result = load_from_file(file_path)
            self.assertEqual(result.total_lines, 3)
            self.assertEqual(result.entry_count, 2)
            self.assertEqual(result.duplicate_count, 1)

    def test_load_gbk_file(self) -> None:
        """GBK 编码的文件也能读取（会自动尝试多种编码）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "ips_gbk.txt"
            file_path.write_bytes("104.16.0.1\n# 中文注释\n104.16.0.2\n".encode("gbk"))

            result = load_from_file(file_path)
            self.assertEqual(result.entry_count, 2)

    def test_load_utf8_bom_file(self) -> None:
        """带 BOM 的 UTF-8 文件不会把 BOM 当成 IP 内容。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "ips_bom.txt"
            file_path.write_bytes("\ufeff104.16.0.1\n104.16.0.2\n".encode("utf-8"))

            result = load_from_file(file_path)
            self.assertEqual(result.entry_count, 2)
            self.assertEqual(result.entries[0].ip, "104.16.0.1")

    def test_missing_file_raises_loader_error(self) -> None:
        """文件不存在时抛出 IPLoaderError，并给出中文提示。"""
        missing = Path(tempfile.gettempdir()) / "not_exists_ips_file.txt"
        with self.assertRaises(IPLoaderError) as context:
            load_from_file(missing)
        self.assertIn("不存在", str(context.exception))

    def test_directory_raises_loader_error(self) -> None:
        """传入目录时抛出 IPLoaderError。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(IPLoaderError):
                load_from_file(Path(temp_dir))


if __name__ == "__main__":
    unittest.main(verbosity=2)