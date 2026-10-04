"""HTTP/HTTPS 连通性测试 + 下载速度测试模块（V1.2 新增）。

测试思路：
1. 在已确认 TCP 可通的 IP 上发起 HTTP 请求；
2. 对 Cloudflare 的 IP，TLS 握手时 SNI 使用 speed.cloudflare.com，
   并按该域名验证证书（Cloudflare 边缘节点对任意 IP 都接受这个 SNI）；
3. HTTP 连通性测试：请求 /__down?bytes=0（Cloudflare 官方测速端点，响应快、流量为 0）；
4. 下载测速：请求 /__down?bytes=N，统计实际收到的字节数和耗时，算出速度。

端口协议规则（Cloudflare 支持的端口）：
    HTTPS：443, 2053, 2083, 2087, 2096, 8443
    HTTP ：80, 8080, 8880, 2052, 2082, 2086, 2095

所有网络操作都带超时，任何异常都会被捕获并转成中文错误信息，不会让程序崩溃。
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from dataclasses import dataclass
from typing import Optional

from utils.logger import get_logger

logger: logging.Logger = get_logger()

# Cloudflare 官方测速域名（TLS SNI 与 HTTP Host 都使用它）
DEFAULT_HTTP_HOST = "speed.cloudflare.com"

# 走 HTTPS 的端口集合；其余 Cloudflare 端口按 HTTP 处理
HTTPS_PORTS = {443, 2053, 2083, 2087, 2096, 8443}

# 每次读取网络数据的块大小（64KB）
READ_CHUNK_SIZE = 65536

# HTTP 请求头模板（Connection: close 让服务器发完就断开，方便统计下载字节数）
REQUEST_TEMPLATE = (
    "GET /__down?bytes={bytes} HTTP/1.1\r\n"
    "Host: {host}\r\n"
    "User-Agent: IP-Optimizer/1.2\r\n"
    "Accept: */*\r\n"
    "Connection: close\r\n"
    "\r\n"
)

# 错误信息（界面直接显示给用户）
ERROR_TIMEOUT = "timeout"
ERROR_TLS = "TLS握手失败"
ERROR_NO_RESPONSE = "无HTTP响应"
ERROR_BAD_STATUS = "HTTP状态异常"


@dataclass(frozen=True)
class HttpResult:
    """单个 IP 的 HTTP 连通性测试结果。"""

    success: bool = False                # 是否拿到 2xx/3xx 响应
    status: Optional[int] = None         # HTTP 状态码，例如 200
    latency: Optional[int] = None        # 从发起到收到响应头的耗时（毫秒）
    error: Optional[str] = None          # 失败原因；成功时为 None
    https: bool = False                  # 本次是否走 HTTPS


@dataclass(frozen=True)
class DownloadResult:
    """单个 IP 的下载测速结果。"""

    success: bool = False                # 是否成功下载到数据
    bytes_received: int = 0              # 实际收到的字节数
    speed_bps: Optional[float] = None    # 下载速度（字节/秒）
    elapsed_ms: Optional[int] = None     # 下载耗时（毫秒）
    error: Optional[str] = None          # 失败原因；成功时为 None


def is_https_port(port: int) -> bool:
    """判断端口是否走 HTTPS。"""
    return port in HTTPS_PORTS


def _create_ssl_context() -> ssl.SSLContext:
    """创建 TLS 上下文：按 speed.cloudflare.com 验证证书（解决 SNI 问题）。"""
    return ssl.create_default_context()


def _parse_status_line(line: bytes) -> Optional[int]:
    """解析 HTTP 状态行，返回状态码；格式不对返回 None。

    例如 b"HTTP/1.1 200 OK" -> 200
    """
    try:
        parts = line.decode("latin-1", errors="replace").split()
        if len(parts) >= 2 and parts[0].upper().startswith("HTTP/") and parts[1].isdigit():
            return int(parts[1])
    except Exception:
        pass
    return None


async def _open_http_connection(
    ip: str,
    port: int,
    timeout_ms: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """建立 HTTP/HTTPS 连接（含 TLS 握手），带超时。"""
    timeout_seconds = max(int(timeout_ms), 1) / 1000.0
    use_ssl = is_https_port(port)

    connect_task = asyncio.open_connection(
        host=ip,
        port=port,
        ssl=_create_ssl_context() if use_ssl else None,
        # 关键：连接目标是 IP，但 TLS 的 SNI/证书校验域名用 speed.cloudflare.com
        server_hostname=DEFAULT_HTTP_HOST if use_ssl else None,
    )
    return await asyncio.wait_for(connect_task, timeout=timeout_seconds)


async def _read_response_headers(
    reader: asyncio.StreamReader,
    deadline: float,
) -> tuple[Optional[int], int]:
    """读取状态行和响应头，返回 (状态码, 头部字节数)。"""
    header_bytes = 0
    status: Optional[int] = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        line = await asyncio.wait_for(reader.readline(), timeout=remaining)
        header_bytes += len(line)
        if not line:
            # 连接被对方关闭
            if status is None:
                raise ConnectionResetError(ERROR_NO_RESPONSE)
            break
        if status is None:
            status = _parse_status_line(line)
            if status is None:
                raise ValueError(ERROR_NO_RESPONSE)
        if line in (b"\r\n", b"\n"):
            break  # 空行：响应头结束

    return status, header_bytes


async def http_test(
    ip: str,
    port: int = 443,
    timeout_ms: int = 3000,
    host: str = DEFAULT_HTTP_HOST,
) -> HttpResult:
    """对单个 IP 做 HTTP/HTTPS 连通性测试。

    请求 /__down?bytes=0：几乎不消耗流量，只验证 HTTP 层是否可用。
    """
    timeout_seconds = max(int(timeout_ms), 1) / 1000.0
    deadline = time.monotonic() + timeout_seconds
    start_time = time.perf_counter()
    writer: Optional[asyncio.StreamWriter] = None

    try:
        reader, writer = await _open_http_connection(ip, port, timeout_ms)
        request = REQUEST_TEMPLATE.format(bytes=0, host=host).encode("latin-1")
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)

        status, _ = await _read_response_headers(reader, deadline)
        latency = int(round((time.perf_counter() - start_time) * 1000))

        # 2xx / 3xx 都算 HTTP 可用
        if status is not None and 200 <= status < 400:
            return HttpResult(success=True, status=status, latency=latency,
                              error=None, https=is_https_port(port))
        return HttpResult(success=False, status=status, latency=latency,
                          error=f"{ERROR_BAD_STATUS}（{status}）", https=is_https_port(port))

    except asyncio.TimeoutError:
        return HttpResult(success=False, status=None, latency=None,
                          error=ERROR_TIMEOUT, https=is_https_port(port))
    except ssl.SSLError as exc:
        return HttpResult(success=False, status=None, latency=None,
                          error=f"{ERROR_TLS}：{exc}", https=is_https_port(port))
    except ConnectionResetError as exc:
        # 区分「无响应」和其他重置
        message = str(exc) if str(exc) else "连接被重置"
        return HttpResult(success=False, status=None, latency=None,
                          error=message if message == ERROR_NO_RESPONSE else "连接被重置",
                          https=is_https_port(port))
    except ValueError:
        return HttpResult(success=False, status=None, latency=None,
                          error=ERROR_NO_RESPONSE, https=is_https_port(port))
    except OSError as exc:
        return HttpResult(success=False, status=None, latency=None,
                          error=f"网络错误：{exc}", https=is_https_port(port))
    except Exception as exc:  # 兜底：任何异常都不能让程序崩溃
        logger.exception("HTTP 测试出现未预期的异常：%s:%s", ip, port)
        return HttpResult(success=False, status=None, latency=None,
                          error=f"未知错误：{exc}", https=is_https_port(port))
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


async def download_test(
    ip: str,
    port: int = 443,
    download_bytes: int = 1024 * 1024,
    timeout_ms: int = 10000,
    host: str = DEFAULT_HTTP_HOST,
) -> DownloadResult:
    """对单个 IP 做下载测速：请求 /__down?bytes=N 并统计速度。

    注意：这个函数自己建立连接（不复用 HTTP 测试的连接），
    这样每次测速都是完整、独立的一次请求，结果更真实。
    """
    # 下载数量至少 1 字节，并且限制在 1B ~ 100MB 之间
    download_bytes = max(1, min(int(download_bytes), 100 * 1024 * 1024))
    timeout_seconds = max(int(timeout_ms), 1) / 1000.0
    start_time = time.perf_counter()
    writer: Optional[asyncio.StreamWriter] = None

    try:
        reader, writer = await _open_http_connection(ip, port, timeout_ms)
        request = REQUEST_TEMPLATE.format(bytes=download_bytes, host=host).encode("latin-1")
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=timeout_seconds)

        # 重新计算截止时间：连接 + 发送已经消耗了一部分时间，
        # 这里给“读取响应体”重新计算一个完整的超时窗口，避免大文件下载被误判为超时。
        deadline = time.monotonic() + timeout_seconds

        status, _ = await _read_response_headers(reader, deadline)
        if status is None or not (200 <= status < 400):
            return DownloadResult(success=False, bytes_received=0, speed_bps=None,
                                  elapsed_ms=None,
                                  error=f"{ERROR_BAD_STATUS}（{status}）")

        # 循环读取响应体，直到收满 Content-Length 或对方关闭连接
        received = 0
        while received < download_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            chunk = await asyncio.wait_for(
                reader.read(min(READ_CHUNK_SIZE, download_bytes - received)),
                timeout=remaining,
            )
            if not chunk:
                break  # 对方提前关闭连接
            received += len(chunk)

        elapsed = time.perf_counter() - start_time
        elapsed_ms = max(int(round(elapsed * 1000)), 1)

        if received <= 0:
            return DownloadResult(success=False, bytes_received=0, speed_bps=None,
                                  elapsed_ms=elapsed_ms, error=ERROR_NO_RESPONSE)

        speed_bps = received / (elapsed_ms / 1000.0)
        return DownloadResult(success=True, bytes_received=received,
                              speed_bps=speed_bps, elapsed_ms=elapsed_ms, error=None)

    except asyncio.TimeoutError:
        return DownloadResult(success=False, bytes_received=0, speed_bps=None,
                              elapsed_ms=None, error=ERROR_TIMEOUT)
    except ssl.SSLError as exc:
        return DownloadResult(success=False, bytes_received=0, speed_bps=None,
                              elapsed_ms=None, error=f"{ERROR_TLS}：{exc}")
    except ConnectionResetError:
        return DownloadResult(success=False, bytes_received=0, speed_bps=None,
                              elapsed_ms=None, error="连接被重置")
    except ValueError:
        return DownloadResult(success=False, bytes_received=0, speed_bps=None,
                              elapsed_ms=None, error=ERROR_NO_RESPONSE)
    except OSError as exc:
        return DownloadResult(success=False, bytes_received=0, speed_bps=None,
                              elapsed_ms=None, error=f"网络错误：{exc}")
    except Exception as exc:  # 兜底
        logger.exception("下载测速出现未预期的异常：%s:%s", ip, port)
        return DownloadResult(success=False, bytes_received=0, speed_bps=None,
                              elapsed_ms=None, error=f"未知错误：{exc}")
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass


def format_speed(speed_bps: Optional[float]) -> str:
    """把字节/秒格式化成易读文本，例如 12.3 MB/s。"""
    if speed_bps is None:
        return "--"
    if speed_bps >= 1024 * 1024:
        return f"{speed_bps / 1024 / 1024:.1f} MB/s"
    return f"{speed_bps / 1024:.0f} KB/s"