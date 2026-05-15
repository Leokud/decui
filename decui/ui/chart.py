"""Простой виджет столбчатой диаграммы для отчётов."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget


class BarChart(QWidget):
    """Горизонтальная столбчатая диаграмма.

    Использование в отчёте:
        from decui.ui.chart import BarChart
        chart = BarChart(labels=["A","B"], values=[10, 25])
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
        self._values = values or []
        self._title = title
        self._bar_color = QColor(bar_color)
        self.setMinimumHeight(120)

    def set_data(self, labels: list[str], values: list[float]) -> None:
        self._labels = labels
        self._values = values
        self.update()

    def paintEvent(self, event: Any) -> None:
        if not self._values:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        margin = 50
        chart_w = w - margin * 2
        chart_h = h - margin * 2 - 20

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        max_val = max(self._values) if self._values else 1
        bar_count = len(self._values)
        bar_w = max(8, (chart_w // bar_count) - 8)
        gap = max(4, (chart_w - bar_w * bar_count) // (bar_count + 1))

        # Title
        if self._title:
            painter.setFont(QFont("", 10, QFont.Bold))
            painter.drawText(0, 10, w, 30, Qt.AlignCenter, self._title)

        # Bars
        base_y = h - margin
        for i, val in enumerate(self._values):
            bar_h = int((val / max_val) * chart_h)
            x = margin + gap + i * (bar_w + gap)
            y = base_y - bar_h

            painter.setBrush(self._bar_color)
            painter.setPen(QPen(self._bar_color.darker(120), 1))
            painter.drawRect(x, y, bar_w, bar_h)

            # Value label
            painter.setPen(Qt.black)
            painter.setFont(QFont("", 8))
            painter.drawText(x, y - 14, bar_w, 14, Qt.AlignCenter, f"{val:.1f}")

            # Axis label
            if i < len(self._labels):
                painter.drawText(x - 10, base_y + 4, bar_w + 20, 20,
                                 Qt.AlignCenter, str(self._labels[i]))

        painter.end()
