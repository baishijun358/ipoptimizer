"""Cloudflare 官方 IPv4 网段获取模块。

数据来源：Cloudflare 官方公开 API（无需任何密钥）：
    https://api.cloudflare.com/client/v4/ips

备用来源（同样是 Cloudflare 官方公开的纯文本列表）：
    https://www.cloudflare.com/ips-v4

备用来源的作用：如果主 API 请求失败（例如本机网络或代理异常），
会自动尝试备用源；两个都失败时，把真实错误写入日志，并把
分类后的中文提示显示给用户。

返回示例：
    {"success": true, "result": {"ipv4_cidrs": ["104.16.0.0/13", ...], ...}}

本模块只负责「请求 + 解析 + 错误处理」，不做任何 IP 生成（那是
cidr_generator 的职责）。全部使用 Python 标准库 urllib，不新增依赖。
注意：不绕过 SSL 证书验证（使用 urllib 默认的证书校验）。

命令行自测（不启动界面，直接测试 API 连通性）：
    python -m core.cloudflare_source
"""

from __future__ import annotations

import ipaddress
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from utils.logger import get_logger

logger: logging.Logger = get_logger()

# Cloudflare 官方 IP 列表 API（主源）
API_URL = "https://api.cloudflare.com/client/v4/ips"
# Cloudflare 官方纯文本 IPv4 列表（备用源，同样是官方公开数据）
FALLBACK_TEXT_URL = "https://www.cloudflare.com/ips-v4"

# 网络超时（秒）：既不能太短（网络慢时误报失败），也不能太久（用户等太久）
DEFAULT_TIMEOUT_SECONDS = 10.0

# ---------------------------------------------------------------------
# 错误分类（供界面和测试判断错误类型）
# ---------------------------------------------------------------------
CATEGORY_TIMEOUT = "timeout"      # 请求超时
CATEGORY_NETWORK = "network"      # 连接错误（无法连接、被重置、DNS 失败等）
CATEGORY_SSL = "ssl"              # SSL / 证书错误
CATEGORY_HTTP = "http"            # HTTP 状态码错误
CATEGORY_JSON = "json"            # 返回内容不是 JSON
CATEGORY_API = "api"              # API 返回异常（success=false / 缺少字段等）

# ---------------------------------------------------------------------
# 错误信息（中文，界面直接显示；真实异常细节只写入日志）
# ---------------------------------------------------------------------
ERROR_TIMEOUT = "获取失败：网络连接超时"
ERROR_NETWORK = "获取失败：网络连接错误"
ERROR_SSL = "获取失败：SSL/证书错误"
ERROR_BAD_JSON = "获取失败：返回内容解析失败"
ERROR_API_FAILED = "获取失败：Cloudflare API 返回异常"
ERROR_NO_RESULT = "获取失败：Cloudflare API 返回异常（缺少 result 数据）"
ERROR_NO_IPV4 = "获取失败：没有获取到有效的 IPv4 网段"


def http_error_message(code: int) -> str:
    """HTTP 错误的中文提示（包含状态码，方便用户排查）。"""
    return f"获取失败：HTTP状态码：{code}"


