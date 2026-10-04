"""core/http_tester.py 以及三级测试结果合并逻辑的单元测试（V1.2 新增）。

运行方式（在项目根目录执行）：
    python -m unittest discover -s tests -v
也可以单独运行：
    python tests/test_http_tester.py

测试策略：
1. 纯函数（端口判断、状态行解析、速度格式化）直接断言；
2. HTTP / 下载测试用「本机临时 HTTP 服务器」验证，
   不依赖外部网络，所以测试稳定、可在离线环境运行。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

# 把项目根目录加入模块搜索路径，保证可以直接导入 core 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.http_tester import (  # noqa: E402
    DEFAULT_HTTP_HOST,
    ERROR_BAD_STATUS,
    ERROR_NO_RESPONSE,
    ERROR_TIMEOUT,
    HTTPS_PORTS,
    DownloadResult,
    HttpResult,
    download_test,
    format_speed,
    http_test,
    is_https_port,
)
from core.scanner import (  # noqa: E402
    DEFAULT_DOWNLOAD_BYTES,
    DEFAULT_DOWNLOAD_TIMEOUT_MS,
    DEFAULT_HTTP_TIMEOUT_MS,
    Scanner,
    ScanSummary,
    StageStats,
    sort_results,
)
from core.tcp_tester import TestResult, attach_stage_results  # noqa: E402

# ----------------------------------------------------------------------
# 本机临时 HTTP 服务器：用于验证 HTTP 测试和下载测速
# ----------------------------------------------------------------------
FAKE_BODY_BYTES = 64 * 1024          # 假服务器每次返回的响应体大小
DELAY_SECONDS = 2.0                  # 用于测试超时


async def _handle_fake_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """极简 HTTP 响应器，支持三种行为（由请求中的标记决定）：

    1. 请求里带 status=404 -> 返回 404（用于测试“状态码异常”）
    2. 请求里带 slow=1 -> 先等待 DELAY_SECONDS 再返回（用于测试超时）
    3. 其他情况 -> 返回 200 + FAKE_BODY_BYTES 字节数据（正常下载）

    标记可以写在请求行（路径）或 Host 头里：
    测试通过 host="127.0.0.1/status=404" 注入标记，生产代码会把它放进 Host 头。
    """
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)

        # 读完请求头，直到空行（行为标记写在 Host 头里，所以要连头一起检查）
        header_lines: list[bytes] = []
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if line in (b"\r\n", b"\n", b""):
                break
            header_lines.append(line)

        # 请求行 + 全部响应头拼在一起找标记，兼容标记写在路径或 Host 头两种情况
        marker_text = request_line.decode("latin-1", errors="replace") + "".join(
            line.decode("latin-1", errors="replace") for line in header_lines
        )

        if "slow=1" in marker_text:
            # 故意拖慢响应，用于测试客户端超时
            await asyncio.sleep(DELAY_SECONDS)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        elif "status=404" in marker_text:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        else:
            header = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Length: {FAKE_BODY_BYTES}\r\n"
                "Content-Type: application/octet-stream\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("latin-1")
            writer.write(header)
            writer.write(b"\x00" * FAKE_BODY_BYTES)

        await writer.drain()
    except Exception:
        pass  # 客户端提前断开时忽略
    finally:
        try:
            writer.close()
        except Exception:
            pass


class LocalHttpServerMixin:
    """启动 / 关闭本机 HTTP 服务器的测试基类。"""

    loop: asyncio.AbstractEventLoop
    server: asyncio.AbstractServer
    port: int

    def setUp(self) -> None:  # noqa: N802 (unittest 规定的命名)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.server = self.loop.run_until_complete(
            asyncio.start_server(_handle_fake_client, host="127.0.0.1", port=0)
        )
        # port=0 表示让系统自动分配空闲端口，避免端口冲突
        self.port = self.server.sockets[0].getsockname()[1]

    def tearDown(self) -> None:  # noqa: N802
        self.server.close()
        self.loop.run_until_complete(self.server.wait_closed())
        self.loop.close()

    def run_async(self, coro):
        """在测试用的独立事件循环里运行协程。"""
        return self.loop.run_until_complete(coro)


# 单元测试里用不到 ssl（本机服务器是明文 HTTP），
# 但要注意：如果随机端口刚好落在 HTTPS_PORTS 里，连接会按 HTTPS 处理。
class TestPortAndParsingHelpers(unittest.TestCase):
    """纯函数测试：端口判断、状态行解析、速度格式化。"""

    def test_is_https_port(self) -> None:
        """Cloudflare 的 HTTPS 端口集合判断正确。"""
        for port in (443, 2053, 2083, 2087, 2096, 8443):
            self.assertTrue(is_https_port(port), f"{port} 应该走 HTTPS")
        for port in (80, 8080, 8880, 2052, 2082, 2086, 2095):
            self.assertFalse(is_https_port(port), f"{port} 应该走 HTTP")

    def test_https_ports_constant(self) -> None:
        """HTTPS 端口集合的内容与 Cloudflare 官方一致。"""
        self.assertEqual(HTTPS_PORTS, {443, 2053, 2083, 2087, 2096, 8443})

    def test_default_host(self) -> None:
        """默认主机名使用 Cloudflare 官方测速域名（TLS SNI 需要它）。"""
        self.assertEqual(DEFAULT_HTTP_HOST, "speed.cloudflare.com")

    def test_format_speed(self) -> None:
        """速度格式化：MB/s、KB/s、未测三种情况。"""
        self.assertEqual(format_speed(None), "--")
        self.assertEqual(format_speed(2 * 1024 * 1024), "2.0 MB/s")
        self.assertEqual(format_speed(1024 * 1024), "1.0 MB/s")
        self.assertEqual(format_speed(512 * 1024), "512 KB/s")
        self.assertEqual(format_speed(0), "0 KB/s")


class TestHttpTest(LocalHttpServerMixin, unittest.TestCase):
    """用本机服务器验证 HTTP 连通性测试。"""

    def test_http_success(self) -> None:
        """正常返回 200 时，应该成功并带有状态码和耗时。"""
        result = self.run_async(http_test("127.0.0.1", self.port, timeout_ms=3000))
        self.assertTrue(result.success, f"应该成功，实际错误：{result.error}")
        self.assertEqual(result.status, 200)
        self.assertIsNotNone(result.latency)
        self.assertGreaterEqual(result.latency, 0)
        self.assertIsNone(result.error)

    def test_http_bad_status(self) -> None:
        """返回 404 时，HTTP 测试应该失败并记录状态码。"""
        result = self.run_async(
            http_test("127.0.0.1", self.port, timeout_ms=3000, host="127.0.0.1/status=404")
        )
        self.assertFalse(result.success)
        self.assertEqual(result.status, 404)
        self.assertIn(ERROR_BAD_STATUS, result.error or "")

    def test_http_timeout(self) -> None:
        """服务器迟迟不响应时，应该返回 timeout。"""
        result = self.run_async(
            http_test("127.0.0.1", self.port, timeout_ms=300, host="127.0.0.1/slow=1")
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error, ERROR_TIMEOUT)
        self.assertIsNone(result.latency)

    def test_http_connection_refused(self) -> None:
        """端口没人监听时（连接被拒绝）也要安全返回，不能抛异常。"""
        # 先关掉服务器，制造“连接被拒绝”
        self.server.close()
        self.run_async(self.server.wait_closed())
        result = self.run_async(http_test("127.0.0.1", self.port, timeout_ms=1000))
        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        self.assertNotIn("未知错误", result.error or "")

    def test_http_result_dataclass_defaults(self) -> None:
        """HttpResult 的默认值符合预期。"""
        result = HttpResult()
        self.assertFalse(result.success)
        self.assertIsNone(result.status)
        self.assertIsNone(result.latency)
        self.assertIsNone(result.error)
        self.assertFalse(result.https)


class TestDownloadTest(LocalHttpServerMixin, unittest.TestCase):
    """用本机服务器验证下载测速。"""

    def test_download_success(self) -> None:
        """下载到完整数据时，应该成功并给出速度。"""
        result = self.run_async(
            download_test("127.0.0.1", self.port, download_bytes=FAKE_BODY_BYTES, timeout_ms=5000)
        )
        self.assertTrue(result.success, f"应该成功，实际错误：{result.error}")
        self.assertEqual(result.bytes_received, FAKE_BODY_BYTES)
        self.assertIsNotNone(result.speed_bps)
        self.assertGreater(result.speed_bps or 0, 0)
        self.assertIsNotNone(result.elapsed_ms)
        self.assertGreaterEqual(result.elapsed_ms or 0, 1)

    def test_download_partial_bytes(self) -> None:
        """请求量小于服务器返回量时，只统计请求的字节数（不会多读）。"""
        result = self.run_async(
            download_test("127.0.0.1", self.port, download_bytes=1024, timeout_ms=5000)
        )
        self.assertTrue(result.success, f"应该成功，实际错误：{result.error}")
        self.assertEqual(result.bytes_received, 1024)

    def test_download_bytes_lower_bound(self) -> None:
        """下载量被限制在合法范围内（至少 1 字节）。"""
        result = self.run_async(
            download_test("127.0.0.1", self.port, download_bytes=0, timeout_ms=5000)
        )
        self.assertTrue(result.success, f"应该成功，实际错误：{result.error}")
        self.assertEqual(result.bytes_received, 1)

    def test_download_bad_status(self) -> None:
        """HTTP 状态异常时，下载应该失败。"""
        result = self.run_async(
            download_test(
                "127.0.0.1",
                self.port,
                download_bytes=1024,
                timeout_ms=5000,
                host="127.0.0.1/status=404",
            )
        )
        self.assertFalse(result.success)
        self.assertIn(ERROR_BAD_STATUS, result.error or "")
        self.assertEqual(result.bytes_received, 0)

    def test_download_timeout(self) -> None:
        """响应太慢时，下载应该返回 timeout。"""
        result = self.run_async(
            download_test(
                "127.0.0.1",
                self.port,
                download_bytes=FAKE_BODY_BYTES,
                timeout_ms=300,
                host="127.0.0.1/slow=1",
            )
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error, ERROR_TIMEOUT)

    def test_download_result_dataclass_defaults(self) -> None:
        """DownloadResult 的默认值符合预期。"""
        result = DownloadResult()
        self.assertFalse(result.success)
        self.assertEqual(result.bytes_received, 0)
        self.assertIsNone(result.speed_bps)
        self.assertIsNone(result.elapsed_ms)
        self.assertIsNone(result.error)


class TestAttachStageResults(unittest.TestCase):
    """三级测试结果合并逻辑（V1.2 的核心数据流）。"""

    @staticmethod
    def _tcp_ok() -> TestResult:
        return TestResult(ip="104.16.0.1", port=443, latency=42, success=True, error=None)

    def test_tcp_only_keeps_old_behaviour(self) -> None:
        """只传 TCP 结果时，返回原对象（第一阶段行为完全不变）。"""
        tcp = self._tcp_ok()
        merged = attach_stage_results(tcp)
        self.assertIs(merged, tcp)
        self.assertFalse(merged.http_tested)
        self.assertFalse(merged.download_tested)

    def test_attach_http_only(self) -> None:
        """只合并 HTTP 结果：下载阶段保持「未测试」。"""
        http = HttpResult(success=True, status=200, latency=88, error=None, https=True)
        merged = attach_stage_results(self._tcp_ok(), http)

        self.assertTrue(merged.http_tested)
        self.assertTrue(merged.http_success)
        self.assertEqual(merged.http_status, 200)
        self.assertEqual(merged.http_latency, 88)
        self.assertIsNone(merged.http_error)

        self.assertFalse(merged.download_tested)
        self.assertFalse(merged.download_success)
        self.assertIsNone(merged.download_speed_bps)

    def test_attach_all_stages(self) -> None:
        """三级结果全部合并。"""
        http = HttpResult(success=True, status=200, latency=88, error=None, https=True)
        download = DownloadResult(
            success=True,
            bytes_received=1024 * 1024,
            speed_bps=8 * 1024 * 1024,
            elapsed_ms=125,
            error=None,
        )
        merged = attach_stage_results(self._tcp_ok(), http, download)

        self.assertTrue(merged.success)          # TCP 结果保留
        self.assertEqual(merged.latency, 42)
        self.assertTrue(merged.http_success)
        self.assertTrue(merged.download_tested)
        self.assertTrue(merged.download_success)
        self.assertEqual(merged.download_bytes, 1024 * 1024)
        self.assertAlmostEqual(merged.download_speed_bps or 0, 8 * 1024 * 1024)
        self.assertEqual(merged.download_elapsed_ms, 125)

    def test_attach_http_failure(self) -> None:
        """HTTP 失败时，下载阶段不会执行（结果为未测试）。"""
        http = HttpResult(success=False, status=None, latency=None,
                          error=ERROR_TIMEOUT, https=True)
        merged = attach_stage_results(self._tcp_ok(), http)

        self.assertTrue(merged.http_tested)
        self.assertFalse(merged.http_success)
        self.assertEqual(merged.http_error, ERROR_TIMEOUT)
        self.assertFalse(merged.download_tested)

    def test_as_dict_contains_all_stages(self) -> None:
        """导出字典包含三级测试的全部字段（为后续阶段导出做准备）。"""
        http = HttpResult(success=True, status=200, latency=88)
        download = DownloadResult(success=True, bytes_received=2048, speed_bps=4096.0,
                                  elapsed_ms=500)
        data = attach_stage_results(self._tcp_ok(), http, download).as_dict()

        expected_keys = {
            "ip", "port", "latency", "success", "error",
            "http_tested", "http_success", "http_status", "http_latency", "http_error",
            "download_tested", "download_success", "download_speed_bps",
            "download_bytes", "download_elapsed_ms", "download_error",
        }
        self.assertEqual(set(data.keys()), expected_keys)


class TestScannerDefaultsAndStats(unittest.TestCase):
    """并发扫描器的默认参数与各阶段统计（不需要真实网络）。"""

    def test_defaults_unchanged(self) -> None:
        """默认值：TCP 的行为与第一阶段保持一致，HTTP/下载默认开启。"""
        scanner = Scanner()
        self.assertEqual(scanner.port, 443)
        self.assertEqual(scanner.concurrency, 50)
        self.assertEqual(scanner.timeout_ms, 1000)         # TCP 超时仍是 1000ms
        self.assertTrue(scanner.http_enabled)
        self.assertEqual(scanner.http_timeout_ms, DEFAULT_HTTP_TIMEOUT_MS)
        self.assertTrue(scanner.download_enabled)
        self.assertEqual(scanner.download_bytes, DEFAULT_DOWNLOAD_BYTES)
        self.assertEqual(scanner.download_timeout_ms, DEFAULT_DOWNLOAD_TIMEOUT_MS)

    def test_stage_switches(self) -> None:
        """可以按需关闭 HTTP / 下载阶段（向后兼容第一阶段用法）。"""
        scanner = Scanner(http_enabled=False, download_enabled=False)
        self.assertFalse(scanner.http_enabled)
        self.assertFalse(scanner.download_enabled)

    def test_range_protection(self) -> None:
        """并发数、超时、下载量都会被限制在安全范围内。"""
        scanner = Scanner(
            concurrency=99999,
            timeout_ms=1,
            http_timeout_ms=999999,
            download_bytes=999 * 1024 * 1024,
            download_timeout_ms=999999,
        )
        self.assertEqual(scanner.concurrency, 500)
        self.assertEqual(scanner.timeout_ms, 100)
        self.assertEqual(scanner.http_timeout_ms, 30000)
        self.assertEqual(scanner.download_bytes, 20 * 1024 * 1024)
        self.assertEqual(scanner.download_timeout_ms, 120000)

        low = Scanner(concurrency=0, timeout_ms=1, http_timeout_ms=1,
                      download_bytes=0, download_timeout_ms=1)
        self.assertEqual(low.concurrency, 1)
        self.assertEqual(low.timeout_ms, 100)
        self.assertEqual(low.http_timeout_ms, 500)
        self.assertEqual(low.download_bytes, 64 * 1024)
        self.assertEqual(low.download_timeout_ms, 1000)

    def test_scan_summary_new_fields_default_zero(self) -> None:
        """ScanSummary 新增字段默认是 0，旧代码（只读 total/success/failed）不受影响。"""
        summary = ScanSummary(total=10)
        self.assertEqual(summary.tested, 0)
        self.assertEqual(summary.success, 0)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.http_tested, 0)
        self.assertEqual(summary.http_success, 0)
        self.assertEqual(summary.http_failed, 0)
        self.assertEqual(summary.download_tested, 0)
        self.assertEqual(summary.download_success, 0)
        self.assertEqual(summary.download_failed, 0)

    def test_stage_stats_from_summary(self) -> None:
        """StageStats 快照与汇总信息一致（界面显示各阶段成功/失败）。"""
        summary = ScanSummary(
            total=100, tested=100, success=60, failed=40,
            http_tested=60, http_success=50, http_failed=10,
            download_tested=50, download_success=45, download_failed=5,
        )
        stats = StageStats.from_summary(summary)

        self.assertEqual(stats.total, 100)
        self.assertEqual(stats.tcp_tested, 100)
        self.assertEqual(stats.tcp_success, 60)
        self.assertEqual(stats.tcp_failed, 40)
        self.assertEqual(stats.http_tested, 60)
        self.assertEqual(stats.http_success, 50)
        self.assertEqual(stats.http_failed, 10)
        self.assertEqual(stats.download_tested, 50)
        self.assertEqual(stats.download_success, 45)
        self.assertEqual(stats.download_failed, 5)

    def test_stage_stats_is_frozen(self) -> None:
        """StageStats 不可变，跨线程传递时不会被意外修改。"""
        stats = StageStats(total=1)
        with self.assertRaises(Exception):
            stats.total = 2  # type: ignore[misc]

    def test_sort_results_ignores_new_fields(self) -> None:
        """排序逻辑不变：TCP 成功的按延迟升序在前，失败的在后。"""
        results = [
            TestResult(ip="1.1.1.1", port=443, latency=None, success=False, error="timeout"),
            TestResult(ip="1.1.1.2", port=443, latency=50, success=True,
                       http_tested=True, http_success=True, http_status=200, http_latency=80,
                       download_tested=True, download_success=True, download_speed_bps=1024.0),
            TestResult(ip="1.1.1.3", port=443, latency=20, success=True),
        ]
        sorted_results = sort_results(results)
        self.assertEqual([item.ip for item in sorted_results], ["1.1.1.3", "1.1.1.2", "1.1.1.1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)