"""IP 来源面板。

只负责界面上的「IP 来源」区域：
1. 选择 IP 文件；
2. 粘贴 IP；
3. 导入并校验 IP（调用 core.ip_validator）；
4. 显示统计信息（读取数量 / 有效 IP / 无效 IP 等）。

导入完成后发出 imported 信号，主窗口只需要接收信号即可，
这样主窗口的代码不会太长，职责也更清晰。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from core.ip_loader import IPEntry, IPLoaderError
from core.ip_validator import ImportOutcome, ImportSummary, import_from_file, import_from_text
from gui.fetch_dialog import DEFAULT_COUNT, FetchSettingsDialog
from gui.fetch_worker import FetchWorker, TestConnectionWorker
from utils.logger import get_logger

logger: logging.Logger = get_logger()

# 提示信息里最多展示几条无效原因
MAX_INVALID_SAMPLES = 5


class IPPanel(QGroupBox):
    """IP 来源区域控件。"""

    # 导入完成：参数是 ImportOutcome 对象
    imported = Signal(object)
    # 清空导入内容
    cleared = Signal()
    # 自动获取完成：参数是中文摘要（给主窗口状态栏用）
    fetch_completed = Signal(str)
    # 自动获取失败：中文错误信息
    fetch_failed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__("IP 来源", parent)

        self._ip_file_path: str = ""        # 当前选择的文件路径
        self._valid_entries: List[IPEntry] = []  # 校验通过、可以测速的 IP
        self._fetch_worker: FetchWorker | None = None    # 自动获取后台线程
        self._test_worker: TestConnectionWorker | None = None  # 连接测试后台线程
        self._fetch_dialog: FetchSettingsDialog | None = None  # 获取设置对话框
        self._last_fetch_count: int = DEFAULT_COUNT      # 上次获取使用的数量（刷新时复用）

        self._build_ui()
        self.reset()

    # ==================================================================
    # 界面
    # ==================================================================
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 1) 选择文件
        file_layout = QHBoxLayout()
        self.file_path_edit = QLineEdit()
        self.file_path_edit.setReadOnly(True)
        self.file_path_edit.setPlaceholderText("未选择文件（支持 TXT，每行一个 IP 或 IP:端口）")
        self.choose_file_button = QPushButton("选择IP文件")
        self.choose_file_button.clicked.connect(self._on_choose_file)
        file_layout.addWidget(self.file_path_edit, 1)
        file_layout.addWidget(self.choose_file_button)
        layout.addLayout(file_layout)

        # 2) 粘贴输入
        layout.addWidget(QLabel("或者粘贴 IP（每行一个，支持 IP:端口）："))
        self.ip_text_edit = QPlainTextEdit()
        self.ip_text_edit.setPlaceholderText("104.16.0.1\n104.16.0.2:443\n104.16.0.3")
        self.ip_text_edit.setFixedHeight(110)
        layout.addWidget(self.ip_text_edit)

        # 3) 操作按钮
        button_layout = QHBoxLayout()
        self.import_button = QPushButton("导入IP")
        self.import_button.clicked.connect(self._on_import_ips)
        self.clear_button = QPushButton("清空导入")
        self.clear_button.clicked.connect(self._on_clear_import)
        button_layout.addWidget(self.import_button)
        button_layout.addWidget(self.clear_button)
        self.auto_button = QPushButton("自动获取IP")
        self.auto_button.setToolTip("从 Cloudflare 官方公开 IPv4 网段随机生成候选 IP")
        self.auto_button.clicked.connect(self._on_auto_fetch)
        self.refresh_button = QPushButton("刷新IP")
        self.refresh_button.setToolTip("重新获取 Cloudflare 网段并重新随机生成候选 IP")
        self.refresh_button.clicked.connect(self._on_refresh)
        button_layout.addWidget(self.auto_button)
        button_layout.addWidget(self.refresh_button)
        button_layout.addStretch(1)
        layout.addLayout(button_layout)

        layout.addWidget(
            QLabel("提示：已选择文件时，点击【导入IP】导入文件内容；未选择文件时，导入下面粘贴的内容。")
        )

        # 4) 统计信息
        self.stat_labels: dict[str, QLabel] = {}
        stats_layout = QGridLayout()
        stat_items = (
            ("total", "读取数量"),
            ("unique", "去重后数量"),
            ("duplicate", "重复数量"),
            ("format_error", "格式错误"),
            ("valid", "有效IP"),
            ("invalid", "无效IP"),
        )
        for index, (key, title) in enumerate(stat_items):
            label = QLabel(f"{title}：0")
            self.stat_labels[key] = label
            stats_layout.addWidget(label, index // 3, index % 3)
        layout.addLayout(stats_layout)

        # 5) 自动获取状态（获取完成后显示摘要）
        self.fetch_status_label = QLabel("自动获取：尚未使用（点击【自动获取IP】从 Cloudflare 获取）")
        layout.addWidget(self.fetch_status_label)

    # ==================================================================
    # 对外接口
    # ==================================================================
    @property
    def valid_entries(self) -> List[IPEntry]:
        """当前可以参与测速的 IP 列表。"""
        return self._valid_entries

    @property
    def file_path(self) -> str:
        """当前选择的 IP 文件路径（没有选择时为空字符串）。"""
        return self._ip_file_path

    @property
    def is_fetching(self) -> bool:
        """是否正在自动获取 IP。"""
        return self._fetch_worker is not None and self._fetch_worker.isRunning()

    def reset(self) -> None:
        """清空文件选择、粘贴内容和统计信息。"""
        self._ip_file_path = ""
        self._valid_entries = []
        self.file_path_edit.clear()
        self.file_path_edit.setToolTip("")
        self.ip_text_edit.clear()
        self._update_statistics(ImportSummary())
        self.fetch_status_label.setText("自动获取：尚未使用（点击【自动获取IP】从 Cloudflare 获取）")

    def set_controls_enabled(self, enabled: bool) -> None:
        """测速/获取过程中禁用导入相关控件，避免用户中途修改。"""
        fetching = self.is_fetching
        self.import_button.setEnabled(enabled and not fetching)
        self.clear_button.setEnabled(enabled and not fetching)
        self.choose_file_button.setEnabled(enabled and not fetching)
        self.auto_button.setEnabled(enabled and not fetching)
        self.refresh_button.setEnabled(enabled and not fetching)

    # ==================================================================
    # 自动获取 Cloudflare IP
    # ==================================================================
    def _on_auto_fetch(self) -> None:
        """点击【自动获取IP】：打开设置对话框。"""
        if self.is_fetching:
            QMessageBox.information(self, "提示", "正在获取 IP，请稍候……")
            return
        if self._fetch_dialog is None:
            self._fetch_dialog = FetchSettingsDialog(self)
            self._fetch_dialog.start_requested.connect(self._start_fetch_worker)
            self._fetch_dialog.test_requested.connect(self._start_test_worker)
        self._fetch_dialog.exec()

    def _on_refresh(self) -> None:
        """点击【刷新IP】：用上次设置的数量重新获取（不打开对话框）。"""
        if self.is_fetching:
            QMessageBox.information(self, "提示", "正在获取 IP，请稍候……")
            return
        count = self._last_fetch_count
        logger.info("点击【刷新IP】：重新获取 %s 个候选 IP", count)
        self._start_fetch_worker(count)

    def _start_test_worker(self) -> None:
        """启动「测试Cloudflare连接」的后台线程（只测连接，不生成 IP）。"""
        if self._test_worker is not None and self._test_worker.isRunning():
            return
        self._test_worker = TestConnectionWorker(parent=self)
        self._test_worker.test_finished.connect(self._on_test_finished)
        self._test_worker.finished.connect(self._on_test_thread_finished)
        if self._fetch_dialog is not None:
            self._fetch_dialog.on_test_started()
        logger.info("开始测试 Cloudflare API 连接")
        self._test_worker.start()

    def _on_test_finished(self, result) -> None:
        """连接测试结束：显示结果（成功和失败都在对话框状态区展示）。"""
        if self._fetch_dialog is not None and self._fetch_dialog.isVisible():
            self._fetch_dialog.on_test_finished(result)
        if result.ok:
            logger.info(
                "Cloudflare 连接测试成功：HTTP %s，CIDR %s 个，耗时 %s ms（来源 %s）",
                result.status_code,
                result.cidr_count,
                result.latency_ms,
                result.source,
            )
        else:
            logger.error("Cloudflare 连接测试失败：%s", result.error)

    def _on_test_thread_finished(self) -> None:
        """测试线程结束：释放对象。"""
        worker = self._test_worker
        self._test_worker = None
        if worker is not None:
            worker.deleteLater()

    def _start_fetch_worker(self, count: int) -> None:
        """启动后台获取线程（网络请求不能放在 Qt 主线程）。"""
        if self.is_fetching:
            return

        # 把当前已有的 IP 传给生成器，实现「与现有列表自动去重」
        exclude = {entry.ip for entry in self._valid_entries}
        self._last_fetch_count = count

        self._fetch_worker = FetchWorker(count=count, exclude_ips=exclude, parent=self)
        self._fetch_worker.fetch_finished.connect(self._on_fetch_finished)
        self._fetch_worker.fetch_failed.connect(self._on_fetch_failed)
        self._fetch_worker.finished.connect(self._on_fetch_thread_finished)
        self.set_controls_enabled(False)

        if self._fetch_dialog is not None:
            self._fetch_dialog.on_fetch_started(count)
        self.fetch_status_label.setText(f"自动获取：正在获取 {count} 个候选 IP……")
        self._fetch_worker.start()

    def _on_fetch_finished(self, ips, cidr_count: int, elapsed: float, stats) -> None:
        """获取完成：把新 IP 合并进当前列表（自动去重由生成器保证）。"""
        # 合并：新 IP 追加到现有列表后面
        existing = list(self._valid_entries)
        existing_ips = {entry.ip for entry in existing}
        actually_added = 0
        for ip in ips:
            if ip in existing_ips:
                continue  # 双保险：理论上生成器已去重
            existing.append(IPEntry(ip=ip))
            existing_ips.add(ip)
            actually_added += 1

        self._valid_entries = existing

        # 更新统计标签（读取数量 = 现有总数，有效 IP = 总数）
        summary = ImportSummary(
            total_lines=len(existing),
            unique_count=len(existing),
            valid_count=len(existing),
        )
        self._update_statistics(summary)

        # 更新获取状态显示
        status_text = (
            f"自动获取完成：新增 {actually_added} 个候选 IP"
            f"（Cloudflare 官方，CIDR {cidr_count} 个，耗时 {elapsed:.2f} 秒）"
        )
        self.fetch_status_label.setText(status_text)
        if self._fetch_dialog is not None and self._fetch_dialog.isVisible():
            self._fetch_dialog.on_fetch_finished(ips, cidr_count, elapsed, stats)

        logger.info(status_text)
        self.fetch_completed.emit(status_text)

    def _on_fetch_failed(self, message: str) -> None:
        """获取失败：中文提示，不崩溃。"""
        self.fetch_status_label.setText(f"自动获取失败：{message}")
        if self._fetch_dialog is not None and self._fetch_dialog.isVisible():
            self._fetch_dialog.on_fetch_failed(message)
        logger.error("自动获取 IP 失败：%s", message)
        self.fetch_failed.emit(message)

    def _on_fetch_thread_finished(self) -> None:
        """获取线程结束：恢复按钮状态。"""
        worker = self._fetch_worker
        self._fetch_worker = None
        if worker is not None:
            worker.deleteLater()
        self.set_controls_enabled(True)

    def shutdown_fetch(self) -> None:
        """窗口关闭时安全停止获取线程。"""
        worker = self._fetch_worker
        if worker is not None and worker.isRunning():
            worker.wait(3000)
        test_worker = self._test_worker
        if test_worker is not None and test_worker.isRunning():
            test_worker.wait(3000)

    # ==================================================================
    # 事件处理
    # ==================================================================
    def _on_choose_file(self) -> None:
        """选择 IP 文件。"""
        start_dir = self._ip_file_path or str(Path.home())
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 IP 文件",
            start_dir,
            "文本文件 (*.txt *.csv *.list);;所有文件 (*.*)",
        )
        if not file_path:
            return

        self._ip_file_path = file_path
        self.file_path_edit.setText(file_path)
        self.file_path_edit.setToolTip(file_path)
        logger.info("已选择 IP 文件：%s", file_path)

    def _on_clear_import(self) -> None:
        """清空导入内容。"""
        self.reset()
        logger.info("已清空导入内容")
        self.cleared.emit()

    def _on_import_ips(self) -> None:
        """导入并校验 IP。"""
        try:
            if self._ip_file_path:
                outcome = import_from_file(self._ip_file_path)
                source = f"文件 {self._ip_file_path}"
            else:
                text = self.ip_text_edit.toPlainText()
                if not text.strip():
                    QMessageBox.warning(
                        self, "提示", "请先选择 IP 文件，或在文本框中粘贴 IP（每行一个）。"
                    )
                    return
                outcome = import_from_text(text)
                source = "粘贴内容"
        except IPLoaderError as exc:
            # 文件不存在、编码异常等可预期错误：中文提示，不让程序崩溃
            logger.error("导入 IP 失败：%s", exc)
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        except Exception as exc:  # 兜底：任何异常都不能让程序崩溃
            logger.exception("导入 IP 出现未预期错误")
            QMessageBox.critical(self, "导入失败", f"导入 IP 时发生错误：{exc}")
            return

        self._valid_entries = outcome.valid_entries
        self._update_statistics(outcome.summary)
        logger.info("IP 导入完成（%s）：有效 %s 条", source, outcome.summary.valid_count)

        message = self._format_import_message(outcome)
        if outcome.summary.valid_count == 0:
            QMessageBox.warning(self, "导入完成", message)
        else:
            QMessageBox.information(self, "导入完成", message)

        self.imported.emit(outcome)

    # ==================================================================
    # 内部方法
    # ==================================================================
    def _update_statistics(self, summary: ImportSummary) -> None:
        """刷新统计标签。"""
        self.stat_labels["total"].setText(f"读取数量：{summary.total_lines}")
        self.stat_labels["unique"].setText(f"去重后数量：{summary.unique_count}")
        self.stat_labels["duplicate"].setText(f"重复数量：{summary.duplicate_count}")
        self.stat_labels["format_error"].setText(f"格式错误：{summary.format_error_count}")
        self.stat_labels["valid"].setText(f"有效IP：{summary.valid_count}")
        self.stat_labels["invalid"].setText(f"无效IP：{summary.invalid_count}")

    @staticmethod
    def _format_import_message(outcome: ImportOutcome) -> str:
        """生成导入结果提示文本（包含无效原因示例，方便用户排查）。"""
        lines = [outcome.summary.to_text()]

        invalid_entries = outcome.validation.invalid
        if invalid_entries:
            lines.append("")
            lines.append("无效 IP 示例：")
            for item in invalid_entries[:MAX_INVALID_SAMPLES]:
                lines.append(f"  {item.entry.text} —— {item.reason}")
            if len(invalid_entries) > MAX_INVALID_SAMPLES:
                lines.append(f"  …… 其余 {len(invalid_entries) - MAX_INVALID_SAMPLES} 条已省略")

        for error in outcome.load_result.format_errors[:MAX_INVALID_SAMPLES]:
            lines.append(f"  第 {error.line_number} 行 “{error.text}” —— {error.reason}")

        return "\n".join(lines)