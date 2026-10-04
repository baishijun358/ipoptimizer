"""IP 导入模块。

负责“把用户给的文本变成一条条待测速的 IP”：
1. 支持从文本文件读取（TXT）；
2. 支持用户直接粘贴的多行文本；
3. 支持 `IP` 和 `IP:端口` 两种写法；
4. 自动去重（按 “IP + 端口” 组合去重，端口为空时视为同一条）。

注意：本模块只负责“读取 / 解析 / 去重”，
IP 是否合法（非法、私有、回环、IPv6 等）由 ip_validator 模块负责。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from utils.logger import get_logger

logger: logging.Logger = get_logger()

# 默认测速端口（用户没有写端口时使用）
DEFAULT_PORT = 443
MIN_PORT = 1
MAX_PORT = 65535

# 读取文件时依次尝试的编码：utf-8-sig 用于处理带 BOM 的 UTF-8 文件
DEFAULT_ENCODINGS: Tuple[str, ...] = ("utf-8-sig", "utf-8", "gbk")

# 以这些符号开头的行会被当作注释忽略
COMMENT_PREFIXES: Tuple[str, ...] = ("#", "//", ";")


class IPLoaderError(Exception):
    """IP 导入过程中的可预期错误（文件不存在、编码异常等），提示信息为中文。"""


@dataclass(frozen=True)
class IPEntry:
    """一条待测速的 IP 记录。"""

    ip: str                       # IP 地址文本（例如 104.16.0.1）
    port: Optional[int] = None    # 用户指定的端口；为 None 表示使用界面上的端口
    source: str = ""              # 原始文本行，便于排查问题

    @property
    def dedup_key(self) -> Tuple[str, Optional[int]]:
        """去重用的键：同一个 IP 配不同端口时视为两条不同的测试目标。"""
        return (self.ip, self.port)

    @property
    def text(self) -> str:
        """显示用的文本，例如 104.16.0.1:443。"""
        if self.port is None:
            return self.ip
        return f"{self.ip}:{self.port}"


@dataclass(frozen=True)
class LineError:
    """一行无法解析的文本。"""

    line_number: int   # 行号（从 1 开始）
    text: str          # 原始文本
    reason: str        # 中文原因说明


@dataclass
class LoadResult:
    """IP 导入结果。"""

    entries: List[IPEntry] = field(default_factory=list)          # 去重后的 IP 列表
    total_lines: int = 0                                          # 读取到的非空有效行数
    duplicate_count: int = 0                                      # 重复行数量
    format_errors: List[LineError] = field(default_factory=list)  # 格式错误的行

    @property
    def entry_count(self) -> int:
        """去重之后的 IP 数量。"""
        return len(self.entries)

    @property
    def format_error_count(self) -> int:
        """格式错误的行数。"""
        return len(self.format_errors)


def _parse_port(port_text: str) -> int:
    """把端口字符串转成整数；不合法时抛出 ValueError。"""
    text = port_text.strip()
    if not text:
        raise ValueError("端口不能为空")
    if not text.isdigit():
        raise ValueError("端口必须是数字")
    port = int(text)
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(f"端口超出范围（{MIN_PORT}-{MAX_PORT}）")
    return port


def parse_address(text: str) -> Tuple[str, Optional[int]]:
    """把一行文本解析成 (ip, port)。

    支持的形式：
        104.16.0.1
        104.16.0.1:443
        [2606:4700::1]:443   （IPv6 会在校验阶段被过滤掉）

    解析失败（例如端口不是数字）时抛出 ValueError。
    """
    raw = text.strip()

    # 1) [IPv6]:端口 形式
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            raise ValueError("IPv6 地址缺少右括号 ]")
        host = raw[1:end].strip()
        rest = raw[end + 1:].strip()
        if not rest:
            return host, None
        if not rest.startswith(":"):
            raise ValueError("IP 与端口之间需要用冒号分隔")
        return host, _parse_port(rest[1:])

    colon_count = raw.count(":")
    if colon_count == 0:
        # 只有 IP，没有端口
        return raw, None
    if colon_count == 1:
        # IP:端口
        host, port_text = raw.split(":", 1)
        return host.strip(), _parse_port(port_text)

    # 冒号多于一个：按 IPv6 地址处理（校验阶段会过滤掉）
    return raw, None


def parse_text(text: str) -> LoadResult:
    """解析多行文本，返回去重后的结果。

    会自动跳过：空行、注释行（以 # // ; 开头）、行内 # 注释之后的内容。
    """
    result = LoadResult()
    seen: set[Tuple[str, Optional[int]]] = set()

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        # 去掉行内注释和首尾空白
        line = raw_line.split("#", 1)[0].strip() if "#" in raw_line else raw_line.strip()
        if not line:
            continue  # 空行直接跳过
        if line.startswith(COMMENT_PREFIXES):
            continue  # 注释行直接跳过

        result.total_lines += 1

        try:
            ip, port = parse_address(line)
        except ValueError as exc:
            result.format_errors.append(LineError(line_number, line, str(exc)))
            continue

        if not ip:
            result.format_errors.append(LineError(line_number, line, "IP 不能为空"))
            continue

        entry = IPEntry(ip=ip, port=port, source=line)
        if entry.dedup_key in seen:
            result.duplicate_count += 1
            continue

        seen.add(entry.dedup_key)
        result.entries.append(entry)

    logger.info(
        "解析 IP 文本完成：读取 %s 行，去重后 %s 条，重复 %s 条，格式错误 %s 行",
        result.total_lines,
        result.entry_count,
        result.duplicate_count,
        result.format_error_count,
    )
    return result


def _read_file_text(path: Path, encodings: Iterable[str]) -> str:
    """按给定编码依次尝试读取文件内容。"""
    last_error: Optional[UnicodeDecodeError] = None
    for encoding in encodings:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            # 当前编码不匹配，换下一种编码继续尝试
            last_error = exc
        except OSError as exc:
            # 文件被占用、没有权限等
            raise IPLoaderError(f"读取文件失败：{exc}") from exc

    raise IPLoaderError(
        "文件编码无法识别（已尝试 utf-8、gbk）。请用记事本另存为 UTF-8 编码后再试。"
    ) from last_error


def load_from_file(
    path: str | Path,
    encodings: Iterable[str] = DEFAULT_ENCODINGS,
) -> LoadResult:
    """从文本文件导入 IP 列表。

    文件不存在、不是文件、编码异常时抛出 IPLoaderError（中文提示）。
    """
    file_path = Path(path)

    if not file_path.exists():
        raise IPLoaderError(f"IP 文件不存在：{file_path}")
    if not file_path.is_file():
        raise IPLoaderError(f"这不是一个文件：{file_path}")

    content = _read_file_text(file_path, encodings)
    result = parse_text(content)

    logger.info(
        "从文件导入 IP：%s（读取 %s 行，去重后 %s 条）",
        file_path,
        result.total_lines,
        result.entry_count,
    )
    return result