"""测试结果表格控件。

只负责“把测速结果显示在表格里”，不包含任何测速逻辑。
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from core.tcp_tester import TestResult
from core.http_tester import format_speed
from core.ranking import FinalEntry, RankEntry
from utils.logger import get_logger

logger: logging.Logger = get_logger()

# 延迟颜色阈值（毫秒）：越低越绿，越高越红
FAST_LATENCY_MS = 80
SLOW_LATENCY_MS = 200

COLOR_SUCCESS = QColor(21, 115, 71)    # 绿色
COLOR_WARNING = QColor(198, 120, 0)    # 橙色
COLOR_FAILED = QColor(176, 42, 55)     # 红色
COLOR_MUTED = QColor(128, 128, 128)    # 灰色：表示「未测试」

# 下载速度的颜色阈值（字节/秒）：≥5MB/s 绿色，≥1MB/s 橙色，其余红色
FAST_SPEED_BPS = 5 * 1024 * 1024
SLOW_SPEED_BPS = 1 * 1024 * 1024


class _NumberItem(QTableWidgetItem):
    """按数字大小排序的单元格。

    QTableWidget 默认按字符串比较排序（"100 ms" 会排在 "42 ms" 前面），
    这里把数字存到 UserRole 里，排序时按真实数字比较。
    """

    def __init__(self, text: str, value: float) -> None:
        super().__init__(text)
        self.setData(Qt.ItemDataRole.UserRole, value)
        self.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

    def __lt__(self, other: QTableWidgetItem) -> bool:  # type: ignore[override]
        try:
            left = float(self.data(Qt.ItemDataRole.UserRole))
            right = float(other.data(Qt.ItemDataRole.UserRole))
            return left < right
        except (TypeError, ValueError):
            # 拿不到数字时退回默认的字符串比较
            return super().__lt__(other)


def _percent_item(rate: float, total: int) -> QTableWidgetItem:
    """成功率列（V1.4）：显示 xx%；没有复测次数时显示 --。

    - 100% 绿色、≥60% 橙色、其余红色，方便一眼看出稳定与否；
    - 排序用真实比例（0~1），所以 100% 排在 0% 前面。
    """
    if not total:
        item = _NumberItem("--", float("-inf"))
        item.setForeground(COLOR_MUTED)
        item.setToolTip("暂无复测数据")
        return item
    item = _NumberItem(f"{rate * 100:.0f}%", float(rate))
    if rate >= 0.999:
        item.setForeground(COLOR_SUCCESS)
    elif rate >= 0.6:
        item.setForeground(COLOR_WARNING)
    else:
        item.setForeground(COLOR_FAILED)
    item.setToolTip(f"成功 {round(rate * total)} / {total} 次")
    return item


def _cv_item(cv: Optional[float], label: str) -> QTableWidgetItem:
    """波动列（V1.4）：显示变异系数百分比，越小越稳定。

    - 样本不足 2 个（cv 为 None）显示 --（只有一轮无法判断波动）；
    - 排序用真实 CV，所以「很稳定」的排在前面。
    """
    if cv is None:
        item = _NumberItem("--", float("-inf"))
        item.setForeground(COLOR_MUTED)
        item.setToolTip("复测轮数不足，无法计算波动")
        return item
    item = _NumberItem(f"±{cv * 100:.0f}%", float(cv))
    if cv <= 0.15:
        item.setForeground(COLOR_SUCCESS)
    elif cv <= 0.60:
        item.setForeground(COLOR_WARNING)
    else:
        item.setForeground(COLOR_FAILED)
    item.setToolTip(f"{label}波动 {cv * 100:.1f}%（CV = 标准差 / 平均值，越小越稳定）")
    return item


class ResultTable(QTableWidget):
    """测速结果表格：排名 / IP / 端口 / TCP延迟 / HTTP状态 / HTTP延迟 / 下载速度 / 评分 / 状态。"""

    HEADERS = (
        "排名", "IP", "端口", "TCP延迟", "HTTP状态", "HTTP延迟", "下载速度",
        "综合评分", "稳定性", "TCP成功率", "HTTP成功率", "下载成功率",
        "TCP波动", "下载波动", "最终评分", "最终排名", "状态",
    )
    COL_RANK = 0
    COL_IP = 1
    COL_PORT = 2
    COL_LATENCY = 3
    # ---- V1.2 新增三列 ----
    COL_HTTP_STATUS = 4      # HTTP 状态码
    COL_HTTP_LATENCY = 5     # HTTP 响应耗时
    COL_SPEED = 6            # 下载速度
    COL_SCORE = 7            # 综合评分（V1.3）
    # ---- V1.4 新增八列 ----
    COL_STABILITY = 8        # 稳定性评分
    COL_TCP_RATE = 9         # TCP 成功率
    COL_HTTP_RATE = 10       # HTTP 成功率
    COL_DOWNLOAD_RATE = 11   # 下载成功率
    COL_TCP_CV = 12          # TCP 延迟波动
    COL_SPEED_CV = 13        # 下载速度波动
    COL_FINAL_SCORE = 14     # 最终评分
    COL_FINAL_RANK = 15      # 最终排名
    COL_STATUS = 16          # 总状态（保持放在最后一列）

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(self.HEADERS), parent)
        self.setHorizontalHeaderLabels(list(self.HEADERS))
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)      # 只读
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.setSortingEnabled(False)  # 测速过程中先不排序，避免大量数据反复重排

        header = self.horizontalHeader()
        header.setSectionResizeMode(self.COL_RANK, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_IP, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_PORT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_LATENCY, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_HTTP_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_HTTP_LATENCY, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SPEED, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SCORE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_STABILITY, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_TCP_RATE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_HTTP_RATE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_DOWNLOAD_RATE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_TCP_CV, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_SPEED_CV, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_FINAL_SCORE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_FINAL_RANK, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)

        self._results: List[TestResult] = []              # 原始测速结果（测速线程实时写入）
        self._rank_entries: List[RankEntry] = []          # V1.3 排名快照（测速结束后生成）
        self._final_entries: List[FinalEntry] = []        # V1.4 最终排名快照（复测后生成）

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def results(self) -> List[TestResult]:
        """当前表格中显示的所有结果。"""
        return list(self._results)

    @property
    def rank_entries(self) -> List[RankEntry]:
        """当前排名快照（测速结束后可用；测速中为空列表）。"""
        return list(self._rank_entries)

    def top_ips(self, n: int) -> List[str]:
        """取前 N 名的有效 IP（只含成功 IP，供一键复制/导出使用）。

        V1.4：复测完成后优先按最终排名取（稳定者优先）；否则按 V1.3 排名取。
        """
        if self._final_entries:
            return [entry.result.ip for entry in self._final_entries[:max(0, n)]]
        ips: List[str] = []
        for entry in self._rank_entries:
            if entry.rank > 0 and entry.result.success:
                ips.append(entry.result.ip)
                if len(ips) >= n:
                    break
        return ips

    def clear_results(self) -> None:
        """清空表格。"""
        self._results.clear()
        self._rank_entries.clear()
        self._final_entries.clear()
        self.setSortingEnabled(False)
        self.setRowCount(0)

    def add_results(self, results: Sequence[TestResult]) -> None:
        """批量追加结果（测速过程中使用，此时尚无排名/评分）。"""
        if not results:
            return
        self.setUpdatesEnabled(False)
        try:
            start_row = self.rowCount()
            self.setRowCount(start_row + len(results))
            for offset, result in enumerate(results):
                self._results.append(result)
                self._fill_row(start_row + offset, result, len(self._results))
        finally:
            self.setUpdatesEnabled(True)

    def set_results(self, results: Sequence[TestResult]) -> None:
        """用一批新结果替换表格内容（测速结束后排序显示时使用）。"""
        self.clear_results()
        self.add_results(results)

    def set_ranking(self, entries: Sequence[RankEntry]) -> None:
        """V1.3：按最终排名重绘表格（带排名与综合评分）。

        entries 来自 core/ranking.build_ranking()：
        - 有效 IP 带 rank（1 开始）和 score（0~100），排在前面；
        - 失败 IP rank=0、score=None，追加在后面。

        V1.4：重新生成 V1.3 排名会使旧的最终排名失效，这里一并清空最终排名。
        （注意：只清空最终排名快照，复测原始汇总由 MainWindow 统一清理。）
        """
        self._final_entries.clear()
        self._rank_entries = list(entries)
        self._results.clear()
        self.setSortingEnabled(False)
        self.setRowCount(0)
        if not self._rank_entries:
            return
        self.setUpdatesEnabled(False)
        try:
            self.setRowCount(len(self._rank_entries))
            for offset, entry in enumerate(self._rank_entries):
                self._results.append(entry.result)
                self._fill_row(offset, entry.result, offset + 1, entry)
        finally:
            self.setUpdatesEnabled(True)
        # 默认按「排名」列升序（修复：Qt 默认排序指示器是降序，会把失败 IP 排到最前面）
        self.setSortingEnabled(False)
        self.horizontalHeader().setSortIndicator(self.COL_RANK, Qt.SortOrder.AscendingOrder)

    def set_final_ranking(self, entries: Sequence[FinalEntry]) -> None:
        """V1.4：按最终排名重绘表格（填充稳定性与最终评分八列）。

        entries 来自 core/ranking.build_final_ranking()。
        复测只覆盖 TOP 集合，因此表格只显示参与复测的 IP（按最终名次排列）。

        注意：V1.3 的排名快照（_rank_entries）会保留，供「应用筛选」与后续
        复测使用；表格当前显示的则是最终排名。
        """
        # 先按 IP 记住 V1.3 名次（用于“排名”列保留显示，方便与最终排名对比）
        ranked_by_ip = {entry.result.ip: entry for entry in self._rank_entries}
        self._results.clear()
        self._final_entries = list(entries)
        self.setSortingEnabled(False)
        self.setRowCount(0)
        if not self._final_entries:
            return
        self.setUpdatesEnabled(False)
        try:
            self.setRowCount(len(self._final_entries))
            for offset, fe in enumerate(self._final_entries):
                self._results.append(fe.result)
                row = offset
                result = fe.result
                data = fe.data

                # 排名列显示 V1.3 名次（复测前该 IP 在 V1.3 中的名次，保留方便对比）
                v13_rank = None
                v13_saved = ranked_by_ip.get(result.ip)
                if v13_saved is not None and v13_saved.rank > 0:
                    v13_rank = v13_saved.rank
                if v13_rank is not None and v13_rank > 0:
                    rank_item = _NumberItem(str(v13_rank), float(v13_rank))
                    rank_item.setForeground(COLOR_SUCCESS if v13_rank <= 10 else COLOR_MUTED)
                    rank_item.setToolTip(f"V1.3 综合排名第 {v13_rank}")
                else:
                    rank_item = _NumberItem("--", float("inf"))
                    rank_item.setForeground(COLOR_MUTED)
                    rank_item.setToolTip("V1.3 排名中无此 IP（复测后新增显示）")
                self.setItem(row, self.COL_RANK, rank_item)
                ip_item = QTableWidgetItem(result.ip)
                ip_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                ip_item.setToolTip(
                    f"V1.3评分 {fe.score}｜稳定性 {fe.stability_score}｜"
                    f"复测 {data.rounds} 轮｜"
                    f"TCP {data.min_tcp_latency}~{data.max_tcp_latency} ms"
                )
                self.setItem(row, self.COL_IP, ip_item)
                self.setItem(row, self.COL_PORT, _NumberItem(str(result.port), float(result.port)))
                self.setItem(row, self.COL_LATENCY, self._build_latency_item(result))
                self.setItem(row, self.COL_HTTP_STATUS, self._build_http_status_item(result))
                self.setItem(row, self.COL_HTTP_LATENCY, self._build_http_latency_item(result))
                self.setItem(row, self.COL_SPEED, self._build_speed_item(result))
                self.setItem(row, self.COL_SCORE, self._build_score_item(fe.score))

                # ---- 稳定性八列 ----
                self.setItem(row, self.COL_STABILITY, self._build_stability_item(fe.stability_score))
                self.setItem(row, self.COL_TCP_RATE, _percent_item(data.tcp_success_rate, data.tcp_total))
                self.setItem(row, self.COL_HTTP_RATE, _percent_item(data.http_success_rate, data.http_total))
                self.setItem(row, self.COL_DOWNLOAD_RATE, _percent_item(data.download_success_rate, data.download_total))
                self.setItem(row, self.COL_TCP_CV, _cv_item(data.tcp_cv, "TCP 延迟"))
                self.setItem(row, self.COL_SPEED_CV, _cv_item(data.speed_cv, "下载速度"))
                final_item = _NumberItem(str(fe.final_score), float(fe.final_score))
                final_item.setForeground(self._score_color(fe.final_score))
                final_item.setToolTip(
                    f"最终评分 = V1.3评分 {fe.score} × 0.9 + 稳定性 {fe.stability_score} × 0.1"
                )
                self.setItem(row, self.COL_FINAL_SCORE, final_item)
                # 最终排名列：build_final_ranking 已按名次排好序，这里直接用行号+1 显示
                # （fe.rank 在表格写入后同样会更新为该名次，供后续摘要/导出使用）
                fe.rank = offset + 1
                final_rank_item = _NumberItem(str(fe.rank), float(fe.rank))
                final_rank_item.setForeground(COLOR_SUCCESS if fe.rank <= 10 else COLOR_MUTED)
                final_rank_item.setToolTip(f"最终排名第 {fe.rank}（V1.3评分×0.9 + 稳定性×0.1）")
                self.setItem(row, self.COL_FINAL_RANK, final_rank_item)

                self.setItem(row, self.COL_STATUS, self._build_status_item(result))
        finally:
            self.setUpdatesEnabled(True)
        self.setSortingEnabled(False)
        self.horizontalHeader().setSortIndicator(self.COL_FINAL_RANK, Qt.SortOrder.AscendingOrder)

    @staticmethod
    def _build_stability_item(stability_score: int) -> QTableWidgetItem:
        """稳定性评分列：按分数着色。"""
        item = _NumberItem(str(stability_score), float(stability_score))
        if stability_score >= 80:
            item.setForeground(COLOR_SUCCESS)
        elif stability_score >= 50:
            item.setForeground(COLOR_WARNING)
        else:
            item.setForeground(COLOR_FAILED)
        item.setToolTip(f"稳定性评分 {stability_score} 分（0~100，来自多轮复测）")
        return item

    @property
    def final_entries(self) -> List[FinalEntry]:
        """当前最终排名快照（稳定性复测后可用；否则为空列表）。"""
        return list(self._final_entries)

    def enable_sorting(self) -> None:
        """允许用户点击表头排序（测速结束后再开启）。"""
        self.setSortingEnabled(True)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _fill_row(self, row: int, result: TestResult, index: int,
                  entry: Optional[RankEntry] = None) -> None:
        """写入一行数据。

        entry 为 None 时表示「测速进行中」：排名/评分列显示占位符；
        entry 不为 None 时表示「测速结束后」：显示真实名次与评分。
        """
        # 排名（V1.3：有效排名显示名次；测速中或失败 IP 显示 --）
        if entry is not None and entry.rank > 0:
            rank_item = _NumberItem(str(entry.rank), float(entry.rank))
            rank_item.setForeground(COLOR_SUCCESS if entry.rank <= 10 else COLOR_MUTED)
            rank_item.setToolTip(f"综合排名第 {entry.rank}")
        else:
            rank_item = _NumberItem(str(index), float(index)) if entry is None else _NumberItem("--", float("inf"))
            if entry is None:
                rank_item.setForeground(COLOR_MUTED)
            else:
                rank_item.setForeground(COLOR_MUTED)
                rank_item.setToolTip("未进入排名（测速失败）")
        self.setItem(row, self.COL_RANK, rank_item)

        # IP
        ip_item = QTableWidgetItem(result.ip)
        ip_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if result.error:
            # 失败原因放在鼠标悬停提示里，表格保持简洁
            ip_item.setToolTip(f"{result.ip}:{result.port}  {result.error}")
        self.setItem(row, self.COL_IP, ip_item)

        # 端口
        port_item = _NumberItem(str(result.port), float(result.port))
        self.setItem(row, self.COL_PORT, port_item)

        # 延迟
        if result.success and result.latency is not None:
            latency_item = _NumberItem(f"{result.latency} ms", float(result.latency))
            latency_item.setForeground(self._latency_color(result.latency))
            latency_item.setToolTip(f"{result.latency} ms")
        else:
            # 失败的结果延迟显示 --，排序时用无穷大，保证排在最后
            latency_item = _NumberItem("--", float("inf"))
            latency_item.setForeground(COLOR_FAILED)
        self.setItem(row, self.COL_LATENCY, latency_item)

        # ---- V1.2 新增：HTTP 状态 / HTTP 延迟 / 下载速度 ----
        self.setItem(row, self.COL_HTTP_STATUS, self._build_http_status_item(result))
        self.setItem(row, self.COL_HTTP_LATENCY, self._build_http_latency_item(result))
        self.setItem(row, self.COL_SPEED, self._build_speed_item(result))

        # ---- V1.3 新增：综合评分 ----
        self.setItem(row, self.COL_SCORE, self._build_score_item(entry.score if entry is not None else None))

        # ---- V1.4 新增八列：稳定性复测之前全部显示 -- ----
        self.setItem(row, self.COL_STABILITY, _NumberItem("--", float("-inf")))
        self.item(row, self.COL_STABILITY).setForeground(COLOR_MUTED)
        self.item(row, self.COL_STABILITY).setToolTip("尚未进行稳定性复测")
        for col in (self.COL_TCP_RATE, self.COL_HTTP_RATE, self.COL_DOWNLOAD_RATE,
                    self.COL_TCP_CV, self.COL_SPEED_CV, self.COL_FINAL_SCORE, self.COL_FINAL_RANK):
            self.setItem(row, col, _NumberItem("--", float("-inf")))
            self.item(row, col).setForeground(COLOR_MUTED)

        # 状态
        self.setItem(row, self.COL_STATUS, self._build_status_item(result))

    @staticmethod
    def _build_score_item(score: Optional[int]) -> QTableWidgetItem:
        """综合评分列：0~100 按分数着色；无评分显示 -- 且排序沉底。"""
        if score is None:
            item = _NumberItem("--", float("-inf"))
            item.setForeground(COLOR_MUTED)
            item.setToolTip("未参与评分（下载未成功）")
            return item
        item = _NumberItem(str(score), float(score))
        if score >= 80:
            item.setForeground(COLOR_SUCCESS)
        elif score >= 50:
            item.setForeground(COLOR_WARNING)
        else:
            item.setForeground(COLOR_FAILED)
        item.setToolTip(f"综合评分 {score} 分（0~100）")
        return item

    def _build_http_status_item(self, result: TestResult) -> QTableWidgetItem:
        """HTTP 状态列：显示 200 等状态码；未测试显示 --，失败显示原因。"""
        if not result.http_tested:
            # 没有执行 HTTP 测试（TCP 不通，或用户在界面上关闭了 HTTP 测试）
            item = QTableWidgetItem("--")
            item.setForeground(COLOR_MUTED)
            item.setToolTip("未执行 HTTP 测试")
        elif result.http_status is not None:
            item = _NumberItem(str(result.http_status), float(result.http_status))
            if result.http_success:
                item.setForeground(COLOR_SUCCESS)
            else:
                item.setForeground(COLOR_FAILED)
                item.setToolTip(result.http_error or "HTTP 状态异常")
        else:
            # 没拿到状态码（超时 / TLS 失败等）
            item = QTableWidgetItem("--")
            item.setForeground(COLOR_FAILED)
            item.setToolTip(result.http_error or "未获取到 HTTP 响应")
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    def _build_http_latency_item(self, result: TestResult) -> QTableWidgetItem:
        """HTTP 延迟列：显示 HTTP 响应耗时；失败用无穷大排序到末尾。"""
        if result.http_latency is not None:
            item = _NumberItem(f"{result.http_latency} ms", float(result.http_latency))
            item.setForeground(self._latency_color(result.http_latency))
            item.setToolTip(f"HTTP 响应耗时 {result.http_latency} ms")
        else:
            item = _NumberItem("--", float("inf"))
            item.setForeground(COLOR_MUTED if not result.http_tested else COLOR_FAILED)
            item.setToolTip("未执行 HTTP 测试" if not result.http_tested else (result.http_error or "HTTP 超时"))
        return item

    def _build_speed_item(self, result: TestResult) -> QTableWidgetItem:
        """下载速度列：显示 MB/s 或 KB/s；未测速显示 --。"""
        if result.download_speed_bps is not None:
            text = format_speed(result.download_speed_bps)
            # 用真实速度（字节/秒）排序，而不是按文字排序
            item = _NumberItem(text, float(result.download_speed_bps))
            item.setForeground(self._speed_color(result.download_speed_bps))
            item.setToolTip(
                f"下载 {result.download_bytes} 字节，"
                f"耗时 {result.download_elapsed_ms} ms，速度 {text}"
            )
        else:
            item = _NumberItem("--", float("-inf"))  # 没速度的排在最后
            item.setForeground(COLOR_MUTED if not result.download_tested else COLOR_FAILED)
            item.setToolTip(
                "未执行下载测速" if not result.download_tested else (result.download_error or "下载失败")
            )
        return item

    def _score_color(self, score: Optional[int]) -> QColor:
        """按评分返回颜色（与综合评分列一致：≥80 绿，≥50 橙，其余红）。"""
        if score is None:
            return COLOR_MUTED
        if score >= 80:
            return COLOR_SUCCESS
        if score >= 50:
            return COLOR_WARNING
        return COLOR_FAILED

    def _build_latency_item(self, result: TestResult) -> QTableWidgetItem:
        """TCP 延迟列：成功显示 xx ms（按延迟着色），失败显示 --。"""
        if result.success and result.latency is not None:
            item = _NumberItem(f"{result.latency} ms", float(result.latency))
            item.setForeground(self._latency_color(result.latency))
            item.setToolTip(f"{result.latency} ms")
        else:
            item = _NumberItem("--", float("inf"))
            item.setForeground(COLOR_FAILED)
        return item

    @staticmethod
    def _build_status_item(result: TestResult) -> QTableWidgetItem:
        """总状态列：TCP / HTTP / 下载 三级结果的综合说明。"""
        if result.success and result.http_success and result.download_success:
            item = QTableWidgetItem("全部成功")
            item.setForeground(COLOR_SUCCESS)
            item.setToolTip("TCP、HTTP、下载三级测试全部成功")
        elif result.success and result.http_success:
            # TCP + HTTP 成功，但下载失败或未测
            if result.download_tested and not result.download_success:
                item = QTableWidgetItem(f"下载失败（{result.download_error or '未知原因'}）")
            else:
                item = QTableWidgetItem("HTTP成功")
            item.setForeground(COLOR_WARNING)
            item.setToolTip("TCP 与 HTTP 成功，下载阶段未成功")
        elif result.success and not result.http_tested:
            # 只做了 TCP（第一阶段的方式）
            item = QTableWidgetItem("TCP成功")
            item.setForeground(COLOR_SUCCESS)
            item.setToolTip("TCP 连接成功（未执行 HTTP 测试）")
        elif result.success and not result.http_success:
            item = QTableWidgetItem(f"HTTP失败（{result.http_error or '未知原因'}）")
            item.setForeground(COLOR_FAILED)
            item.setToolTip(result.http_error or "未知原因")
        else:
            item = QTableWidgetItem(f"TCP失败（{result.error or '未知原因'}）")
            item.setForeground(COLOR_FAILED)
            item.setToolTip(result.error or "未知原因")
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        return item

    @staticmethod
    def _latency_color(latency: Optional[int]) -> QColor:
        """按延迟大小返回颜色。"""
        if latency is None:
            return COLOR_FAILED
        if latency <= FAST_LATENCY_MS:
            return COLOR_SUCCESS
        if latency <= SLOW_LATENCY_MS:
            return COLOR_WARNING
        return COLOR_FAILED

    @staticmethod
    def _speed_color(speed_bps: Optional[float]) -> QColor:
        """按下载速度返回颜色（速度越高越绿）。"""
        if speed_bps is None:
            return COLOR_FAILED
        if speed_bps >= FAST_SPEED_BPS:
            return COLOR_SUCCESS
        if speed_bps >= SLOW_SPEED_BPS:
            return COLOR_WARNING
        return COLOR_FAILED