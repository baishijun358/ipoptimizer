"""IP 校验模块。

使用 Python 标准库 ipaddress 判断一个 IP 能不能用于公网测试：
过滤 非法 IP、空 IP、IPv6、私有地址、回环地址、链路本地地址、组播地址等。

同时提供两个方便 GUI 调用的“导入 + 校验”组合函数，
这样界面层只需要调用一个函数就能拿到结果和统计数字。
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from core.ip_loader import (
    MAX_PORT,
    MIN_PORT,
    IPEntry,
    LoadResult,
    load_from_file,
    parse_text,
)
from utils.logger import get_logger

logger: logging.Logger = get_logger()

# 校验不通过时的中文原因（界面上直接显示给用户）
REASON_EMPTY = "空 IP"
REASON_IPV6 = "IPv6 地址（第一阶段只支持 IPv4）"
REASON_FORMAT = "非法 IPv4 格式"
REASON_LOOPBACK = "回环地址"
REASON_LINK_LOCAL = "链路本地地址"
REASON_MULTICAST = "组播地址"
REASON_UNSPECIFIED = "未指定地址"
REASON_RESERVED = "保留地址"
REASON_PRIVATE = "私有地址"
REASON_NOT_GLOBAL = "非公网地址"


@dataclass(frozen=True)
class InvalidEntry:
    """校验不通过的 IP 记录。"""

    entry: IPEntry
    reason: str


@dataclass
class ValidationResult:
    """IP 校验结果。"""

    valid: List[IPEntry] = field(default_factory=list)          # 可以用来测速的 IP
    invalid: List[InvalidEntry] = field(default_factory=list)   # 被过滤掉的 IP

    @property
    def valid_count(self) -> int:
        return len(self.valid)

    @property
    def invalid_count(self) -> int:
        return len(self.invalid)


@dataclass
class ImportSummary:
    """给用户看的导入统计信息。"""

    total_lines: int = 0         # 读取数量（非空行）
    unique_count: int = 0        # 去重后数量
    duplicate_count: int = 0     # 重复数量
    format_error_count: int = 0  # 格式错误行数
    valid_count: int = 0         # 有效 IP
    invalid_count: int = 0       # 无效 IP（格式错误 + 校验不通过）

    def to_text(self) -> str:
        """生成中文摘要，用于界面提示和日志。"""
        return (
            f"读取数量：{self.total_lines}\n"
            f"去重后数量：{self.unique_count}\n"
            f"重复数量：{self.duplicate_count}\n"
            f"格式错误：{self.format_error_count}\n"
            f"有效 IP：{self.valid_count}\n"
            f"无效 IP：{self.invalid_count}"
        )


@dataclass
class ImportOutcome:
    """导入 + 校验的完整结果（方便界面一次拿到所有数据）。"""

    load_result: LoadResult
    validation: ValidationResult
    summary: ImportSummary

    @property
    def valid_entries(self) -> List[IPEntry]:
        """可直接用于测速的 IP 列表。"""
        return self.validation.valid


def validate_ip(ip: str) -> Tuple[bool, Optional[str]]:
    """校验单个 IPv4 地址。

    返回 (是否可用, 不可用原因)。原因文本可直接显示给用户。
    """
    text = (ip or "").strip()
    if not text:
        return False, REASON_EMPTY

    # 含冒号的先按 IPv6 处理（第一阶段不做 IPv6 测速）
    if ":" in text:
        return False, REASON_IPV6

    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False, REASON_FORMAT

    if address.version != 4:
        return False, REASON_IPV6

    # 判断顺序会影响提示信息，这里按“最具体的原因优先”排列
    if address.is_loopback:
        return False, REASON_LOOPBACK
    if address.is_link_local:
        return False, REASON_LINK_LOCAL
    if address.is_multicast:
        return False, REASON_MULTICAST
    if address.is_unspecified:
        return False, REASON_UNSPECIFIED
    if address.is_reserved:
        return False, REASON_RESERVED
    if address.is_private:
        return False, REASON_PRIVATE
    if not address.is_global:
        return False, REASON_NOT_GLOBAL

    return True, None


def validate_port(port: Optional[int]) -> Tuple[bool, Optional[str]]:
    """校验端口号；端口为空表示使用界面上的默认端口，是允许的。"""
    if port is None:
        return True, None
    if not isinstance(port, int):
        return False, "端口必须是数字"
    if not MIN_PORT <= port <= MAX_PORT:
        return False, f"端口超出范围（{MIN_PORT}-{MAX_PORT}）"
    return True, None


def validate_entries(entries: List[IPEntry]) -> ValidationResult:
    """批量校验 IP 列表，返回有效和无效两组结果。"""
    result = ValidationResult()

    for entry in entries:
        is_valid, reason = validate_ip(entry.ip)
        if is_valid:
            is_port_valid, port_reason = validate_port(entry.port)
            if not is_port_valid:
                result.invalid.append(InvalidEntry(entry, port_reason or "端口不合法"))
                continue
            result.valid.append(entry)
        else:
            result.invalid.append(InvalidEntry(entry, reason or REASON_FORMAT))

    logger.info("IP 校验完成：有效 %s 条，无效 %s 条", result.valid_count, result.invalid_count)
    return result


def create_summary(load_result: LoadResult, validation: ValidationResult) -> ImportSummary:
    """根据读取结果和校验结果生成统计信息。

    数量关系：读取数量 = 有效 IP + 重复数量 + 无效 IP
    """
    return ImportSummary(
        total_lines=load_result.total_lines,
        unique_count=load_result.entry_count,
        duplicate_count=load_result.duplicate_count,
        format_error_count=load_result.format_error_count,
        valid_count=validation.valid_count,
        invalid_count=load_result.format_error_count + validation.invalid_count,
    )


def import_from_text(text: str) -> ImportOutcome:
    """从粘贴的文本导入并校验 IP。"""
    load_result = parse_text(text)
    validation = validate_entries(load_result.entries)
    summary = create_summary(load_result, validation)
    logger.info("粘贴导入 IP：有效 %s 条（读取 %s 行）", summary.valid_count, summary.total_lines)
    return ImportOutcome(load_result=load_result, validation=validation, summary=summary)


def import_from_file(path: str | Path) -> ImportOutcome:
    """从文本文件导入并校验 IP；文件相关错误会抛出 IPLoaderError。"""
    load_result = load_from_file(path)
    validation = validate_entries(load_result.entries)
    summary = create_summary(load_result, validation)
    logger.info(
        "文件导入 IP：%s（有效 %s 条，读取 %s 行）",
        path,
        summary.valid_count,
        summary.total_lines,
    )
    return ImportOutcome(load_result=load_result, validation=validation, summary=summary)