"""「自动获取 Cloudflare IP」设置对话框。

只负责界面：IP 版本提示、候选 IP 数量下拉框、开始获取按钮、
以及获取完成后的状态显示（IP来源 / CIDR数量 / 候选IP / 有效IP / 重复IP / 获取耗时）。

真正的网络请求和 IP 生成在 gui/fetch_worker.py（子线程）里完成，
对话框只接收信号更新显示，所以界面不会卡死。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from utils.logger import get_logger

logger: logging.Logger = get_logger()

# 候选 IP 数量选项（与需求一致），默认 1000
COUNT_CHOICES = (100, 500, 1000, 3000, 5000, 10000)
DEFAULT_COUNT = 1000

COLOR_OK = QColor(21, 115, 71)     # 绿色：成功
COLOR_ERROR = QColor(176, 42, 55)  # 红色：失败


class FetchSettingsDialog(QDialog):
    """获取设置与状态显示对话框。"""

    # 用户点击【开始获取】：参数是候选 IP 数量
    start_requested = Signal(int)
    # 用户点击【测试Cloudflare连接】（由 IPPanel 启动后台测试线程）
    test_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("自动获取 Cloudflare IP")
        self.setMinimumWidth(430)
        self._build_ui()

    # ==================================================================
    # 界面
    # ==================================================================
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 1) 设置区
        form = QFormLayout()
        self.ip_version_label = QLabel("IPv4（第一阶段只支持 IPv4）")
        self.count_combo = QComboBox()
        for value in COUNT_CHOICES:
            self.count_combo.addItem(str(value), value)
        self.count_combo.setCurrentIndex(COUNT_CHOICES.index(DEFAULT_COUNT))
        form.addRow("IP版本：", self.ip_version_label)
        form.addRow("候选IP数量：", self.count_combo)
        layout.addLayout(form)

        hint = QLabel(
            "说明：软件会请求 Cloudflare 官方公开 IPv4 网段，\n"
            "随机生成候选 IP，自动去重并加入当前 IP 列表。\n"
            "获取完成后不会自动开始测速，需要你手动点击【开始测速】。"
        )
        layout.addWidget(hint)

        # 2) 状态区
        self.status_label = QLabel("状态：点击【开始获取】开始")
        layout.addWidget(self.status_label)

        self.result_labels: dict[str, QLabel] = {}
        result_items = (
            ("source", "IP来源：—"),
            ("cidr", "CIDR数量：—"),
            ("candidate", "候选IP：—"),
            ("valid", "有效IP：—"),
            ("duplicate", "重复IP：—"),
            ("elapsed", "获取耗时：—"),
        )
        for key, text in result_items:
            label = QLabel(text)
            self.result_labels[key] = label
            layout.addWidget(label)

        # 3) 按钮
        buttons = QHBoxLayout()
        self.start_button = QPushButton("开始获取")
        self.start_button.clicked.connect(self._on_start)
        self.test_button = QPushButton("测试Cloudflare连接")
        self.test_button.setToolTip("只测试 Cloudflare API 是否可以访问，不生成 IP")
        self.test_button.clicked.connect(self._on_test)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.reject)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.test_button)
        buttons.addWidget(close_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    # ==================================================================
    # 对外接口（由 IPPanel 调用）
    # ==================================================================
    def _on_start(self) -> None:
        """用户点击【开始获取】：把选中的数量通过信号发给 IPPanel。"""
        count = self.selected_count()
        logger.info("用户点击【开始获取】：候选 IP 数量 %s", count)
        self.start_requested.emit(count)

    def _on_test(self) -> None:
        """用户点击【测试Cloudflare连接】：通过信号让 IPPanel 启动测试线程。"""
        logger.info("用户点击【测试Cloudflare连接】")
        self.test_requested.emit()

    def selected_count(self) -> int:
        """当前选择的候选 IP 数量。"""
        return self.count_combo.currentData() or DEFAULT_COUNT

    def set_running(self, running: bool) -> None:
        """获取进行中：禁用开始按钮和数量选择。"""
        self.start_button.setEnabled(not running)
        self.count_combo.setEnabled(not running)

    def set_test_running(self, running: bool) -> None:
        """连接测试进行中：禁用测试按钮。"""
        self.test_button.setEnabled(not running)

    def on_fetch_started(self, count: int) -> None:
        """开始获取（子线程已启动）。"""
        self.set_running(True)
        self.status_label.setText("状态：正在请求 Cloudflare 官方 IP 数据，请稍候……")
        self.status_label.setStyleSheet("")

    def on_fetch_finished(self, ips, cidr_count: int, elapsed: float, stats) -> None:
        """获取完成：显示统计信息。"""
        self.set_running(False)
        self.status_label.setText(f"状态：获取完成，新增 {len(ips)} 个候选 IP，已加入当前 IP 列表")
        self.status_label.setStyleSheet(f"color: {COLOR_OK.name()};")

        self.result_labels["source"].setText("IP来源：Cloudflare 官方")
        self.result_labels["cidr"].setText(f"CIDR数量：{cidr_count}")
        self.result_labels["candidate"].setText(f"候选IP：{stats.requested_count}")
        self.result_labels["valid"].setText(f"有效IP：{len(ips)}")
        self.result_labels["duplicate"].setText(f"重复IP：{stats.duplicate_count}")
        self.result_labels["elapsed"].setText(f"获取耗时：{elapsed:.2f} 秒")

    def on_fetch_failed(self, message: str) -> None:
        """获取失败：显示中文错误。"""
        self.set_running(False)
        self.status_label.setText(f"状态：{message}")
        self.status_label.setStyleSheet(f"color: {COLOR_ERROR.name()};")
        QMessageBox.critical(self, "获取失败", message)

    # ------------------------------------------------------------------
    # 连接测试的状态显示
    # ------------------------------------------------------------------
    def on_test_started(self) -> None:
        """连接测试开始（测试线程已启动）。"""
        self.set_test_running(True)
        self.status_label.setText("状态：正在测试 Cloudflare API 连接，请稍候……")
        self.status_label.setStyleSheet("")

    def on_test_finished(self, result) -> None:
        """连接测试结束：显示成功或失败信息（result 是 ApiTestResult）。"""
        self.set_test_running(False)
        if result.ok:
            source_text = "主源(JSON API)" if result.source == "api" else "备用源(官方文本)"
            self.status_label.setText(
                f"状态：Cloudflare API 连接正常"
                f"（HTTP {result.status_code}，IPv4 CIDR {result.cidr_count} 个，"
                f"耗时 {result.latency_ms} ms，来源：{source_text}）"
            )
            self.status_label.setStyleSheet(f"color: {COLOR_OK.name()};")
        else:
            self.status_label.setText(f"状态：连接失败：{result.error}")
            self.status_label.setStyleSheet(f"color: {COLOR_ERROR.name()};")