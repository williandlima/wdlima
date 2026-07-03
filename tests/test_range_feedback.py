"""Testa a parte PURA de gui/widgets/range_feedback.py (sem Qt/offscreen).

A parte visual (cor/tooltip em QDoubleSpinBox/QTableWidgetItem) é coberta
pelos testes com marker 'gui' em test_gui_smoke.py / test_manual_output_range.py
/ test_test_parameters_view_range.py -- aqui só a lógica de classificação,
que precisa rodar em qualquer CI mesmo sem display.
"""
from __future__ import annotations

import itertools

import pytest

from config import VoltageRange
from gui.widgets.range_feedback import RangeFitState, evaluate_range_fit, evaluate_test_limit_fit
from hardware.power_supply import PowerSupplyE363x

_LOW = VoltageRange(name="LOW", max_voltage=25.0, max_current=7.0)
_HIGH = VoltageRange(name="HIGH", max_voltage=50.0, max_current=4.0)
_RANGES = (_LOW, _HIGH)


def test_no_ranges_configured_is_always_ok() -> None:
    result = evaluate_range_fit(999.0, 999.0, ())
    assert result.state is RangeFitState.OK
    assert result.message == ""


def test_fits_low_range_automatic_mode_is_ok() -> None:
    assert evaluate_range_fit(5.0, 1.0, _RANGES).state is RangeFitState.OK


def test_fits_high_but_not_low_automatic_mode_is_ok() -> None:
    """26V não cabe em LOW mas cabe em HIGH -- seleção automática cobre
    isso, então não é motivo de aviso (é exatamente o caso do fix da
    E3634A: 26V/1A deve ficar OK no modo automático)."""
    result = evaluate_range_fit(26.0, 1.0, _RANGES)
    assert result.state is RangeFitState.OK


def test_does_not_fit_any_range_automatic_mode_is_out_of_all() -> None:
    result = evaluate_range_fit(60.0, 1.0, _RANGES)
    assert result.state is RangeFitState.OUT_OF_ALL_RANGES
    assert "60.00" in result.message


def test_forced_range_that_fits_is_ok() -> None:
    result = evaluate_range_fit(5.0, 1.0, _RANGES, forced_range_name="HIGH")
    assert result.state is RangeFitState.OK


def test_forced_low_range_with_value_that_only_fits_high_warns() -> None:
    result = evaluate_range_fit(26.0, 1.0, _RANGES, forced_range_name="LOW")
    assert result.state is RangeFitState.OUT_OF_FORCED_RANGE
    assert "LOW" in result.message
    assert "HIGH" in result.message


def test_forced_range_with_value_that_fits_nothing_is_out_of_all() -> None:
    result = evaluate_range_fit(60.0, 1.0, _RANGES, forced_range_name="LOW")
    assert result.state is RangeFitState.OUT_OF_ALL_RANGES


def test_forced_range_name_that_does_not_exist_reports_the_config_typo() -> None:
    """Nome de faixa forçada inexistente ('MEDIUM', ex.: typo/faixa removida
    de app_config.yaml) não deve explodir, e precisa avisar da causa real
    (nome inválido) -- não mascarar como "valor não cabe", que sugeriria ao
    operador trocar de faixa quando na verdade a config está errada. Mesma
    mensagem que PowerSupplyE363x._ensure_range levantaria de verdade
    (as duas passam por classify_range_fit)."""
    result = evaluate_range_fit(5.0, 1.0, _RANGES, forced_range_name="MEDIUM")
    assert result.state is RangeFitState.OUT_OF_ALL_RANGES
    assert "MEDIUM" in result.message
    assert "não existe" in result.message


# -- evaluate_test_limit_fit: passo do ciclo x parâmetros do ensaio ----------


def test_step_within_test_parameters_is_ok() -> None:
    result = evaluate_test_limit_fit(5.0, 1.0, voltage_min=4.5, voltage_max=5.5, current_max=1.0)
    assert result.state is RangeFitState.OK
    assert result.message == ""


def test_step_voltage_above_test_maximum_warns_upper_limit() -> None:
    result = evaluate_test_limit_fit(8.0, 1.0, voltage_min=4.5, voltage_max=5.5, current_max=1.0)
    assert result.state is RangeFitState.OUT_OF_ALL_RANGES
    assert "8.00" in result.message
    assert "limite superior" in result.message
    assert "5.50" in result.message


def test_step_voltage_below_test_minimum_warns_lower_limit() -> None:
    result = evaluate_test_limit_fit(1.0, 1.0, voltage_min=4.5, voltage_max=5.5, current_max=1.0)
    assert result.state is RangeFitState.OUT_OF_ALL_RANGES
    assert "abaixo do limite inferior" in result.message
    assert "4.50" in result.message


def test_step_current_above_test_maximum_warns_upper_limit() -> None:
    result = evaluate_test_limit_fit(5.0, 2.0, voltage_min=4.5, voltage_max=5.5, current_max=1.0)
    assert result.state is RangeFitState.OUT_OF_ALL_RANGES
    assert "2.000" in result.message
    assert "limite superior" in result.message
    assert "1.000" in result.message


def test_step_exactly_at_the_boundary_is_ok() -> None:
    """Limites são inclusivos -- um passo igual ao mínimo/máximo declarado
    não é "extrapolar", é usar o próprio limite configurado."""
    assert evaluate_test_limit_fit(5.5, 1.0, voltage_min=4.5, voltage_max=5.5, current_max=1.0).state is RangeFitState.OK
    assert evaluate_test_limit_fit(4.5, 1.0, voltage_min=4.5, voltage_max=5.5, current_max=1.0).state is RangeFitState.OK
    assert evaluate_test_limit_fit(5.0, 1.0, voltage_min=4.5, voltage_max=5.5, current_max=1.0).state is RangeFitState.OK


# -- Guarda de regressão: GUI e driver NUNCA podem divergir -------------------
#
# evaluate_range_fit() e PowerSupplyE363x._ensure_range() decidiam a mesma
# coisa (o que cabe em qual faixa) com duas implementações escritas à mão em
# arquivos diferentes -- bastava uma diverência sutil para a pré-visualização
# da tela mostrar "OK" num setpoint que o instrumento real recusaria (ou
# vice-versa). Agora as duas chamam PowerSupplyE363x.classify_range_fit();
# este teste varre uma grade de casos (dentro/fora de cada faixa, forçada e
# automática, nome inválido) comparando OK/não-OK da GUI contra o que o
# driver realmente aceita/recusa, para travar essa equivalência no futuro.


@pytest.mark.parametrize(
    "volts,amps,forced_range_name",
    [
        (v, a, forced)
        for v, a in itertools.product((0.0, 5.0, 24.99, 25.0, 25.01, 26.0, 49.99, 50.0, 60.0), (0.5, 4.0, 4.01, 7.0, 7.01))
        for forced in (None, "LOW", "HIGH", "MEDIUM")
    ],
)
def test_gui_preview_never_diverges_from_the_real_driver_decision(
    volts: float, amps: float, forced_range_name: str | None
) -> None:
    gui_result = evaluate_range_fit(volts, amps, _RANGES, forced_range_name)
    driver_would_accept = (
        PowerSupplyE363x.classify_range_fit(volts, amps, _RANGES, forced_range_name)[0] is not None
    )
    assert (gui_result.state is RangeFitState.OK) == driver_would_accept, (
        f"GUI diz {gui_result.state} mas o driver "
        f"{'aceitaria' if driver_would_accept else 'recusaria'} {volts}V/{amps}A "
        f"(forçado={forced_range_name!r})"
    )
