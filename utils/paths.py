"""运行时路径解析（V1.4.1 新增）。

程序既要用 `python main.py` 直接跑，也要能被 PyInstaller 打包成 exe 分发。
两种情况下"程序所在目录"的计算方式不同：

- 源码运行：以本文件上两级目录（项目根目录）为准；
- 打包运行：模块被解包到临时目录，`__file__` 指向临时位置，程序退出后
  临时目录会被删除，日志和导出文件会跟着丢失。此时必须以 exe 所在目录为准。

另外，如果用户把 exe 放进 Program Files 这类普通用户不可写的目录，
直接创建 logs/ 会抛 PermissionError 导致程序打不开，因此这里做一次可写性
探测，不可写时退回到用户目录（%LOCALAPPDATA% → 我的文档）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 回退目录使用的子目录名（英文，避免部分环境读取中文路径出问题）
FALLBACK_DIR_NAME = "IPOptimizer"

# 缓存解析结果，避免重复探测文件系统
_resolved_root: Path | None = None


def install_root() -> Path:
    """返回"程序本体所在目录"：打包后是 exe 所在目录，源码运行是项目根目录。"""
    if getattr(sys, "frozen", False):  # PyInstaller 打包后运行时此属性为 True
        return Path(sys.executable).resolve().parent
    # 本文件位于 <项目根>/utils/paths.py，上两级即项目根目录
    return Path(__file__).resolve().parent.parent


def _candidate_roots() -> list[Path]:
    """按优先级返回候选数据目录：程序所在目录 → 用户本地数据目录 → 我的文档。"""
    candidates: list[Path] = [install_root()]

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / FALLBACK_DIR_NAME)

    candidates.append(Path.home() / "Documents" / FALLBACK_DIR_NAME)
    return candidates


def _is_writable(root: Path) -> bool:
    """真正写一个临时文件来验证可写性，只看 os.access 在部分 Windows 目录上不可靠。"""
    probe_dir = root / "logs"
    probe_file = probe_dir / ".writetest.tmp"
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        return True
    except OSError:
        return False


def data_root() -> Path:
    """返回可写的程序数据根目录（logs / output 都放在它下面），结果会被缓存。"""
    global _resolved_root
    if _resolved_root is not None:
        return _resolved_root

    for candidate in _candidate_roots():
        if _is_writable(candidate):
            _resolved_root = candidate
            return _resolved_root

    # 全部探测失败：退回安装目录，让后续 logging / 导出模块给出具体中文报错
    _resolved_root = install_root()
    return _resolved_root


# 兼容别名的日志目录与输出目录（模块级常量，测试里可直接替换）
DATA_ROOT = data_root()
LOG_DIR = DATA_ROOT / "logs"
OUTPUT_DIR = DATA_ROOT / "output"


def is_packaged() -> bool:
    """当前是否运行在 PyInstaller 打包后的环境里。"""
    return bool(getattr(sys, "frozen", False))
