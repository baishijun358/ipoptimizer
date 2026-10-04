"""TCP 测速模块。

流程：IP -> 建立 TCP 连接（默认 443 端口） -> 记录连接耗时 -> 返回结果。
所有网络操作都带超时，任何异常都会被捕获并转成中文错误信息，不会让程序崩溃。

V1.2 说明：
    本模块仍然只负责「TCP 连接测试」，但 TestResult 里新增了 HTTP / 下载两个阶段的
    结果字段（全部带默认值）。这样 scanner 可以把三级测试的结果合并成一条记录，
    而只关心 TCP 的旧代码（包括第一阶段的全部功能）完全不受影响。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional

from core.ip_loader import DEFAULT_PORT
from utils.logger import get_logger

if TYPE_CHECKING:  # 只用于类型标注，运行时导入避免多余的模块依赖
    from core.http_tester import DownloadResult, HttpResult

logger: logging.Logger = get_logger()

# 错误信息（界面直接显示给用户）
ERROR_TIMEOUT = "timeout"
ERROR_DNS = "DNS解析失败"
ERROR_REFUSED = "连接被拒绝"


@dataclass(frozen=True)
class TestResult:
    """单个 IP 的测速结果（三级测试的完整记录）。"""

    ip: str
    port: int
    # ---- 第一级：TCP 连接测试 ----
    latency: Optional[int] = None   # TCP 连接耗时（毫秒）；失败时为 None
    success: bool = False           # TCP 是否连接成功
    error: Optional[str] = None     # TCP 失败原因；成功时为 None

    # ---- 第二级：HTTP/HTTPS 连通性测试（V1.2 新增）----
    http_tested: bool = False                     # 是否执行过 HTTP 测试
    http_success: bool = False                    # 是否拿到 2xx/3xx 响应
    http_status: Optional[int] = None             # HTTP 状态码，例如 200
    http_latency: Optional[int] = None            # HTTP 响应耗时（毫秒）
    http_error: Optional[str] = None              # HTTP 失败原因

    # ---- 第三级：下载测速（V1.2 新增）----
    download_tested: bool = False                 # 是否执行过下载测速
    download_success: bool = False                # 是否成功下载到数据
    download_speed_bps: Optional[float] = None    # 下载速度（字节/秒）
    download_bytes: int = 0                       # 实际下载到的字节数
    download_elapsed_ms: Optional[int] = None     # 下载耗时（毫秒）
    download_error: Optional[str] = None          # 下载失败原因

    def as_dict(self) -> dict:
        """转成字典，方便后续阶段导出 JSON / CSV。"""
        return {
            "ip": self.ip,
            "port": self.port,
            "latency": self.latency,
            "success": self.success,
            "error": self.error,
            "http_tested": self.http_tested,
            "http_success": self.http_success,
            "http_status": self.http_status,
            "http_latency": self.http_latency,
            "http_error": self.http_error,
            "download_tested": self.download_tested,
            "download_success": self.download_success,
            "download_speed_bps": self.download_speed_bps,
            "download_bytes": self.download_bytes,
            "download_elapsed_ms": self.download_elapsed_ms,
            "download_error": self.download_error,
        }


def attach_stage_results(
    tcp_result: TestResult,
    http_result: Optional["HttpResult"] = None,
    download_result: Optional["DownloadResult"] = None,
) -> TestResult:
    """把 HTTP / 下载两个阶段的结果合并进 TCP 结果，返回一条完整的记录。

    传入 None 表示该阶段没有执行（例如 TCP 不通、或用户在界面上关闭了该阶段）。
    """
    http_fields: dict = {}
    if http_result is not None:
        http_fields = {
            "http_tested": True,
            "http_success": http_result.success,
            "http_status": http_result.status,
            "http_latency": http_result.latency,
            "http_error": http_result.error,
        }

    download_fields: dict = {}
    if download_result is not None:
        download_fields = {
            "download_tested": True,
            "download_success": download_result.success,
            "download_speed_bps": download_result.speed_bps,
            "download_bytes": download_result.bytes_received,
            "download_elapsed_ms": download_result.elapsed_ms,
            "download_error": download_result.error,
        }

    if not http_fields and not download_fields:
        return tcp_result
    return replace(tcp_result, **http_fields, **download_fields)


async def tcp_ping(
    ip: str,
    port: int = DEFAULT_PORT,
    timeout_ms: int = 1000,
) -> TestResult:
    """对单个 IP:端口 做一次 TCP 连接测试。

    参数：
        ip：IPv4 地址
        port：目标端口
        timeout_ms：超时时间（毫秒）
    返回：
        TestResult
    """
    # 保证超时时间至少 1ms，避免 0 或负数导致异常
    timeout_seconds = max(int(timeout_ms), 1) / 1000.0
    start_time = time.perf_counter()
    writer: Optional[asyncio.StreamWriter] = None

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host=ip, port=port),
            timeout=timeout_seconds,
        )
        # 连接建立成功，记录耗时（毫秒）
        latency = int(round((time.perf_counter() - start_time) * 1000))
        return TestResult(ip=ip, port=port, latency=latency, success=True, error=None)

    except asyncio.TimeoutError:
        # 超时不算程序错误，属于正常结果
        return TestResult(ip=ip, port=port, latency=None, success=False, error=ERROR_TIMEOUT)
    except socket.gaierror:
        return TestResult(ip=ip, port=port, latency=None, success=False, error=ERROR_DNS)
    except ConnectionRefusedError:
        return TestResult(ip=ip, port=port, latency=None, success=False, error=ERROR_REFUSED)
    except ConnectionResetError:
        return TestResult(ip=ip, port=port, latency=None, success=False, error="连接被重置")
    except OSError as exc:
        return TestResult(ip=ip, port=port, latency=None, success=False, error=f"网络错误：{exc}")
    except Exception as exc:  # 兜底：任何异常都不能让程序崩溃
        logger.exception("测速出现未预期的异常：%s:%s", ip, port)
        return TestResult(ip=ip, port=port, latency=None, success=False, error=f"未知错误：{exc}")
    finally:
        # 测试完立即关闭连接，避免占用本机端口
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass