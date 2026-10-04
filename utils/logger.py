"""日志模块：统一创建并管理日志记录器。

第一阶段只使用 Python 标准库 logging，日志写入 logs/app.log。
注意：日志中不要记录任何敏感信息（本项目也不收集用户个人信息）。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from utils.paths import data_root

# 运行时数据根目录：源码运行 = 项目根目录；打包后 = exe 所在目录
# （exe 放到不可写目录时会自动回退到用户目录，具体策略见 utils/paths.py）。
# 统一由 utils.paths 计算，保证日志目录与导出目录始终指向同一个根。
PROJECT_ROOT = data_root()

# 日志目录、输出目录
LOG_DIR = PROJECT_ROOT / "logs"
OUTPUT_DIR = PROJECT_ROOT / "output"

# 日志文件名与记录器名称
LOG_FILE_NAME = "app.log"
LOGGER_NAME = "ip_optimizer"

# 单个日志文件最大 2MB，最多保留 3 个历史文件，避免日志无限变大
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3

_logger: logging.Logger | None = None


def setup_logger(level: int = logging.INFO) -> logging.Logger:
    """创建（或返回已创建的）全局日志记录器。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)

    # 已经有处理器说明初始化过了，直接返回，避免重复输出日志
    if logger.handlers:
        return logger

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1) 写入文件的日志：logs/app.log，UTF-8 编码，自动轮转
    file_handler = RotatingFileHandler(
        LOG_DIR / LOG_FILE_NAME,
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 2) 控制台日志：方便在 VS Code 终端里直接观察运行情况
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


def get_logger() -> logging.Logger:
    """获取全局日志记录器（单例）。其他模块统一通过它来记录日志。"""
    global _logger
    if _logger is None:
        _logger = setup_logger()
    return _logger


def ensure_runtime_dirs() -> None:
    """确保运行时需要的目录（logs、output）存在。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)