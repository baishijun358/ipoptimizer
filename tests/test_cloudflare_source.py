"""core/cloudflare_source.py 的单元测试。

不依赖真实网络：用 mock 模拟 Cloudflare API 的各种返回情况。
运行方式（在项目根目录执行）：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.cloudflare_source as cfs  # noqa: E402
from core.cloudflare_source import (  # noqa: E402
    CATEGORY_TIMEOUT,
    CloudflareError,
    http_error_message,
    test_cloudflare_connection,
)


def _fake_response(payload, status: int = 200):
    """构造一个模拟的 HTTP 响应对象（带 status 和 read()）。"""
    response = io.BytesIO(json.dumps(payload).encode("utf-8"))
    response.status = status
    return response


def _good_payload() -> dict:
    """正常的 Cloudflare API 返回。"""
    return {
        "success": True,
        "result": {
            "ipv4_cidrs": ["104.16.0.0/13", "172.64.0.0/13", "188.114.96.0/20"],
            "ipv6_cidrs": ["2606:4700::/32"],
        },
    }


def _patch_urlopen(**kwargs):
    """同时 mock 主源和备用源（备用源是纯文本，用不同的 fake 函数区分）。"""
    return mock.patch.object(cfs.urllib.request, "urlopen", **kwargs)


class TestFetchIPv4Cidrs(unittest.TestCase):
    """测试正常与异常情况（主源与备用源同时 mock，避免测试触网）。"""

    def test_normal_response(self) -> None:
        """正常 API 返回：解析出 IPv4 网段。"""
        with _patch_urlopen(return_value=_fake_response(_good_payload())):
            result = cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertEqual(result.cidrs, ["104.16.0.0/13", "172.64.0.0/13", "188.114.96.0/20"])
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.source, "api")
        self.assertGreaterEqual(result.elapsed_seconds, 0.0)

    def test_http_error(self) -> None:
        """HTTP 500 错误：抛出 CloudflareError，信息包含 HTTP 状态码。"""
        error = urllib.error.HTTPError(cfs.API_URL, 500, "Server Error", None, None)
        with _patch_urlopen(side_effect=error):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertIn("HTTP", str(context.exception))
        self.assertIn("500", str(context.exception))

    def test_timeout(self) -> None:
        """网络超时：抛出 CloudflareError，分类为 timeout。"""
        error = urllib.error.URLError(TimeoutError("timed out"))
        with _patch_urlopen(side_effect=error):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertEqual(str(context.exception), cfs.ERROR_TIMEOUT)
        self.assertEqual(context.exception.category, CATEGORY_TIMEOUT)
        self.assertIn("timed out", context.exception.detail)

    def test_network_unreachable(self) -> None:
        """没有网络（DNS 解析失败）：提示网络连接错误，detail 含真实原因。"""
        error = urllib.error.URLError("Name or service not known")
        with _patch_urlopen(side_effect=error):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        # 消息以固定文案开头（可能附带系统代理提示），真实原因在 detail 里
        self.assertTrue(str(context.exception).startswith(cfs.ERROR_NETWORK))
        self.assertEqual(context.exception.category, cfs.CATEGORY_NETWORK)
        self.assertIn("Name or service", context.exception.detail)

    def test_connection_refused_mentions_proxy(self) -> None:
        """连接被拒绝（典型场景：本地代理没开）：中文提示包含代理建议。"""
        error = urllib.error.URLError(ConnectionRefusedError())
        with _patch_urlopen(side_effect=error), mock.patch.object(
            cfs, "_proxy_hint", return_value="（本机配置了系统代理 127.0.0.1:1819：请确认代理软件已启动）"
        ):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertIn("代理", str(context.exception))
        self.assertIn("ConnectionRefusedError", context.exception.detail)

    def test_bad_json(self) -> None:
        """返回内容不是 JSON：中文错误提示。"""
        response = io.BytesIO(b"<html>not json</html>")
        response.status = 200
        with _patch_urlopen(return_value=response):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertEqual(str(context.exception), cfs.ERROR_BAD_JSON)
        self.assertEqual(context.exception.category, cfs.CATEGORY_JSON)

    def test_missing_ipv4_cidrs(self) -> None:
        """返回 JSON 里缺少 ipv4_cidrs：中文错误提示。"""
        payload = {"success": True, "result": {"ipv6_cidrs": ["2606:4700::/32"]}}
        with _patch_urlopen(return_value=_fake_response(payload)):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertEqual(str(context.exception), cfs.ERROR_NO_IPV4)

    def test_empty_ipv4_cidrs(self) -> None:
        """ipv4_cidrs 为空列表：同样提示没有有效网段。"""
        payload = {"success": True, "result": {"ipv4_cidrs": []}}
        with _patch_urlopen(return_value=_fake_response(payload)):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertEqual(str(context.exception), cfs.ERROR_NO_IPV4)

    def test_success_false(self) -> None:
        """API 返回 success=false：中文错误提示。"""
        payload = {"success": False, "errors": [{"code": 1000}]}
        with _patch_urlopen(return_value=_fake_response(payload)):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertEqual(str(context.exception), cfs.ERROR_API_FAILED)
        self.assertEqual(context.exception.category, cfs.CATEGORY_API)

    def test_missing_result(self) -> None:
        """缺少 result 字段：中文错误提示。"""
        payload = {"success": True}
        with _patch_urlopen(return_value=_fake_response(payload)):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertIn("result", str(context.exception))

    def test_non_200_status(self) -> None:
        """状态码不是 200：提示 HTTP 错误。"""
        response = io.BytesIO(b"{}")
        response.status = 503
        with _patch_urlopen(return_value=response):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertIn("503", str(context.exception))

    def test_malformed_cidr_entries_skipped(self) -> None:
        """个别 CIDR 格式错误会被跳过，不影响整体。"""
        payload = {
            "success": True,
            "result": {"ipv4_cidrs": ["104.16.0.0/13", "###bad###", "172.64.0.0/13"]},
        }
        with _patch_urlopen(return_value=_fake_response(payload)):
            result = cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertEqual(result.cidrs, ["104.16.0.0/13", "172.64.0.0/13"])

    def test_fallback_source_used_when_api_fails(self) -> None:
        """主源失败时自动尝试备用源（官方纯文本列表）。"""
        def fake_urlopen(request, timeout=None):
            url = getattr(request, "full_url", str(request))
            if "api.cloudflare.com" in url:
                raise urllib.error.URLError("primary down")
            # 备用源返回纯文本
            response = io.BytesIO(b"104.16.0.0/13\n172.64.0.0/13\n")
            response.status = 200
            return response

        with _patch_urlopen(side_effect=fake_urlopen):
            result = cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertEqual(result.cidrs, ["104.16.0.0/13", "172.64.0.0/13"])
        self.assertEqual(result.source, "text")

    def test_both_sources_fail_raises_primary_error(self) -> None:
        """主源与备用源都失败：抛出主源的 CloudflareError。"""
        error = urllib.error.URLError("totally offline")
        with _patch_urlopen(side_effect=error):
            with self.assertRaises(CloudflareError) as context:
                cfs.fetch_ipv4_cidrs(timeout_seconds=1)
        self.assertEqual(context.exception.category, cfs.CATEGORY_NETWORK)


class TestTestConnection(unittest.TestCase):
    """测试 test_cloudflare_connection()。"""

    def test_success(self) -> None:
        """连接成功：返回 HTTP 状态码和 CIDR 数量。"""
        with _patch_urlopen(return_value=_fake_response(_good_payload())):
            result = test_cloudflare_connection(timeout_seconds=1)
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.cidr_count, 3)
        self.assertIsNone(result.error)

    def test_failure(self) -> None:
        """连接失败：ok=False，error 为中文提示。"""
        error = urllib.error.URLError(TimeoutError("timed out"))
        with _patch_urlopen(side_effect=error):
            result = test_cloudflare_connection(timeout_seconds=1)
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error_category, CATEGORY_TIMEOUT)


class TestHttpErrorMessage(unittest.TestCase):
    """测试 HTTP 错误文案。"""

    def test_message_contains_code(self) -> None:
        self.assertEqual(http_error_message(403), "获取失败：HTTP状态码：403")


if __name__ == "__main__":
    unittest.main(verbosity=2)