"""Виджет столбчатой диаграммы для отчётов и дашбордов."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPalette, QPen
from PySide6.QtWidgets import QToolTip, QWidget


class BarChart(QWidget):
    """Вертикальная столбчатая диаграмма (с поддержкой тёмной темы, отрицательных значений и tooltip).

    Использование в отчёте:
        from decui.ui.chart import BarChart
        chart = BarChart(labels=["A", "B"], values=[10, 25], title="Динамика")
        layout.addWidget(chart)
    """

    def __init__(
        self,
        labels: list[str] | None = None,
        values: list[float] | None = None,
        title: str = "",
        bar_color: str = "#4A7BFF",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._labels = labels or []
        self._values = [float(v) for v in (values or [])]
        self._title = title
        self._bar_color = QColor(bar_color)
        self._bar_rects: list[tuple[QRect, str, float]] = []
        self.setMinimumHeight(200)
        self.setMouseTracking(True)

    def set_data(self, labels: list[str], values: list[float]) -> None:
        self._labels = labels
        self._values = [float(v) for v in values]
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        for rect, label, val in self._bar_rects:
            if rect.contains(pos):
                QToolTip.showText(event.globalPosition().toPoint(), f"{label}: {val:,.2f}", self)
                return
        QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        QToolTip.hideText()
        super().leaveEvent(event)

    def paintEvent(self, event: Any) -> None:
        if not self._values:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._bar_rects.clear()

        pal = self.palette()
        text_color = pal.color(QPalette.ColorRole.WindowText)
        grid_color = pal.color(QPalette.ColorRole.Mid)

        w, h = self.width(), self.height()
        left_m = 44
        right_m = 20
        top_m = 38 if self._title else 24
        bottom_m = 36
        chart_w = w - left_m - right_m
        chart_h = h - top_m - bottom_m

        if chart_w < 20 or chart_h < 20:
            painter.end()
            return

        # Title
        if self._title:
            painter.setPen(text_color)
            painter.setFont(QFont("", 10, QFont.Weight.Bold))
            painter.drawText(0, 6, w, 24, Qt.AlignmentFlag.AlignCenter, self._title)

        max_pos = max((v for v in self._values if v > 0), default=0.0)
        min_neg = min((v for v in self._values if v < 0), default=0.0)
        span = (max_pos - min_neg) or 1.0

        # Zero baseline Y position
        base_y = int(top_m + (max_pos / span) * chart_h)

        # Draw baseline
        painter.setPen(QPen(grid_color, 1))
        painter.drawLine(left_m, base_y, w - right_m, base_y)

        bar_count = len(self._values)
        slot_w = chart_w / max(1, bar_count)
        gap = max(4.0, min(14.0, slot_w * 0.25))
        bar_w = max(2.0, slot_w - gap)

        painter.setFont(QFont("", 8))
        fm = QFontMetrics(painter.font())

        for i, val in enumerate(self._values):
            x = int(left_m + i * slot_w + gap / 2)
            bw = max(2, int(bar_w))
            bh = int((abs(val) / span) * chart_h)

            if val >= 0:
                y = base_y - bh
                color = self._bar_color
            else:
                y = base_y
                color = QColor("#e05d44")

            rect = QRect(x, y, bw, max(1, bh) if val != 0 else 0)
            label_str = str(self._labels[i]) if i < len(self._labels) else str(i + 1)
            self._bar_rects.append((QRect(x, top_m, bw, chart_h), label_str, val))

            if bh > 0:
                painter.setBrush(color)
                painter.setPen(QPen(color.darker(120), 1))
                painter.drawRect(rect)

            # Value label (only if enough width to avoid overlap)
            if bw >= 18:
                painter.setPen(text_color)
                val_str = f"{val:g}"
                val_y = y - 16 if val >= 0 else y + bh + 2
                painter.drawText(
                    x - 8, val_y, bw + 16, 15, Qt.AlignmentFlag.AlignCenter, val_str
                )

            # Axis label (elided cleanly)
            if i < len(self._labels):
                painter.setPen(text_color)
                max_lbl_w = max(24, int(slot_w))
                elided = fm.elidedText(label_str, Qt.TextElideMode.ElideRight, max_lbl_w)
                painter.drawText(
                    x - int(gap / 2), h - bottom_m + 6, max_lbl_w, 22,
                    Qt.AlignmentFlag.AlignCenter, elided,
                )

        painter.end()
