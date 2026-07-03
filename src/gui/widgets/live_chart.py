"""Gráfico de tendência de tensão/corrente, com limites como referência visual.

Os limites (min/max) são SEMPRE linhas-guia — nunca disparam avaliação
automática (seção 3.3). Para sessões longas, o chamador deve passar os
dados já decimados (`SamplingBuffer.decimate`) para este widget não tentar
desenhar centenas de milhares de pontos.
"""
from __future__ import annotations

from PySide6 import QtCharts, QtCore, QtGui, QtWidgets

from core.sampling_buffer import Sample


class LiveChart(QtWidgets.QWidget):
    _COLOR_TEXT = "#F5F7FA"
    _COLOR_GRID = "#3A4F7A"
    _COLOR_MINOR_GRID = "#22315A"
    _COLOR_VOLTAGE = "#FF7A29"
    _COLOR_CURRENT = "#4FD1C5"
    _COLOR_LIMIT = "#E74C3C"

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        self._voltage_series = QtCharts.QLineSeries(name="Tensão (V)")
        self._current_series = QtCharts.QLineSeries(name="Corrente (A)")
        self._voltage_min_series = QtCharts.QLineSeries(name="V mín (ref.)")
        self._voltage_max_series = QtCharts.QLineSeries(name="V máx (ref.)")

        voltage_pen = QtGui.QPen(QtGui.QColor(self._COLOR_VOLTAGE))
        voltage_pen.setWidthF(2.2)
        self._voltage_series.setPen(voltage_pen)

        current_pen = QtGui.QPen(QtGui.QColor(self._COLOR_CURRENT))
        current_pen.setWidthF(2.2)
        self._current_series.setPen(current_pen)

        for guide_series in (self._voltage_min_series, self._voltage_max_series):
            pen = QtGui.QPen(QtGui.QColor(self._COLOR_LIMIT))
            pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            pen.setWidthF(1.4)
            guide_series.setPen(pen)

        self._chart = QtCharts.QChart()
        self._chart.addSeries(self._voltage_series)
        self._chart.addSeries(self._voltage_min_series)
        self._chart.addSeries(self._voltage_max_series)
        self._chart.addSeries(self._current_series)

        self._chart.setTitle("Monitoramento em tempo real — Tensão e Corrente")
        title_font = self._chart.titleFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        self._chart.setTitleFont(title_font)
        self._chart.setTitleBrush(QtGui.QBrush(QtGui.QColor(self._COLOR_TEXT)))

        self._chart.setBackgroundBrush(QtGui.QBrush(QtGui.QColor("#0A1F44")))
        self._chart.setPlotAreaBackgroundBrush(QtGui.QBrush(QtGui.QColor("#0E2554")))
        self._chart.setPlotAreaBackgroundVisible(True)
        self._chart.setMargins(QtCore.QMargins(12, 12, 12, 12))
        self._chart.setAnimationOptions(QtCharts.QChart.AnimationOption.NoAnimation)

        legend = self._chart.legend()
        legend.setVisible(True)
        legend.setLabelColor(QtGui.QColor(self._COLOR_TEXT))
        legend.setAlignment(QtCore.Qt.AlignmentFlag.AlignBottom)

        self._axis_x = QtCharts.QValueAxis()
        self._axis_x.setTitleText("Tempo (s)")
        self._axis_voltage = QtCharts.QValueAxis()
        self._axis_voltage.setTitleText("Tensão (V)")
        self._axis_current = QtCharts.QValueAxis()
        self._axis_current.setTitleText("Corrente (A)")

        for axis in (self._axis_x, self._axis_voltage, self._axis_current):
            axis.setLabelsColor(QtGui.QColor(self._COLOR_TEXT))
            axis.setTitleBrush(QtGui.QBrush(QtGui.QColor(self._COLOR_TEXT)))
            axis.setGridLineVisible(True)
            axis.setGridLinePen(QtGui.QPen(QtGui.QColor(self._COLOR_GRID), 1, QtCore.Qt.PenStyle.SolidLine))
            axis.setMinorGridLineVisible(True)
            axis.setMinorGridLinePen(
                QtGui.QPen(QtGui.QColor(self._COLOR_MINOR_GRID), 1, QtCore.Qt.PenStyle.DotLine)
            )
            axis.setMinorTickCount(1)
            axis.setLinePen(QtGui.QPen(QtGui.QColor(self._COLOR_TEXT), 1))
        self._axis_voltage.setLabelFormat("%.2f")
        self._axis_current.setLabelFormat("%.3f")
        self._axis_x.setLabelFormat("%.0f")

        # Cada eixo Y na cor da sua linha: deixa inequívoco que a linha laranja
        # se lê na escala da ESQUERDA (tensão) e a verde na DIREITA (corrente).
        # Sem isso, a corrente (eixo 0–1,2) parecia ~5 quando lida contra o
        # eixo da tensão — o "valor a mais" relatado.
        self._axis_voltage.setLabelsColor(QtGui.QColor(self._COLOR_VOLTAGE))
        self._axis_voltage.setTitleBrush(QtGui.QBrush(QtGui.QColor(self._COLOR_VOLTAGE)))
        self._axis_current.setLabelsColor(QtGui.QColor(self._COLOR_CURRENT))
        self._axis_current.setTitleBrush(QtGui.QBrush(QtGui.QColor(self._COLOR_CURRENT)))

        self._chart.addAxis(self._axis_x, QtCore.Qt.AlignmentFlag.AlignBottom)
        self._chart.addAxis(self._axis_voltage, QtCore.Qt.AlignmentFlag.AlignLeft)
        self._chart.addAxis(self._axis_current, QtCore.Qt.AlignmentFlag.AlignRight)

        for series in (self._voltage_series, self._voltage_min_series, self._voltage_max_series):
            series.attachAxis(self._axis_x)
            series.attachAxis(self._axis_voltage)
        self._current_series.attachAxis(self._axis_x)
        self._current_series.attachAxis(self._axis_current)

        chart_view = QtCharts.QChartView(self._chart)
        chart_view.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        chart_view.setMinimumSize(640, 420)
        chart_view.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(chart_view)

        self._t0: float | None = None
        self._y_min: float = 0.0
        self._y_max: float = 10.0
        self._i_observed_max: float = 0.0

    def set_voltage_limits(
        self,
        voltage_min: float,
        voltage_max: float,
        duration_s: float,
        step_voltages: list[float] | None = None,
    ) -> None:
        self._voltage_min_series.replace([QtCore.QPointF(0, voltage_min), QtCore.QPointF(duration_s, voltage_min)])
        self._voltage_max_series.replace([QtCore.QPointF(0, voltage_max), QtCore.QPointF(duration_s, voltage_max)])
        x_max = max(duration_s, 1.0)
        self._axis_x.setRange(0, x_max)
        self._axis_x.setTickCount(min(11, max(2, int(x_max // 10) + 2)))
        # O eixo Y engloba as linhas-guia (min/max) E as tensões reais dos
        # passos — sem isso, se o nominal for maior que voltage_max, a linha
        # de tensão fica acima do eixo e o operador não vê nada no gráfico.
        all_v = [voltage_min, voltage_max] + (step_voltages or [])
        v_lo = min(all_v)
        v_hi = max(all_v)
        margin = max(0.5, (v_hi - v_lo) * 0.2)
        self._y_min = v_lo - margin
        self._y_max = v_hi + margin
        self._axis_voltage.setRange(self._y_min, self._y_max)
        self._axis_voltage.setTickCount(8)

    def set_current_range(self, current_max: float) -> None:
        # `current_max` é o limite de proteção (OCP), não a corrente esperada
        # do ensaio — usá-lo como escala fixa do eixo deixa a curva real
        # espremida perto de zero quando a corrente do DUT é bem menor que o
        # limite (ex.: limite 1,2 A, corrente real 0,03 A). Serve só como
        # escala inicial antes da 1ª amostra; `update_samples` reajusta o
        # eixo à corrente realmente observada.
        self._i_observed_max = 0.0
        self._axis_current.setRange(0, max(current_max * 1.2, 0.1))
        self._axis_current.setTickCount(8)

    def update_samples(self, samples: list[Sample]) -> None:
        if not samples:
            return
        if self._t0 is None:
            self._t0 = samples[0].timestamp

        voltage_points = [QtCore.QPointF(s.timestamp - self._t0, s.voltage) for s in samples]
        current_points = [QtCore.QPointF(s.timestamp - self._t0, s.current) for s in samples]
        self._voltage_series.replace(voltage_points)
        self._current_series.replace(current_points)

        # Expande o eixo X se o ensaio já durar mais do que a duração
        # prevista em set_voltage_limits() (ex.: retries de leitura, tempo
        # OFF mal contabilizado) -- sem isso a curva simplesmente "sai" do
        # gráfico pela direita, com pontos fora da área visível.
        elapsed_max = voltage_points[-1].x()
        if elapsed_max > self._axis_x.max():
            self._axis_x.setRange(0, elapsed_max * 1.05)

        # Expande o eixo Y se alguma leitura ultrapassar o range configurado.
        v_vals = [s.voltage for s in samples]
        lo, hi = min(v_vals), max(v_vals)
        expand = False
        if lo < self._y_min:
            self._y_min = lo - max(0.5, (self._y_max - self._y_min) * 0.1)
            expand = True
        if hi > self._y_max:
            self._y_max = hi + max(0.5, (self._y_max - self._y_min) * 0.1)
            expand = True
        if expand:
            self._axis_voltage.setRange(self._y_min, self._y_max)

        # A corrente é a grandeza mais importante do monitoramento (a tensão
        # é apenas um preset do procedimento) — o eixo precisa acompanhar a
        # corrente REAL observada, não ficar preso à escala do limite de
        # proteção (que costuma ser muito maior que a corrente do DUT).
        i_vals = [s.current for s in samples]
        batch_max = max(i_vals)
        if batch_max > self._i_observed_max:
            self._i_observed_max = batch_max
        target_hi = max(self._i_observed_max * 1.3, 0.05)
        current_hi = self._axis_current.max()
        if current_hi <= 0 or abs(target_hi - current_hi) / current_hi > 0.15:
            self._axis_current.setRange(0, target_hi)

    def clear(self) -> None:
        self._t0 = None
        self._i_observed_max = 0.0
        self._voltage_series.clear()
        self._current_series.clear()