def _proxy_hint() -> str:
    """当本机配置了系统代理时，给出更准确的中文提示。

    很多用户的电脑配置了本地代理（例如 127.0.0.1:xxxx）。
    如果代理软件没有启动，Python 的请求会连接失败，看起来像「没有网络」，
    所以这里把代理信息提示给用户，方便快速定位问题。
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    proxy = proxies.get("https") or proxies.get("http")
    if proxy:
        return f"（本机配置了系统代理 {proxy}：请确认代理软件已启动，或关闭系统代理后重试）"
    return "（请检查网络连接后重试）"


class CloudflareError(Exception):
    """获取 Cloudflare IP 失败。

    message：中文信息，可直接显示给用户；
    category：错误分类（CATEGORY_* 常量，供界面/测试判断）；
    detail：真实的异常类型与信息（只写入日志，不直接显示给用户）。
    """

    def __init__(self, message: str, category: str = CATEGORY_NETWORK, detail: str = "") -> None:
        super().__init__(message)
        self.category = category
        self.detail = detail


@dataclass
class CidrFetchResult:
    """一次获取的结果。"""

    cidrs: List[str] = field(default_factory=list)  # IPv4 CIDR 列表，例如 104.16.0.0/13
    elapsed_seconds: float = 0.0                    # 获取耗时
    status_code: int = 200                          # HTTP 状态码
    source: str = "api"                             # 数据来源：api=主源，text=备用源


def _do_request(url: str, timeout_seconds: float) -> bytes:
    """发送 GET 请求并返回响应内容（带超时、带 UA、保留 SSL 验证）。

    HTTP 状态码不是 200 时抛出 CloudflareError（CATEGORY_HTTP）。
    网络类异常统一转换成 CloudflareError，并记录真实的异常信息到日志。
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "IP-Optimizer/1.0", "Accept": "application/json, text/plain"},
        method="GET",
    )
    logger.info("Cloudflare API request started: url=%s", url)

    try:
        with urllib.request.urlopen(request, timeout=max(timeout_seconds, 1.0)) as response:
            status = getattr(response, "status", 200)
            logger.info("HTTP status: %s", status)
            if status != 200:
                raise CloudflareError(
                    http_error_message(status), CATEGORY_HTTP, f"HTTP status {status}"
                )
            raw_bytes = response.read()
            logger.info("response length: %s bytes", len(raw_bytes))
            return raw_bytes
    except CloudflareError:
        raise
    except urllib.error.HTTPError as exc:
        # HTTPError 有真实的 HTTP 状态码
        logger.error("connection error: HTTPError %s (%s)", exc.code, exc.reason)
        raise CloudflareError(
            http_error_message(exc.code), CATEGORY_HTTP, f"HTTPError {exc.code}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        detail = f"{type(reason).__name__ if reason is not None else 'URLError'}: {exc.reason}"
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            logger.error("timeout error: %s", detail)
            raise CloudflareError(ERROR_TIMEOUT, CATEGORY_TIMEOUT, detail) from exc
        if isinstance(reason, ssl_error_types()):
            logger.error("connection error: SSL %s", detail)
            raise CloudflareError(ERROR_SSL, CATEGORY_SSL, detail) from exc
        logger.error("connection error: %s", detail)
        raise CloudflareError(
            f"{ERROR_NETWORK}{_proxy_hint()}", CATEGORY_NETWORK, detail
        ) from exc
    except TimeoutError as exc:  # Python 3.10+ 单独的超时异常
        logger.error("timeout error: %s", exc)
        raise CloudflareError(ERROR_TIMEOUT, CATEGORY_TIMEOUT, str(exc)) from exc
    except OSError as exc:  # DNS 解析失败、连接被拒绝、被重置等
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("connection error: %s", detail)
        raise CloudflareError(f"{ERROR_NETWORK}{_proxy_hint()}", CATEGORY_NETWORK, detail) from exc


def ssl_error_types() -> tuple:
    """返回 SSL 相关异常类型（延迟导入，保证可测试性）。"""
    import ssl

    return (ssl.SSLError, ssl.CertificateError)


def _parse_api_payload(raw_bytes: bytes) -> List[str]:
    """解析 JSON API 返回，校验并返回 IPv4 CIDR 列表。"""
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error("JSON parse error: %s", exc)
        raise CloudflareError(ERROR_BAD_JSON, CATEGORY_JSON, f"{type(exc).__name__}: {exc}") from exc

    if not isinstance(payload, dict):
        detail = f"payload type: {type(payload).__name__}"
        logger.error("JSON parse error: %s", detail)
        raise CloudflareError(ERROR_BAD_JSON, CATEGORY_JSON, detail)

    if payload.get("success") is not True:
        errors = payload.get("errors") or []
        detail = f"success=false, errors={errors[:1]}"
        logger.error("Cloudflare API returned error: %s", detail)
        raise CloudflareError(ERROR_API_FAILED, CATEGORY_API, detail)

    result = payload.get("result")
    if not isinstance(result, dict):
        detail = f"missing result, keys={list(payload.keys())}"
        logger.error("Cloudflare API returned error: %s", detail)
        raise CloudflareError(ERROR_NO_RESULT, CATEGORY_API, detail)

    raw_cidrs = result.get("ipv4_cidrs")
    if not isinstance(raw_cidrs, list) or not raw_cidrs:
        detail = f"ipv4_cidrs missing or empty, keys={list(result.keys())}"
        logger.error("Cloudflare API returned error: %s", detail)
        raise CloudflareError(ERROR_NO_IPV4, CATEGORY_API, detail)

    cidrs = _validate_cidrs(raw_cidrs)
    if not cidrs:
        raise CloudflareError(ERROR_NO_IPV4, CATEGORY_API, "all cidrs invalid")
    return cidrs


def _parse_text_payload(raw_bytes: bytes) -> List[str]:
    """解析官方纯文本列表（每行一个 CIDR），校验并返回 IPv4 CIDR 列表。"""
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.error("JSON parse error: text decode %s", exc)
        raise CloudflareError(ERROR_BAD_JSON, CATEGORY_JSON, f"decode: {exc}") from exc

    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    cidrs = _validate_cidrs(lines)
    if not cidrs:
        logger.error("Cloudflare text source returned no valid ipv4 cidrs")
        raise CloudflareError(ERROR_NO_IPV4, CATEGORY_API, "text source empty")
    return cidrs


def _validate_cidrs(raw_items: list) -> List[str]:
    """逐条校验 CIDR 格式（跳过个别坏条目，不影响整体），只保留 IPv4。"""
    cidrs: List[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in seen:
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            logger.warning("跳过格式错误的 CIDR：%r", item)
            continue
        if network.version == 4:
            seen.add(text)
            cidrs.append(text)
    return cidrs


def _fetch_from_api(timeout_seconds: float) -> CidrFetchResult:
    """从主源（JSON API）获取 IPv4 CIDR。"""
    start = time.perf_counter()
    raw_bytes = _do_request(API_URL, timeout_seconds)
    elapsed = time.perf_counter() - start
    cidrs = _parse_api_payload(raw_bytes)
    logger.info("获取 Cloudflare IPv4 网段成功（主源）：%s 个 CIDR，耗时 %.2f 秒", len(cidrs), elapsed)
    return CidrFetchResult(cidrs=cidrs, elapsed_seconds=elapsed, status_code=200, source="api")


def _fetch_from_text(timeout_seconds: float) -> CidrFetchResult:
    """从备用源（官方纯文本列表）获取 IPv4 CIDR。"""
    start = time.perf_counter()
    raw_bytes = _do_request(FALLBACK_TEXT_URL, timeout_seconds)
    elapsed = time.perf_counter() - start
    cidrs = _parse_text_payload(raw_bytes)
    logger.info("获取 Cloudflare IPv4 网段成功（备用源）：%s 个 CIDR，耗时 %.2f 秒", len(cidrs), elapsed)
    return CidrFetchResult(cidrs=cidrs, elapsed_seconds=elapsed, status_code=200, source="text")


def fetch_ipv4_cidrs(timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> CidrFetchResult:
    """获取 Cloudflare 官方 IPv4 CIDR 列表。

    请求流程：
        主源（JSON API）
          ↓ 失败
        备用源（官方纯文本列表）
          ↓ 仍然失败
        抛出主源的错误（真实异常已写入日志）

    任何失败都抛出 CloudflareError（中文信息），不会让程序崩溃。
    """
    logger.info("开始获取 Cloudflare IPv4 网段（超时 %s 秒）", timeout_seconds)
    primary_error: Optional[CloudflareError] = None

    try:
        return _fetch_from_api(timeout_seconds)
    except CloudflareError as exc:
        primary_error = exc
        logger.error("主源请求失败：%s（detail: %s）", exc, exc.detail)

    # 数据级异常（JSON 损坏、缺少 ipv4_cidrs、success=false、HTTP 4xx/5xx）
    # 说明服务端或数据本身出了问题，换备用源也无法解决，直接把真实错误抛给用户；
    # 只有网络级失败（无法连接 / 超时 / SSL）才值得尝试备用源。
    if primary_error.category in (CATEGORY_API, CATEGORY_JSON, CATEGORY_HTTP):
        raise primary_error

    # 主源失败，尝试备用源（同样是官方公开数据）
    try:
        return _fetch_from_text(timeout_seconds)
    except CloudflareError as exc:
        logger.error("备用源请求失败：%s（detail: %s）", exc, exc.detail)
        # 两个都失败：抛主源错误（信息更贴近用户的第一次尝试），详情已全部写入日志
        assert primary_error is not None
        logger.error("获取 Cloudflare IPv4 网段失败：主源与备用源均不可用")
        raise primary_error from exc


# ----------------------------------------------------------------------
# 连接测试（供「测试Cloudflare连接」按钮和命令行使用，不生成 IP）
# ----------------------------------------------------------------------
@dataclass
class ApiTestResult:
    """一次连接测试的结果。"""

    ok: bool = False                # 是否连接成功
    status_code: Optional[int] = None   # HTTP 状态码（成功时 200）
    cidr_count: int = 0             # IPv4 CIDR 数量
    latency_ms: int = 0             # 请求耗时（毫秒）
    source: str = ""                # 数据来源：api / text
    error: Optional[str] = None     # 失败时的中文提示
    error_category: Optional[str] = None  # 失败时的错误分类


def test_cloudflare_connection(timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> ApiTestResult:
    """测试 Cloudflare API 是否可以正常访问（只测试连接，不生成 IP）。"""
    start = time.perf_counter()
    try:
        result = fetch_ipv4_cidrs(timeout_seconds)
    except CloudflareError as exc:
        latency = int((time.perf_counter() - start) * 1000)
        logger.error("Cloudflare 连接测试失败：%s（detail: %s）", exc, exc.detail)
        return ApiTestResult(
            ok=False,
            latency_ms=latency,
            error=str(exc),
            error_category=exc.category,
        )

    latency = int((time.perf_counter() - start) * 1000)
    return ApiTestResult(
        ok=True,
        status_code=result.status_code,
        cidr_count=len(result.cidrs),
        latency_ms=latency,
        source=result.source,
    )


def _run_cli_test() -> int:
    """命令行自测入口：python -m core.cloudflare_source"""
    # Windows 控制台默认 GBK，强制 UTF-8 输出避免乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("正在测试 Cloudflare API 连接……")
    result = test_cloudflare_connection()
    if result.ok:
        print("Cloudflare API连接成功")
        print(f"HTTP状态码：{result.status_code}")
        print(f"IPv4 CIDR数量：{result.cidr_count}")
        print(f"耗时：{result.latency_ms} ms")
        print(f"数据来源：{'主源(JSON API)' if result.source == 'api' else '备用源(官方文本)'}")
        return 0

    print("Cloudflare API连接失败")
    print(result.error or "未知错误")
    print("详细信息请查看 logs/app.log")
    return 1


if __name__ == "__main__":
    sys.exit(_run_cli_test())