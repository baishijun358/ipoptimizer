"""主窗口。

负责界面布局和用户交互：
1. 导入 IP（文件 / 粘贴）并显示统计信息；
2. 设置端口、并发、超时；
3. 开始 / 停止测速，显示进度；
4. 显示结果（按延迟排序）。

所有耗时的网络操作都交给 ScanWorker 线程执行，主线程只负责刷新界面，
所以测速过程中窗口依然可以正常拖动、点击，不会出现“未响应”。
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.ip_loader import IPEntry
from core.ranking import (
    DEFAULT_TOP_N,
    LATENCY_FILTER_OPTIONS,
    SPEED_FILTER_OPTIONS,
    TOP_OPTIONS,
    build_final_ranking,
    build_ranking,
)
from core.scanner import (
    DEFAULT_CONCURRENCY,
    DEFAULT_DOWNLOAD_BYTES,
    DEFAULT_DOWNLOAD_TIMEOUT_MS,
    DEFAULT_HTTP_TIMEOUT_MS,
    DEFAULT_TIMEOUT_MS,
    MAX_CONCURRENCY,
    MAX_DOWNLOAD_TIMEOUT_MS,
    MAX_HTTP_TIMEOUT_MS,
    MAX_TIMEOUT_MS,
    MIN_CONCURRENCY,
    MIN_DOWNLOAD_TIMEOUT_MS,
    MIN_HTTP_TIMEOUT_MS,
    MIN_TIMEOUT_MS,
    StageStats,
    ScanSummary,
)
from core.stability import StabilityData  # V1.4：稳定性复测统计数据结构
from core.tcp_tester import TestResult
from gui.ip_panel import IPPanel
from gui.result_table import ResultTable
from gui.scan_worker import ScanWorker
from gui.stability_worker import StabilityWorker  # V1.4：稳定性复测线程
from utils.export import (
    ExportError,
    export_csv,
    export_stable_csv,
    export_stable_txt,
    export_txt,
)
from utils.logger import get_logger

logger: logging.Logger = get_logger()

APP_TITLE = "数码解码 IP 优选器 V1.4"

# 结果批量刷新间隔（毫秒）：测速时先把结果攒起来，定时批量写入表格，界面更流畅
RESULT_FLUSH_INTERVAL_MS = 300

# 用时刷新间隔（毫秒）：即使某个批次都在等待超时，界面上的“用时”也会持续走动
ELAPSED_REFRESH_INTERVAL_MS = 200

DEFAULT_PORT = 443

# 下载大小的下拉选项（文本，字节）：方便初学者直接选择，不用自己换算
DOWNLOAD_SIZE_OPTIONS = (
    ("256 KB", 256 * 1024),
    ("512 KB", 512 * 1024),
    ("1 MB", 1024 * 1024),
    ("2 MB", 2 * 1024 * 1024),
    ("5 MB", 5 * 1024 * 1024),
)

# V1.4：稳定性复测的选项（复测轮数 3/5/10，默认 5；复测并发 10/20/50，默认 20）
STABILITY_ROUND_OPTIONS = (3, 5, 10)
DEFAULT_STABILITY_ROUNDS = 5
STABILITY_CONCURRENCY_OPTIONS = (10, 20, 50)
DEFAULT_STABILITY_CONCURRENCY = 20
# 稳定性评分达到该值视为“稳定”（结果摘要里统计稳定/波动数量用）
STABLE_SCORE_THRESHOLD = 60


class MainWindow(QMainWindow):
    """程序主窗口。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self.setWindowTitle(APP_TITLE)
        self.resize(1080, 860)
        self.setMinimumSize(900, 660)

        # 运行时数据
        self._valid_entries: List[IPEntry] = []           # 导入并校验通过、可测速的 IP
        self._pending_results: List[TestResult] = []      # 等待写入表格的结果
        self._all_results: List[TestResult] = []          # 本轮测速的全部结果
        self._worker: Optional[ScanWorker] = None         # 测速线程
        self._scan_start_time: float = 0.0                # 测速开始时间
        self._quit_timer: Optional[QTimer] = None         # 退出前检查线程是否结束
        self._running = False                             # 是否正在主测速（判断控件可用性用）
        # V1.4 复测/排名运行时数据
        self._stability_worker: Optional[StabilityWorker] = None   # 复测线程
        self._stability_map: Dict[str, StabilityData] = {}         # {ip: StabilityData}
        self._stability_running = False                            # 复测是否进行中
        self._stab_results_all: List[TestResult] = []              # 已完成轮次的原始结果（复测出现问题时供复查用）
        self._stab_rounds_total = 0                                # 本次复测计划轮数（进度显示用）
        self._stab_ip_total = 0                                    # 本次复测目标 IP 数（进度显示用）

        # 定时器：批量刷新结果表格
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(RESULT_FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush_results)

        # 定时器：测速过程中持续刷新“用时”
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(ELAPSED_REFRESH_INTERVAL_MS)
        self._elapsed_timer.timeout.connect(self._refresh_elapsed)

        self._build_ui()
        logger.info("软件启动：主窗口创建完成")

    # ==================================================================
    # 界面搭建
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget()  # V1.4：作为滚动区的子页面（不能提前指定父对象）
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        # 顶部标题
        title_label = QLabel(APP_TITLE)
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root_layout.addWidget(title_label)

        # IP 来源区域：单独放在 gui/ip_panel.py 里，主窗口只负责接收信号
        self.ip_panel = IPPanel()
        self.ip_panel.imported.connect(self._on_ips_imported)
        self.ip_panel.cleared.connect(self._on_ips_cleared)
        self.ip_panel.fetch_completed.connect(self._on_fetch_completed)
        self.ip_panel.fetch_failed.connect(self._on_fetch_failed)
        root_layout.addWidget(self.ip_panel)

        root_layout.addWidget(self._build_setting_group())
        root_layout.addWidget(self._build_progress_group())
        root_layout.addWidget(self._build_stability_group())      # V1.4：稳定性复测
        root_layout.addWidget(self._build_result_group(), 1)  # 结果区域占据剩余空间

        # V1.4：界面内容变多（多了一个复测区），小屏幕上可能放不下，
        # 用滚动区包裹，窗口再小也能滚动查看，结果表格仍可随窗口拉伸。
        from PySide6.QtWidgets import QScrollArea
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(central)
        self.setCentralWidget(scroll)
        self.statusBar().showMessage("就绪：请先导入 IP")

    def _build_setting_group(self) -> QGroupBox:
        """测速设置区域（V1.2：新增 HTTP 测试与下载测速的设置）。"""
        group = QGroupBox("测速设置")
        layout = QHBoxLayout(group)

        # ---- 左半部分：第一级 TCP 测试设置（保持第一阶段原样） ----
        tcp_form = QFormLayout()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(DEFAULT_PORT)
        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(MIN_CONCURRENCY, MAX_CONCURRENCY)
        self.concurrency_spin.setValue(DEFAULT_CONCURRENCY)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(MIN_TIMEOUT_MS, MAX_TIMEOUT_MS)
        self.timeout_spin.setValue(DEFAULT_TIMEOUT_MS)
        self.timeout_spin.setSuffix(" ms")
        tcp_form.addRow("端口：", self.port_spin)
        tcp_form.addRow("并发：", self.concurrency_spin)
        tcp_form.addRow("TCP超时：", self.timeout_spin)
        layout.addLayout(tcp_form)

        # ---- 右半部分：第二级 HTTP + 第三级 下载（V1.2 新增） ----
        stage_form = QFormLayout()

        # HTTP 测试开关（TCP 成功后才执行）
        self.http_checkbox = QCheckBox("启用 HTTP 测试")
        self.http_checkbox.setChecked(True)
        self.http_checkbox.setToolTip("TCP 连接成功后，再测试 HTTP/HTTPS 是否真的能通")
        self.http_checkbox.toggled.connect(self._on_http_toggled)

        self.http_timeout_spin = QSpinBox()
        self.http_timeout_spin.setRange(MIN_HTTP_TIMEOUT_MS, MAX_HTTP_TIMEOUT_MS)
        self.http_timeout_spin.setValue(DEFAULT_HTTP_TIMEOUT_MS)
        self.http_timeout_spin.setSuffix(" ms")

        # 下载测速开关（HTTP 成功后才执行）
        self.download_checkbox = QCheckBox("启用下载测速")
        self.download_checkbox.setChecked(True)
        self.download_checkbox.setToolTip("HTTP 测试成功后，再下载一小段数据来测量真实速度")
        self.download_checkbox.toggled.connect(self._on_download_toggled)

        self.download_size_combo = QComboBox()
        for text, size in DOWNLOAD_SIZE_OPTIONS:
            self.download_size_combo.addItem(text, size)
        # 默认选中 1 MB
        default_index = next(
            (
                index
                for index, (_, size) in enumerate(DOWNLOAD_SIZE_OPTIONS)
                if size == DEFAULT_DOWNLOAD_BYTES
            ),
            2,
        )
        self.download_size_combo.setCurrentIndex(default_index)

        self.download_timeout_spin = QSpinBox()
        self.download_timeout_spin.setRange(MIN_DOWNLOAD_TIMEOUT_MS, MAX_DOWNLOAD_TIMEOUT_MS)
        self.download_timeout_spin.setValue(DEFAULT_DOWNLOAD_TIMEOUT_MS)
        self.download_timeout_spin.setSuffix(" ms")

        stage_form.addRow(self.http_checkbox)
        stage_form.addRow("HTTP超时：", self.http_timeout_spin)
        stage_form.addRow(self.download_checkbox)
        stage_form.addRow("下载大小：", self.download_size_combo)
        stage_form.addRow("下载超时：", self.download_timeout_spin)
        layout.addLayout(stage_form)

        button_layout = QVBoxLayout()
        self.start_button = QPushButton("开始测速")
        self.start_button.clicked.connect(self._on_start_scan)
        self.stop_button = QPushButton("停止测速")
        self.stop_button.clicked.connect(self._on_stop_scan)
        self.stop_button.setEnabled(False)
        button_layout.addWidget(self.start_button)
        button_layout.addWidget(self.stop_button)
        layout.addLayout(button_layout)

        layout.addStretch(1)

        # 根据开关的初始状态，同步子控件的可用性
        self._on_http_toggled(self.http_checkbox.isChecked())
        self._on_download_toggled(self.download_checkbox.isChecked())
        return group

    def _on_http_toggled(self, checked: bool) -> None:
        """HTTP 开关变化：联动 HTTP 超时输入框。"""
        # 测速/复测进行中不允许改动设置；关闭 HTTP 测试时，下载测速也一定不会执行
        editable = checked and not self._running and not self._stability_running
        self.http_timeout_spin.setEnabled(editable)
        self.download_checkbox.setEnabled(checked and not self._running and not self._stability_running)
        self._on_download_toggled(self.download_checkbox.isChecked())

    def _on_download_toggled(self, checked: bool) -> None:
        """下载开关变化：联动下载大小和超时输入框。"""
        enabled = (
            checked and self.http_checkbox.isChecked()
            and not self._running and not self._stability_running
        )
        self.download_size_combo.setEnabled(enabled)
        self.download_timeout_spin.setEnabled(enabled)

    def _build_progress_group(self) -> QGroupBox:
        """测试进度区域。"""
        group = QGroupBox("测试进度")
        layout = QVBoxLayout(group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        layout.addWidget(self.progress_bar)

        info_layout = QHBoxLayout()
        self.tested_label = QLabel("已测试：0 / 0")
        self.success_label = QLabel("成功：0")
        self.failed_label = QLabel("失败：0")
        self.elapsed_label = QLabel("用时：0.0 秒")
        for label in (self.tested_label, self.success_label, self.failed_label, self.elapsed_label):
            info_layout.addWidget(label)
        info_layout.addStretch(1)
        layout.addLayout(info_layout)

        # ---- V1.2 新增：三级测试各自的成功 / 失败统计 ----
        stage_layout = QHBoxLayout()
        self.stage_label = QLabel("阶段统计：")
        self.tcp_stat_label = QLabel("TCP 成功 0 / 失败 0")
        self.http_stat_label = QLabel("HTTP 成功 0 / 失败 0 / 未测试 0")
        self.download_stat_label = QLabel("下载 成功 0 / 失败 0 / 未测试 0")
        for label in (
            self.stage_label,
            self.tcp_stat_label,
            self.http_stat_label,
            self.download_stat_label,
        ):
            stage_layout.addWidget(label)
        stage_layout.addStretch(1)
        layout.addLayout(stage_layout)

        return group

    # ==================================================================
    # V1.4：稳定性复测（对 V1.3 TOP IP 多次重复测试，算稳定性评分与最终排名）
    # ==================================================================
    def _build_stability_group(self) -> QGroupBox:
        """稳定性复测区域：复测轮数/并发选择、开始/停止按钮、进度与当前状态。

        只在主测速结束且有 V1.3 排名后才允许开始（按钮禁用逻辑见
        _set_running_state / _on_scan_finished）。
        """
        group = QGroupBox("稳定性复测（对 TOP IP 多次重复测试，取稳定者优先）")
        layout = QVBoxLayout(group)

        # ---- 选项行：复测轮数 + 复测并发 ----
        option_layout = QHBoxLayout()
        option_layout.addWidget(QLabel("复测轮数："))
        self.stab_rounds_combo = QComboBox()
        for n in STABILITY_ROUND_OPTIONS:
            self.stab_rounds_combo.addItem(f"{n} 次", n)
        default_round_index = next(
            (i for i, n in enumerate(STABILITY_ROUND_OPTIONS) if n == DEFAULT_STABILITY_ROUNDS),
            1,
        )
        self.stab_rounds_combo.setCurrentIndex(default_round_index)
        self.stab_rounds_combo.setToolTip("同一个 TOP IP 集合重复测试几次（次数越多结论越稳，但用时越长）")
        option_layout.addWidget(self.stab_rounds_combo)

        option_layout.addWidget(QLabel("复测并发："))
        self.stab_concurrency_combo = QComboBox()
        for n in STABILITY_CONCURRENCY_OPTIONS:
            self.stab_concurrency_combo.addItem(str(n), n)
        default_conc_index = next(
            (i for i, n in enumerate(STABILITY_CONCURRENCY_OPTIONS)
             if n == DEFAULT_STABILITY_CONCURRENCY),
            1,
        )
        self.stab_concurrency_combo.setCurrentIndex(default_conc_index)
        self.stab_concurrency_combo.setToolTip("复测时同时测试几个 IP（越大越快，但对网络压力越大）")
        option_layout.addWidget(self.stab_concurrency_combo)

        # ---- 开始 / 停止按钮 ----
        self.stab_start_button = QPushButton("开始复测")
        self.stab_start_button.setToolTip("对当前 V1.3 排名的 TOP IP 进行多轮复测")
        self.stab_start_button.clicked.connect(self._on_stability_start)
        self.stab_start_button.setEnabled(False)  # 主测速结束前不可用
        option_layout.addWidget(self.stab_start_button)

        self.stab_stop_button = QPushButton("停止复测")
        self.stab_stop_button.setToolTip("停止复测（已完成的轮次结果会保留）")
        self.stab_stop_button.clicked.connect(self._on_stability_stop)
        self.stab_stop_button.setEnabled(False)  # 复测进行中才可用
        option_layout.addWidget(self.stab_stop_button)
        option_layout.addStretch(1)
        layout.addLayout(option_layout)

        # ---- 进度条 ----
        self.stab_progress_bar = QProgressBar()
        self.stab_progress_bar.setRange(0, 100)
        self.stab_progress_bar.setValue(0)
        self.stab_progress_bar.setFormat("%p%")
        layout.addWidget(self.stab_progress_bar)

        # ---- 状态行：当前轮次/当前IP/统计 ----
        status_layout = QHBoxLayout()
        self.stab_status_label = QLabel("尚未复测")
        self.stab_current_label = QLabel("当前：--")
        self.stab_summary_label = QLabel("")
        # 摘要可能很长（稳定数/平均稳定性/TOP10），允许自动换行避免撑宽窗口
        self.stab_summary_label.setWordWrap(True)
        for label in (self.stab_status_label, self.stab_current_label, self.stab_summary_label):
            status_layout.addWidget(label)
        status_layout.addStretch(1)
        layout.addLayout(status_layout)

        # ---- 稳定结果导出按钮行 ----
        export_layout = QHBoxLayout()
        self.export_stable_txt_button = QPushButton("导出稳定TOP TXT")
        self.export_stable_txt_button.setToolTip("导出最终排名前 100 的 IP（每行一个）到 output\\ 目录")
        self.export_stable_txt_button.clicked.connect(self._on_export_stable_txt)
        self.export_stable_txt_button.setEnabled(False)  # 复测完成后才有数据
        export_layout.addWidget(self.export_stable_txt_button)

        self.export_stable_csv_button = QPushButton("导出稳定性CSV")
        self.export_stable_csv_button.setToolTip("导出稳定性复测的完整统计（含稳定性/最终评分）到 output\\ 目录")
        self.export_stable_csv_button.clicked.connect(self._on_export_stable_csv)
        self.export_stable_csv_button.setEnabled(False)  # 复测完成后才有数据
        export_layout.addWidget(self.export_stable_csv_button)
        export_layout.addStretch(1)
        layout.addLayout(export_layout)

        return group


    def _build_result_group(self) -> QGroupBox:
        """测试结果区域（V1.3：排名/评分/筛选/复制/导出）。"""
        group = QGroupBox("测试结果（按综合评分排名：下载速度权重最高，延迟越低越好）")
        layout = QVBoxLayout(group)

        # ---- V1.3 新增：筛选与 TOP 设置行 ----
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("最低速度："))
        self.speed_filter_combo = QComboBox()
        for text, _value in SPEED_FILTER_OPTIONS:
            self.speed_filter_combo.addItem(text)
        self.speed_filter_combo.setToolTip("只有下载速度不低于该值的 IP 才进入最终排名")
        filter_layout.addWidget(self.speed_filter_combo)

        filter_layout.addWidget(QLabel("最大TCP延迟："))
        self.latency_filter_combo = QComboBox()
        for text, _value in LATENCY_FILTER_OPTIONS:
            self.latency_filter_combo.addItem(text)
        self.latency_filter_combo.setToolTip("只有 TCP 延迟不超过该值的 IP 才进入最终排名")
        filter_layout.addWidget(self.latency_filter_combo)

        filter_layout.addWidget(QLabel("显示："))
        self.top_combo = QComboBox()
        for n in TOP_OPTIONS:
            self.top_combo.addItem(f"TOP {n}", n)
        # 默认选中 TOP 100
        default_top_index = next(
            (i for i, n in enumerate(TOP_OPTIONS) if n == DEFAULT_TOP_N), len(TOP_OPTIONS) - 1
        )
        self.top_combo.setCurrentIndex(default_top_index)
        filter_layout.addWidget(self.top_combo)

        self.apply_filter_button = QPushButton("应用筛选")
        self.apply_filter_button.setToolTip("按上面的条件重新计算排名（测速结束后可用）")
        self.apply_filter_button.clicked.connect(self._apply_ranking_from_ui)
        filter_layout.addWidget(self.apply_filter_button)
        filter_layout.addStretch(1)
        layout.addLayout(filter_layout)

        self.result_table = ResultTable()
        layout.addWidget(self.result_table)

        # ---- V1.3 新增：复制与导出按钮行 ----
        button_layout = QHBoxLayout()
        self.copy_top10_button = QPushButton("复制TOP10")
        self.copy_top50_button = QPushButton("复制TOP50")
        self.copy_top100_button = QPushButton("复制TOP100")
        self.export_txt_button = QPushButton("导出TXT")
        self.export_csv_button = QPushButton("导出CSV")
        for n, button in ((10, self.copy_top10_button), (50, self.copy_top50_button), (100, self.copy_top100_button)):
            button.setToolTip(f"复制前 {n} 名 IP（每行一个，不带其他文字）")
            button.clicked.connect(lambda _checked=False, count=n: self._copy_top(count))
        self.export_txt_button.setToolTip("导出 TOP100 的 IP 列表到 output\\ 目录（每行一个 IP）")
        self.export_csv_button.setToolTip("导出完整结果（含排名/延迟/状态/评分）到 output\\ 目录")
        self.export_txt_button.clicked.connect(self._on_export_txt)
        self.export_csv_button.clicked.connect(self._on_export_csv)
        for button in (
            self.copy_top10_button, self.copy_top50_button, self.copy_top100_button,
            self.export_txt_button, self.export_csv_button,
        ):
            button.setEnabled(False)  # 测速结束后才有数据
            button_layout.addWidget(button)
        button_layout.addStretch(1)
        layout.addLayout(button_layout)

        self.result_summary_label = QLabel("暂无结果")
        layout.addWidget(self.result_summary_label)

        return group

    def _copy_top(self, count: int) -> None:
        """复制前 N 名 IP 到剪贴板（每行一个，不带其他文字）。

        V1.4：复测完成后优先按最终排名复制（稳定者优先），
        否则按 V1.3 排名复制（见 ResultTable.top_ips）。
        """
        ips = self.result_table.top_ips(count)
        if not ips:
            QMessageBox.information(self, "提示", "还没有可复制的 IP，请先完成测速。")
            return
        QApplication.clipboard().setText("\n".join(ips))
        self.statusBar().showMessage(f"已复制 TOP{len(ips)} 共 {len(ips)} 个 IP 到剪贴板")
        logger.info("用户复制 TOP%s：%s 个 IP", count, len(ips))

    def _on_export_txt(self) -> None:
        """导出 TXT（V1.3 排名 TOP100，每行一个 IP）。

        V1.4：想要「按最终排名（稳定者优先）」的列表请用稳定性区域的
        【导出稳定TOP TXT】按钮（见 _on_export_stable_txt）。
        """
        try:
            path = export_txt(self.result_table.rank_entries, top_n=DEFAULT_TOP_N)
        except ExportError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出成功", f"TOP TXT 已保存到：\n{path}")
        logger.info("用户导出 TOP TXT：%s", path)

    def _on_export_csv(self) -> None:
        """导出 CSV（V1.3 完整字段）。

        V1.4：想要含稳定性/最终评分的完整统计请用稳定性区域的
        【导出稳定性CSV】按钮（见 _on_export_stable_csv）。
        """
        try:
            path = export_csv(self.result_table.rank_entries)
        except ExportError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出成功", f"CSV 已保存到：\n{path}")
        logger.info("用户导出 CSV：%s", path)

    # ------------------------------------------------------------------
    # V1.3：评分 / 排名 / 筛选 / 摘要
    # ------------------------------------------------------------------
    def _current_filters(self) -> dict:
        """读取界面上的筛选条件。"""
        speed_text = self.speed_filter_combo.currentText()
        latency_text = self.latency_filter_combo.currentText()
        min_speed_bps = next((v for t, v in SPEED_FILTER_OPTIONS if t == speed_text), None)
        max_latency = next((v for t, v in LATENCY_FILTER_OPTIONS if t == latency_text), None)
        return {"min_speed_bps": min_speed_bps, "max_tcp_latency_ms": max_latency}

    def _apply_ranking_from_ui(self) -> None:
        """按界面筛选条件重建排名（需要已经完成过测速）。"""
        if not self._all_results:
            QMessageBox.information(self, "提示", "还没有测速结果，请先完成一次测速。")
            return
        self._refresh_ranking_view()

    def _refresh_ranking_view(self) -> None:
        """用当前筛选条件重新评分排名，并刷新表格与摘要。"""
        filters = self._current_filters()
        top_n = self.top_combo.currentData() or DEFAULT_TOP_N
        entries = build_ranking(
            self._all_results,
            min_speed_bps=filters["min_speed_bps"],
            max_tcp_latency_ms=filters["max_tcp_latency_ms"],
            top_n=top_n,
        )
        self.result_table.set_ranking(entries)
        self.result_table.enable_sorting()
        self._update_summary_text(entries)
        # V1.4：V1.3 排名一旦变化，旧的最终排名与复测汇总即失效
        # （result_table.set_ranking 已清空最终排名快照，这里同步清空复测汇总）
        self._stability_map = {}
        self._stab_results_all = []
        self.export_stable_txt_button.setEnabled(False)
        self.export_stable_csv_button.setEnabled(False)
        self.stab_status_label.setText("尚未复测")
        self.stab_summary_label.setText("")
        self._refresh_stability_controls()

    def _update_summary_text(self, entries) -> None:
        """刷新结果摘要（含最快/平均速度、最低/平均延迟、TOP1）。"""
        valid = [e for e in entries if e.score is not None]
        if not valid:
            self.result_summary_label.setText("没有满足条件的有效 IP，可放宽筛选条件后重试")
            return
        speeds = [e.result.download_speed_bps for e in valid if e.result.download_speed_bps]
        tcp_latencies = [e.result.latency for e in valid if e.result.latency is not None]
        http_latencies = [e.result.http_latency for e in valid if e.result.http_latency is not None]
        top1 = valid[0]
        parts = [f"有效IP：{len(valid)}"]
        if speeds:
            parts.append(f"最快速度：{max(speeds) / 1048576:.2f} MB/s")
            parts.append(f"平均速度：{sum(speeds) / len(speeds) / 1048576:.2f} MB/s")
        if tcp_latencies:
            parts.append(f"最低延迟：{min(tcp_latencies)} ms")
            parts.append(f"平均TCP延迟：{sum(tcp_latencies) / len(tcp_latencies):.0f} ms")
        if http_latencies:
            parts.append(f"平均HTTP延迟：{sum(http_latencies) / len(http_latencies):.0f} ms")
        parts.append(f"TOP1：{top1.result.ip}（评分 {top1.score}）")
        self.result_summary_label.setText("　|　".join(parts))

    def _set_export_buttons_enabled(self, enabled: bool) -> None:
        """测速结束后才允许复制/导出。"""
        for button in (
            self.copy_top10_button, self.copy_top50_button, self.copy_top100_button,
            self.export_txt_button, self.export_csv_button,
        ):
            button.setEnabled(enabled)

    # ------------------------------------------------------------------
    # V1.4：稳定性复测的目标 / 启动 / 停止 / 进度
    # ------------------------------------------------------------------
    def _stability_targets(self) -> List[IPEntry]:
        """本次复测的目标 IP：当前 V1.3 排名里「可评分」的条目（已有 TOP N 截断）。

        失败 IP（rank=0 / score=None）不参与复测。
        """
        targets: List[IPEntry] = []
        for entry in self.result_table.rank_entries:
            if entry.score is None or entry.rank <= 0:
                continue
            targets.append(IPEntry(ip=entry.result.ip, port=entry.result.port))
        return targets

    def _on_stability_start(self) -> None:
        """开始稳定性复测（对当前 V1.3 排名的 TOP IP 做多轮重复测试）。"""
        if self._stability_running:
            return
        if self._running:
            QMessageBox.information(self, "提示", "测速正在进行，请等待结束或点击【停止测速】。")
            return
        if self._stability_worker is not None and self._stability_worker.isRunning():
            return
        targets = self._stability_targets()
        if not targets:
            QMessageBox.information(
                self, "提示",
                "当前没有可复测的 IP。\n\n请先完成一次测速，且排名中有评分有效的 IP。",
            )
            return

        rounds = self.stab_rounds_combo.currentData() or DEFAULT_STABILITY_ROUNDS
        concurrency = self.stab_concurrency_combo.currentData() or DEFAULT_STABILITY_CONCURRENCY
        self._stab_rounds_total = int(rounds)
        self._stab_ip_total = len(targets)

        # 复测参数与主测速保持一致（端口/超时/开关/下载大小全部来自界面）
        download_bytes = self.download_size_combo.currentData()
        if download_bytes is None:
            download_bytes = DEFAULT_DOWNLOAD_BYTES

        # 旧的复测汇总作废（表格仍保留 V1.3 排名显示，直到本轮复测完成）
        self._stability_map = {}
        self._stab_results_all = []
        self.export_stable_txt_button.setEnabled(False)
        self.export_stable_csv_button.setEnabled(False)

        self._stability_worker = StabilityWorker(
            targets,
            rounds=self._stab_rounds_total,
            concurrency=int(concurrency),
            port=self.port_spin.value(),
            timeout_ms=self.timeout_spin.value(),
            http_enabled=self.http_checkbox.isChecked(),
            http_timeout_ms=self.http_timeout_spin.value(),
            download_enabled=self.download_checkbox.isChecked(),
            download_bytes=int(download_bytes),
            download_timeout_ms=self.download_timeout_spin.value(),
            parent=self,
        )
        self._stability_worker.item_progress.connect(self._on_stability_item_progress)
        self._stability_worker.round_finished.connect(self._on_stability_round_finished)
        self._stability_worker.stability_finished.connect(self._on_stability_finished)
        self._stability_worker.stability_failed.connect(self._on_stability_failed)
        self._stability_worker.finished.connect(self._on_stability_worker_thread_finished)

        self._set_stability_running_state(True)
        self._stability_worker.start()
        logger.info(
            "用户点击【开始复测】，目标 %s 个 IP，%s 轮，并发 %s",
            self._stab_ip_total, self._stab_rounds_total, concurrency,
        )
        self.statusBar().showMessage("稳定性复测进行中……")

    def _on_stability_stop(self) -> None:
        """请求停止复测（已完成的轮次结果会保留并参与最终排名）。"""
        worker = self._stability_worker
        if worker is None or not worker.isRunning():
            return
        self.stab_start_button.setEnabled(False)
        self.stab_stop_button.setEnabled(False)
        self.stab_status_label.setText("正在停止复测，请稍候……")
        self.statusBar().showMessage("正在停止复测，请稍候……")
        logger.info("用户点击了停止复测")
        worker.request_stop()
        # V1.4：线程尚未完全结束就释放 `_stability_worker` 变量，不过 Qt 对象的
        # `finished` 信号还能触发（内部 worker 对象尚未析构），最终排名还是能算出，
        # 只是进度条等UI控件随后随线程生命周期一起销毁。这里算是“最保守”的处理方式。

    def _on_stability_item_progress(
        self, round_index: int, tested: int, total: int, current_ip: str
    ) -> None:
        """复测进度：按「已完成 IP 数 / 全部轮次总 IP 数」折算成总进度。"""
        rounds_total = max(1, self._stab_rounds_total)
        grand_total = max(1, rounds_total * max(1, self._stab_ip_total))
        done = (max(0, round_index - 1)) * max(1, self._stab_ip_total) + tested
        percent = int(min(100, max(0, done * 100 / grand_total)))
        self.stab_progress_bar.setValue(percent)
        self.stab_status_label.setText(
            f"复测中：第 {round_index}/{rounds_total} 轮，已测 {tested}/{total}"
        )
        if current_ip:
            self.stab_current_label.setText(f"当前：{current_ip}")

    def _on_stability_round_finished(self, round_index: int, results) -> None:
        """一轮复测结束：缓存该轮原始结果，供后续排查与复核。"""
        if results:
            self._stab_results_all.extend(list(results))
        self.stab_status_label.setText(
            f"第 {round_index} 轮完成（已缓存 {len(self._stab_results_all)} 条结果）"
        )

    def _on_stability_finished(self, stability_map, stopped: bool, elapsed: float) -> None:
        """全部复测结束：算最终排名并刷新表格/摘要/导出按钮。"""
        self._stability_map = dict(stability_map) if stability_map else {}
        entries = build_final_ranking(self.result_table.rank_entries, self._stability_map)
        has_final = bool(entries)
        if has_final:
            self.result_table.set_final_ranking(entries)
            self.result_table.enable_sorting()
        self.export_stable_txt_button.setEnabled(has_final)
        self.export_stable_csv_button.setEnabled(has_final)
        self._refresh_stability_summary(stopped, elapsed)
        self._set_stability_running_state(False)

        if not self._stability_map or not has_final:
            QMessageBox.information(
                self, "复测无有效结果",
                "复测完成了，但没有拿到可用的复测数据，结果表格保持 V1.3 排名不变。\n\n"
                "可能原因：网络中断、目标 IP 全部超时，或复测开始前就被停止。",
            )
            self.statusBar().showMessage("复测结束：无有效结果")
            return
        if stopped:
            QMessageBox.information(
                self, "已停止复测",
                f"复测已停止（用时 {elapsed:.1f} 秒），已用完成的轮次生成最终排名。\n\n"
                f"{self.stab_summary_label.text()}",
            )
            self.statusBar().showMessage("复测已停止（已按已完成轮次排名）")
        else:
            QMessageBox.information(
                self, "复测完成",
                f"稳定性复测完成，用时 {elapsed:.1f} 秒。\n\n"
                f"{self.stab_summary_label.text()}",
            )
            self.statusBar().showMessage("复测完成")
        logger.info(
            "复测完成：%s 个 IP 有复测数据，最终排名 %s 条，用时 %.1f 秒，用户停止=%s",
            len(self._stability_map), len(entries), elapsed, stopped,
        )

    def _on_stability_failed(self, message: str) -> None:
        """复测线程出错：恢复按钮，结果表格保持 V1.3 排名不变。"""
        self._set_stability_running_state(False)
        self.stab_status_label.setText("复测失败，请查看日志")
        logger.error("复测失败：%s", message)
        QMessageBox.critical(self, "复测失败", message)
        self.statusBar().showMessage("复测失败")

    def _on_stability_worker_thread_finished(self) -> None:
        """复测线程真正结束后释放对象（复测汇总已由 _on_stability_finished 保存）。"""
        worker = self._stability_worker
        self._stability_worker = None
        if worker is not None:
            worker.deleteLater()
        logger.info("复测线程对象已释放")

    def _has_stability_targets(self) -> bool:
        """是否存在可复测的 IP（V1.3 排名里至少有一个评分有效的 IP）。"""
        return any(
            entry.score is not None and entry.rank > 0
            for entry in self.result_table.rank_entries
        )

    def _refresh_stability_controls(self) -> None:
        """统一刷新复测按钮的可用状态。

        以下任意一种情况都会让「开始复测」不可用：
        - 主测速进行中（_running）；
        - 复测自己正在进行中（_stability_running）；
        - 没有可复测的 IP（还没测速，或筛选后全部失败）。
        「停止复测」只在复测进行中可用。
        """
        self.stab_start_button.setEnabled(
            not self._running and not self._stability_running and self._has_stability_targets()
        )
        self.stab_stop_button.setEnabled(self._stability_running)

    def _set_stability_running_state(self, running: bool) -> None:
        """切换复测进行中的控件可用状态（复测与主测速互斥）。

        复测进行中：V1.3 的筛选/复制/导出按钮暂时禁用，避免用户误以为
        还能用旧排名做导出；复测结束后由 _on_stability_finished 重新开启。
        """
        self._stability_running = running
        self.stab_rounds_combo.setEnabled(not running)
        self.stab_concurrency_combo.setEnabled(not running)
        # 复测/测速任一进行中，都不允许点主测速开始按钮，也不允许改导入/设置
        self.start_button.setEnabled(not running and not self._running)
        self.ip_panel.set_controls_enabled(not running and not self._running)
        # V1.3 的筛选/复制/导出按钮也与复测互斥（复测用的是同一批 TOP IP 集合）
        self.apply_filter_button.setEnabled(not running and not self._running)
        self._set_export_buttons_enabled(
            not running and not self._running and bool(self.result_table.rank_entries)
        )
        self._refresh_stability_controls()
        if running:
            # 新的一次复测：清空进度条与上一轮的摘要
            self.stab_progress_bar.setValue(0)
            self.stab_summary_label.setText("")
            self.stab_current_label.setText("当前：--")

    def _refresh_stability_summary(self, stopped: bool, elapsed: float) -> None:
        """刷新复测摘要：稳定/波动数量、平均稳定性、最高最终评分、TOP10。"""
        entries = self.result_table.final_entries
        if not entries:
            self.stab_summary_label.setText("复测完成：无有效结果")
            self.stab_status_label.setText("复测完成：无有效结果")
            return
        stable = [e for e in entries if e.stability_score >= STABLE_SCORE_THRESHOLD]
        unstable = len(entries) - len(stable)
        avg_stability = sum(e.stability_score for e in entries) / len(entries)
        best = max(entries, key=lambda e: e.final_score)
        top10 = "，".join(e.result.ip for e in entries[:10])
        self.stab_summary_label.setText(
            f"稳定IP：{len(stable)}　|　波动IP：{unstable}　|　"
            f"平均稳定性：{avg_stability:.0f}　|　"
            f"最高最终评分：{best.final_score}（{best.result.ip}）　|　"
            f"TOP10：{top10}"
        )
        self.stab_status_label.setText(
            f"复测{'已停止' if stopped else '完成'}：{len(entries)} 个 IP，用时 {elapsed:.1f} 秒"
        )

    def _on_export_stable_txt(self) -> None:
        """导出稳定 TOP TXT（按最终排名取前 100，每行一个 IP）。"""
        try:
            path = export_stable_txt(self.result_table.final_entries, top_n=DEFAULT_TOP_N)
        except ExportError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出成功", f"稳定 TOP TXT 已保存到：\n{path}")
        logger.info("用户导出稳定 TXT：%s", path)

    def _on_export_stable_csv(self) -> None:
        """导出稳定性 CSV（完整统计字段）。"""
        try:
            path = export_stable_csv(self.result_table.final_entries)
        except ExportError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出成功", f"稳定性 CSV 已保存到：\n{path}")
        logger.info("用户导出稳定性 CSV：%s", path)

    # ==================================================================
    # IP 导入
    # ==================================================================
    def _on_ips_imported(self, outcome) -> None:
        """IP 导入完成（由 IPPanel 通过信号通知）。"""
        self._valid_entries = self.ip_panel.valid_entries

        if outcome.summary.valid_count == 0:
            self.statusBar().showMessage("导入完成：没有可用的公网 IPv4 地址")
            return
        self.statusBar().showMessage(
            f"导入完成：{outcome.summary.valid_count} 个有效 IP，可以开始测速"
        )

    def _on_ips_cleared(self) -> None:
        """用户点击了【清空导入】。"""
        self._valid_entries = []
        self.result_summary_label.setText("暂无结果")
        self.statusBar().showMessage("已清空导入内容")
        # V1.4：复测的原始汇总不再有效，但表格快照（V1.3 排名 / 最终排名）
        # 保持显示、导出仍可用，与此前 V1.3 “清空导入不清空结果”的行为一致。
        self._stability_map = {}
        self._stab_results_all = []
        has_final = bool(self.result_table.final_entries)
        self.export_stable_txt_button.setEnabled(has_final)
        self.export_stable_csv_button.setEnabled(has_final)
        if has_final:
            self.stab_status_label.setText("已清空导入（复测结果仍保留在表格中）")
        else:
            self.stab_status_label.setText("尚未复测")
            self.stab_summary_label.setText("")

    def _on_fetch_completed(self, summary_text: str) -> None:
        """自动获取 Cloudflare IP 完成（由 IPPanel 通知）。"""
        self._valid_entries = self.ip_panel.valid_entries
        self.statusBar().showMessage(f"{summary_text}，可以点击【开始测速】")

    def _on_fetch_failed(self, message: str) -> None:
        """自动获取 Cloudflare IP 失败。"""
        self.statusBar().showMessage(f"自动获取失败：{message}")

    # ==================================================================
    # 测速流程
    # ==================================================================
    def _on_start_scan(self) -> None:
        """开始测速。"""
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "提示", "测速正在进行，请等待结束或点击【停止测速】。")
            return
        if self._stability_running:
            QMessageBox.information(self, "提示", "稳定性复测正在进行，请等待结束或点击【停止复测】。")
            return
        if not self._valid_entries:
            QMessageBox.warning(self, "提示", "还没有可用的 IP，请先导入 IP。")
            return

        port = self.port_spin.value()
        concurrency = self.concurrency_spin.value()
        timeout_ms = self.timeout_spin.value()

        # ---- V1.2：第二级 HTTP 与第三级 下载 的参数（全部来自界面，可随时调整）----
        http_enabled = self.http_checkbox.isChecked()
        http_timeout_ms = self.http_timeout_spin.value()
        download_enabled = self.download_checkbox.isChecked()
        download_bytes = self.download_size_combo.currentData()
        if download_bytes is None:  # 极端情况下取不到数据，退回默认值
            download_bytes = DEFAULT_DOWNLOAD_BYTES
        download_timeout_ms = self.download_timeout_spin.value()

        total = len(self._valid_entries)

        # 重置界面与缓存
        self._pending_results.clear()
        self._all_results.clear()
        self.result_table.clear_results()
        # V1.4：新一轮测速会使旧的复测结果失效，先清空复测状态
        self._stability_map = {}
        self._stab_results_all = []
        self.export_stable_txt_button.setEnabled(False)
        self.export_stable_csv_button.setEnabled(False)
        self.stab_status_label.setText("尚未复测")
        self.stab_summary_label.setText("")
        self.stab_current_label.setText("当前：--")
        self.stab_progress_bar.setValue(0)
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(0)
        self.tested_label.setText(f"已测试：0 / {total}")
        self.success_label.setText("成功：0")
        self.failed_label.setText("失败：0")
        self.elapsed_label.setText("用时：0.0 秒")
        self._reset_stage_labels(total)
        self.result_summary_label.setText("测速进行中……")

        # 创建后台测速线程
        self._worker = ScanWorker(
            self._valid_entries,
            port,
            concurrency,
            timeout_ms,
            http_enabled=http_enabled,
            http_timeout_ms=http_timeout_ms,
            download_enabled=download_enabled,
            download_bytes=int(download_bytes),
            download_timeout_ms=download_timeout_ms,
            parent=self,
        )
        self._worker.result_ready.connect(self._on_result_ready)
        self._worker.progress_changed.connect(self._on_progress_changed)
        self._worker.stage_changed.connect(self._on_stage_changed)  # V1.2：各阶段统计
        self._worker.scan_finished.connect(self._on_scan_finished)
        self._worker.scan_failed.connect(self._on_scan_failed)
        self._worker.finished.connect(self._on_worker_thread_finished)

        self._scan_start_time = time.perf_counter()
        self._set_running_state(True)
        self._flush_timer.start()
        self._elapsed_timer.start()
        # 注意：_set_running_state(True) 已经把「开始复测」按钮一并禁用，
        # 所以复测与主测速不会同时运行，无需额外的抢占标记。
        self._worker.start()

        # 具体的测速参数由 core/scanner.py 记录日志，这里只提示用户操作
        logger.info(
            "用户点击【开始测速】，目标 %s 个（HTTP测试=%s，下载测速=%s）",
            total,
            "开启" if http_enabled else "关闭",
            "开启" if download_enabled else "关闭",
        )
        self.statusBar().showMessage("测速进行中……")

    def _on_stop_scan(self) -> None:
        """请求停止测速。"""
        if self._worker is None or not self._worker.isRunning():
            return

        # 停止过程中按钮保持禁用，等线程安全退出后再恢复
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.result_summary_label.setText("正在停止测速，请稍候……")
        self.statusBar().showMessage("正在停止测速，请稍候……")
        logger.info("用户点击了停止测速")
        self._worker.request_stop()

    def _on_result_ready(self, result: TestResult) -> None:
        """收到一个测速结果（先缓存，稍后批量写入表格）。

        结果先进 _pending_results 缓存，由 _flush_results 定时批量写表格；
        同时存进 _all_results，测速结束后用于计算综合评分与排名。
        """
        self._pending_results.append(result)
        self._all_results.append(result)

    def _flush_results(self) -> None:
        """把缓存的结果批量写入表格。"""
        if not self._pending_results:
            return
        batch = self._pending_results
        self._pending_results = []
        self.result_table.add_results(batch)

    def _on_progress_changed(self, tested: int, success: int, failed: int, total: int) -> None:
        """刷新进度显示。"""
        self.progress_bar.setValue(min(tested, total))
        self.tested_label.setText(f"已测试：{tested} / {total}")
        self.success_label.setText(f"成功：{success}")
        self.failed_label.setText(f"失败：{failed}")

    # ------------------------------------------------------------------
    # V1.2：三级测试（TCP / HTTP / 下载）各自的成功 / 失败统计
    # ------------------------------------------------------------------
    def _reset_stage_labels(self, total: int) -> None:
        """开始测速前，把各阶段统计清零（初始时全部阶段都还没测试）。"""
        self.tcp_stat_label.setText("TCP 成功 0 / 失败 0")
        self.http_stat_label.setText(f"HTTP 成功 0 / 失败 0 / 未测试 {total}")
        self.download_stat_label.setText(f"下载 成功 0 / 失败 0 / 未测试 {total}")

    def _on_stage_changed(self, stats: StageStats) -> None:
        """收到各阶段统计快照，刷新标签。

        「未测试」表示这个阶段没有被执行：
        - HTTP 未测试 = TCP 没通的 IP + 用户关闭了 HTTP 测试
        - 下载未测试 = TCP 或 HTTP 没通的 IP + 用户关闭了下载测速
        """
        self.tcp_stat_label.setText(f"TCP 成功 {stats.tcp_success} / 失败 {stats.tcp_failed}")

        http_untested = max(0, stats.tcp_tested - stats.http_tested)
        self.http_stat_label.setText(
            f"HTTP 成功 {stats.http_success} / 失败 {stats.http_failed} / 未测试 {http_untested}"
        )

        download_untested = max(0, stats.tcp_tested - stats.download_tested)
        self.download_stat_label.setText(
            f"下载 成功 {stats.download_success} / 失败 {stats.download_failed} / "
            f"未测试 {download_untested}"
        )

    def _refresh_elapsed(self) -> None:
        """定时刷新“用时”（测速进行中，即使没有新结果也会走动）。"""
        self.elapsed_label.setText(f"用时：{self._elapsed_seconds():.1f} 秒")

    def _on_scan_finished(self, summary: ScanSummary, elapsed: float) -> None:
        """测速结束：排序显示结果并恢复按钮状态。"""
        self._flush_timer.stop()
        self._elapsed_timer.stop()
        self._flush_results()

        # V1.3：按综合评分排名显示（含筛选与 TOP N），并生成结果摘要
        self._refresh_ranking_view()
        self._set_export_buttons_enabled(True)

        self.progress_bar.setValue(min(summary.tested, summary.total))
        self.tested_label.setText(f"已测试：{summary.tested} / {summary.total}")
        self.success_label.setText(f"成功：{summary.success}")
        self.failed_label.setText(f"失败：{summary.failed}")
        self.elapsed_label.setText(f"用时：{elapsed:.1f} 秒")
        # 用最终汇总信息刷新各阶段统计，保证显示的数字与汇总完全一致
        self._on_stage_changed(StageStats.from_summary(summary))

        self._set_running_state(False)
        logger.info(
            "测速完成：完成 %s/%s，TCP 成功 %s / 失败 %s，HTTP 成功 %s / 失败 %s，"
            "下载成功 %s / 失败 %s，用时 %.1f 秒，用户停止=%s",
            summary.tested,
            summary.total,
            summary.success,
            summary.failed,
            summary.http_success,
            summary.http_failed,
            summary.download_success,
            summary.download_failed,
            elapsed,
            summary.stopped,
        )

        if summary.stopped:
            QMessageBox.information(
                self,
                "已停止测速",
                f"测速已停止，已保留 {summary.tested} 条结果。\n\n"
                f"TCP 成功：{summary.success}，失败：{summary.failed}\n"
                f"HTTP 成功：{summary.http_success}，失败：{summary.http_failed}\n"
                f"下载成功：{summary.download_success}，失败：{summary.download_failed}",
            )
            self.statusBar().showMessage("测速已停止")
        else:
            QMessageBox.information(
                self,
                "测速完成",
                f"测速完成，用时 {elapsed:.1f} 秒。\n\n"
                f"共测试：{summary.tested}\n\n"
                f"TCP 成功：{summary.success}，失败：{summary.failed}\n"
                f"HTTP 成功：{summary.http_success}，失败：{summary.http_failed}\n"
                f"下载成功：{summary.download_success}，失败：{summary.download_failed}",
            )
            self.statusBar().showMessage("测速完成")

    def _on_scan_failed(self, message: str) -> None:
        """测速线程出错。"""
        self._flush_timer.stop()
        self._elapsed_timer.stop()
        self._flush_results()
        self._set_running_state(False)
        self.result_summary_label.setText("测速失败，请查看日志")
        logger.error("测速失败：%s", message)
        QMessageBox.critical(self, "测速失败", message)
        self.statusBar().showMessage("测速失败")

    def _on_worker_thread_finished(self) -> None:
        """线程真正结束后释放对象。"""
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()

    def _set_running_state(self, running: bool) -> None:
        """根据是否正在测速，切换控件的可用状态。"""
        self._running = running
        self.start_button.setEnabled(not running and not self._stability_running)
        self.stop_button.setEnabled(running)
        self.ip_panel.set_controls_enabled(not running and not self._stability_running)
        self.port_spin.setEnabled(not running)
        self.concurrency_spin.setEnabled(not running)
        self.timeout_spin.setEnabled(not running)
        # V1.2：第二级 / 第三级的控件也要一起锁定/解锁
        # （_on_http_toggled 与 _on_download_toggled 会根据 _running 和开关状态算出正确结果）
        self.http_checkbox.setEnabled(not running)
        self._on_http_toggled(self.http_checkbox.isChecked())
        # V1.3：测速期间禁用复制/导出（结束后由 _on_scan_finished 重新开启）
        self._set_export_buttons_enabled(not running and bool(self.result_table.rank_entries))
        # V1.4：测速期间禁用复测开始按钮（结束后有可复测的 IP 才可用）
        self._refresh_stability_controls()

    def _elapsed_seconds(self) -> float:
        """本轮测速已运行的时间（秒）。"""
        if not self._scan_start_time:
            return 0.0
        return time.perf_counter() - self._scan_start_time

    # ==================================================================
    # 关闭窗口
    # ==================================================================
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt 规定的命名)
        """关闭窗口时，如果正在测速，先安全停止线程。"""
        worker = self._worker
        if worker is not None and worker.isRunning():
            answer = QMessageBox.question(
                self,
                "确认退出",
                "测速正在进行，确定要退出吗？\n（会停止测速，并保留已经得到的结果）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

            logger.info("关闭窗口：正在停止测速线程")
            self._flush_timer.stop()
            self._elapsed_timer.stop()
            worker.request_stop()
            # V1.4：复测线程同样请求停止（已完成轮次的结果已保留在内存中）
            stab_worker = self._stability_worker
            if stab_worker is not None and stab_worker.isRunning():
                logger.info("关闭窗口：正在停止复测线程")
                stab_worker.request_stop()

            # 先等一小会儿；如果线程还没结束（例如超时设置很大），
            # 就先隐藏窗口，等线程安全结束后再自动退出，避免强杀线程导致崩溃。
            if not worker.wait(3000):
                logger.info("测速线程仍在收尾，窗口先隐藏，结束后自动退出")
                self.hide()
                self.statusBar().showMessage("正在停止测速，程序即将自动退出……")
                self._start_quit_timer()
                event.ignore()
                return

        # V1.4：如果只有复测线程在跑，同样先安全停止再退出（先向用户确认一次）
        stab_worker = self._stability_worker
        if stab_worker is not None and stab_worker.isRunning():
            answer = QMessageBox.question(
                self,
                "确认退出",
                "稳定性复测正在进行，确定要退出吗？\n（会停止复测，已完成的轮次不会被保存）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            logger.info("关闭窗口：正在停止复测线程")
            stab_worker.request_stop()
            if not stab_worker.wait(3000):
                logger.info("复测线程仍在收尾，窗口先隐藏，结束后自动退出")
                self.hide()
                self.statusBar().showMessage("正在停止复测，程序即将自动退出……")
                self._start_quit_timer()
                event.ignore()
                return

        # 自动获取线程只是单个 HTTP 请求，最多等 3 秒即可安全退出
        self.ip_panel.shutdown_fetch()

        logger.info("程序关闭：主窗口已关闭")
        event.accept()

    def _start_quit_timer(self) -> None:
        """启动定时器，定期检查测速线程是否已经结束。"""
        if self._quit_timer is not None:
            return
        self._quit_timer = QTimer(self)
        self._quit_timer.setInterval(200)
        self._quit_timer.timeout.connect(self._check_worker_before_quit)
        self._quit_timer.start()

    def _check_worker_before_quit(self) -> None:
        """测速线程结束后，真正关闭程序。"""
        worker = self._worker
        if worker is not None and worker.isRunning():
            return
        # V1.4：复测线程也必须结束才能退出
        stab_worker = self._stability_worker
        if stab_worker is not None and stab_worker.isRunning():
            return

        if self._quit_timer is not None:
            self._quit_timer.stop()
            self._quit_timer = None
        self.close()  # 再次触发 closeEvent，此时线程已经结束