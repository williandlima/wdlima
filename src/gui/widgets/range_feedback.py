"""Feedback visual de faixa V/A da fonte, ANTES de qualquer comando SCPI.

Sem isto, o único jeito do operador descobrir que um setpoint não cabe na
faixa configurada (ou na faixa que ele forçou manualmente) é o "SCPI error
-222: Data out of range" depois de clicar em "Ligar saída"/"Salvar e
continuar". A parte pura (`evaluate_range_fit`) chama a MESMA regra de
negócio que `PowerSupplyE363x._ensure_range` usa de verdade
(`PowerSupplyE363x.classify_range_fit`, sem I/O) em vez de reimplementá-la
por conta própria -- assim a pré-visualização da tela não pode divergir
silenciosamente do que o instrumento real vai aceitar ou recusar. Testável
sem Qt e sem porta serial, e reutilizável nos dois lugares que pedem V/A ao
operador (Saída manual e Parâmetros do ensaio).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6 import QtGui, QtWidgets

from config import VoltageRange
from hardware.power_supply import PowerSupplyE363x

# Mesma paleta de branding.color_warning/color_fail (app_config.yaml) --
# hardcoded aqui como nos demais widgets (status_badge.py) para não amarrar
# este helper puro a um QWidget com acesso a BrandingConfig.
_COLOR_WARNING = "#F1C40F"
_COLOR_FAIL = "#E74C3C"


class RangeFitState(str, Enum):
    OK = "OK"
    # Não cabe na faixa que o operador forçou manualmente, mas caberia em outra.
    OUT_OF_FORCED_RANGE = "OUT_OF_FORCED_RANGE"
    # Não cabe em NENHUMA faixa configurada (forçada ou não) -- vai falhar de
    # qualquer forma, mesmo com seleção automática.
    OUT_OF_ALL_RANGES = "OUT_OF_ALL_RANGES"


@dataclass(frozen=True)
class RangeFitResult:
    state: RangeFitState
    message: str  # "" quando OK


def evaluate_range_fit(
    volts: float,
    amps: float,
    ranges: tuple[VoltageRange, ...],
    forced_range_name: str | None = None,
) -> RangeFitResult:
    """Classifica volts/amps contra `ranges` usando a MESMA regra do driver
    (`PowerSupplyE363x.classify_range_fit`) -- só traduz o resultado em cor/
    mensagem de tela, não reimplementa a decisão de encaixe.
    """
    if not ranges:
        return RangeFitResult(RangeFitState.OK, "")

    selected, alternative, forced_exists = PowerSupplyE363x.classify_range_fit(
        volts, amps, ranges, forced_range_name
    )
    if selected is not None:
        return RangeFitResult(RangeFitState.OK, "")

    if forced_range_name is not None and not forced_exists:
        return RangeFitResult(
            RangeFitState.OUT_OF_ALL_RANGES,
            f"Faixa forçada '{forced_range_name}' não existe em 'instrument.ranges' "
            "(app_config.yaml).",
        )
    if forced_range_name is not None and alternative is not None:
        return RangeFitResult(
            RangeFitState.OUT_OF_FORCED_RANGE,
            f"{volts:.2f} V / {amps:.3f} A não cabe na faixa forçada "
            f"'{forced_range_name}'. Caberia em '{alternative.name}' "
            f"(até {alternative.max_voltage:.2f} V / {alternative.max_current:.3f} A).",
        )
    if forced_range_name is not None:
        return RangeFitResult(
            RangeFitState.OUT_OF_ALL_RANGES,
            f"{volts:.2f} V / {amps:.3f} A não cabe em nenhuma faixa configurada da "
            f"fonte (nem na faixa forçada '{forced_range_name}').",
        )
    widest = max(ranges, key=lambda r: r.max_voltage)
    return RangeFitResult(
        RangeFitState.OUT_OF_ALL_RANGES,
        f"{volts:.2f} V / {amps:.3f} A não cabe em nenhuma faixa configurada da fonte "
        f"(máx. suportado: {widest.max_voltage:.2f} V / {widest.max_current:.3f} A).",
    )


def evaluate_test_limit_fit(
    voltage: float, current: float, voltage_min: float, voltage_max: float, current_max: float
) -> RangeFitResult:
    """Confere um passo do ciclo (sequência multi-step) contra os limites que
    o PRÓPRIO operador definiu em "Tensão mínima/máxima" e "Corrente máxima"
    -- distinto de `evaluate_range_fit` (faixa de HARDWARE da fonte). Sem
    isto, um passo digitado fora desses limites era aceito em silêncio; só
    o passo único (referência do gráfico ao vivo, nunca gatilho automático)
    tem essa liberdade -- um ciclo tem que respeitar o que foi documentado
    como aceitável para a placa.
    """
    if voltage > voltage_max:
        return RangeFitResult(
            RangeFitState.OUT_OF_ALL_RANGES,
            f"{voltage:.2f} V excede o limite superior definido nos parâmetros do "
            f"ensaio (Tensão máxima: {voltage_max:.2f} V).",
        )
    if voltage < voltage_min:
        return RangeFitResult(
            RangeFitState.OUT_OF_ALL_RANGES,
            f"{voltage:.2f} V está abaixo do limite inferior definido nos parâmetros "
            f"do ensaio (Tensão mínima: {voltage_min:.2f} V).",
        )
    if current > current_max:
        return RangeFitResult(
            RangeFitState.OUT_OF_ALL_RANGES,
            f"{current:.3f} A excede o limite superior definido nos parâmetros do "
            f"ensaio (Corrente máxima: {current_max:.3f} A).",
        )
    return RangeFitResult(RangeFitState.OK, "")


_SPIN_STYLE_BY_STATE = {
    RangeFitState.OK: "",
    RangeFitState.OUT_OF_FORCED_RANGE: f"border: 2px solid {_COLOR_WARNING};",
    RangeFitState.OUT_OF_ALL_RANGES: f"border: 2px solid {_COLOR_FAIL};",
}


def apply_spin_feedback(spin: QtWidgets.QAbstractSpinBox, result: RangeFitResult) -> None:
    spin.setStyleSheet(_SPIN_STYLE_BY_STATE[result.state])
    spin.setToolTip(result.message)


def apply_table_item_feedback(item: QtWidgets.QTableWidgetItem, result: RangeFitResult) -> None:
    if result.state is RangeFitState.OK:
        item.setBackground(QtGui.QBrush())
        item.setToolTip("")
        return
    color = _COLOR_WARNING if result.state is RangeFitState.OUT_OF_FORCED_RANGE else _COLOR_FAIL
    item.setBackground(QtGui.QBrush(QtGui.QColor(color)))
    item.setToolTip(result.message)


def build_range_combo(ranges: tuple[VoltageRange, ...]) -> QtWidgets.QComboBox:
    """Combo "Faixa da fonte" padrão: Automática + uma opção por faixa
    configurada (`userData` = None ou o nome da faixa, para uso direto com
    `evaluate_range_fit`/`PowerSupplyE363x.set_forced_range`).

    Compartilhado entre Saída manual e Parâmetros do ensaio para as duas
    telas nunca divergirem no texto/formatação apresentados ao operador.
    """
    combo = QtWidgets.QComboBox()
    combo.addItem("Automática (recomendado)", userData=None)
    for voltage_range in ranges:
        combo.addItem(
            f"{voltage_range.name} — até {voltage_range.max_voltage:.2f} V / "
            f"{voltage_range.max_current:.3f} A",
            userData=voltage_range.name,
        )
    combo.setEnabled(bool(ranges))
    return combo


def build_range_warning_label() -> QtWidgets.QLabel:
    """QLabel padrão para o aviso de faixa (mesmo objectName nas duas telas,
    para que uma regra de estilo QSS em `#rangeWarningLabel` afete as duas)."""
    label = QtWidgets.QLabel()
    label.setWordWrap(True)
    label.setObjectName("rangeWarningLabel")
    label.setVisible(False)
    return label
