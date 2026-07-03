"""Testes 'gui' (offscreen) da seleção de faixa e aviso visual em tempo real
nos Parâmetros do ensaio — mesmo pedido do operador atendido em
test_manual_output_range.py, mas para o passo único (Tensão nominal/Corrente
máxima) e para a tabela de sequência multi-step.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6")

pytestmark = pytest.mark.gui

from PySide6 import QtWidgets

from config import VoltageRange, load_config
from database.database import Database
from database.repositories import BoardRepository, TestParameterConfigRepository
from gui.test_parameters_view import TestParametersView

_LOW = VoltageRange(name="LOW", max_voltage=25.0, max_current=7.0)
_HIGH = VoltageRange(name="HIGH", max_voltage=50.0, max_current=4.0)


@pytest.fixture()
def app_config():
    return load_config(create_dirs=False)


def _view(qtbot, tmp_path: Path, app_config, ranges=(_LOW, _HIGH)) -> tuple[TestParametersView, Database]:
    db = Database(tmp_path / "params_range.db")
    db.connect()
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    view = TestParametersView(
        TestParameterConfigRepository(db), asdict(app_config.test_defaults), ranges=ranges
    )
    qtbot.addWidget(view)
    view.show()
    view.set_board(board)
    return view, db


def test_range_combo_lists_automatic_plus_each_configured_range(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    labels = [view.range_combo.itemText(i) for i in range(view.range_combo.count())]
    assert labels[0].startswith("Automática")
    assert any("LOW" in label for label in labels)
    assert any("HIGH" in label for label in labels)
    db.close()


def test_single_step_value_out_of_all_ranges_warns(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    view.nominal_voltage_spin.setValue(5.0)
    view.current_max_spin.setValue(10.0)  # excede LOW (7A) e HIGH (4A)

    assert view.range_warning_label.isVisible() is True
    assert "10.000" in view.range_warning_label.text()
    assert "border" in view.current_max_spin.styleSheet()
    db.close()


def test_single_step_value_out_of_range_blocks_submit(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    view.config_name_edit.setText("Config inválida")
    view.nominal_voltage_spin.setValue(5.0)
    view.current_max_spin.setValue(10.0)  # não cabe em nenhuma faixa

    with patch.object(QtWidgets.QMessageBox, "warning") as mock_warning:
        with qtbot.assertNotEmitted(view.parameters_submitted, wait=200):
            view.submit_button.click()
    mock_warning.assert_called_once()
    assert "Faixa V/A" in mock_warning.call_args.args[1]
    db.close()


def test_forcing_low_range_colors_sequence_row_that_only_fits_high(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    low_index = next(i for i in range(view.range_combo.count()) if view.range_combo.itemData(i) == "LOW")
    view.range_combo.setCurrentIndex(low_index)

    view.nominal_voltage_spin.setValue(26.0)  # só cabe em HIGH
    view.current_max_spin.setValue(1.0)
    view.add_step_button.click()  # adiciona passo com os valores acima

    voltage_item = view.sequence_table.item(0, 0)
    current_item = view.sequence_table.item(0, 1)
    assert voltage_item.toolTip() != ""
    assert "HIGH" in voltage_item.toolTip()
    assert current_item.background().color().name() != "#000000"
    db.close()


def test_sequence_row_out_of_range_blocks_submit(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    view.config_name_edit.setText("Sequência inválida")
    view.voltage_min_spin.setValue(4.5)
    view.voltage_max_spin.setValue(5.5)  # 5V do passo 1 cabe dentro dos parâmetros do ensaio
    view.nominal_voltage_spin.setValue(5.0)
    view.current_max_spin.setValue(1.0)
    view.add_step_button.click()  # passo 1: válido (5V/1A)

    view.current_max_spin.setValue(10.0)  # não cabe em nenhuma faixa
    view.add_step_button.click()  # passo 2: inválido

    with patch.object(QtWidgets.QMessageBox, "warning") as mock_warning:
        with qtbot.assertNotEmitted(view.parameters_submitted, wait=200):
            view.submit_button.click()
    mock_warning.assert_called_once()
    assert "Passo 2" in mock_warning.call_args.args[2]
    db.close()


def test_sequence_step_above_test_voltage_maximum_warns_and_blocks(qtbot, app_config, tmp_path: Path) -> None:
    """Passo do CICLO com tensão que cabe na fonte, mas ultrapassa a Tensão
    máxima definida nos parâmetros do próprio ensaio -- precisa avisar e
    bloquear mesmo sem nenhum problema de faixa de hardware."""
    view, db = _view(qtbot, tmp_path, app_config)
    view.config_name_edit.setText("Passo além dos parâmetros")
    view.voltage_min_spin.setValue(4.5)
    view.voltage_max_spin.setValue(5.5)
    view.current_max_spin.setValue(1.0)
    view.nominal_voltage_spin.setValue(8.0)  # cabe em LOW/HIGH, mas > 5.5V do ensaio
    view.add_step_button.click()

    voltage_item = view.sequence_table.item(0, 0)
    assert "limite superior" in voltage_item.toolTip()
    assert "5.50" in voltage_item.toolTip()

    with patch.object(QtWidgets.QMessageBox, "warning") as mock_warning:
        with qtbot.assertNotEmitted(view.parameters_submitted, wait=200):
            view.submit_button.click()
    mock_warning.assert_called_once()
    message = mock_warning.call_args.args[2]
    assert "Passo 1" in message
    assert "limite superior" in message
    db.close()


def test_sequence_step_above_test_current_maximum_warns_and_blocks(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    view.config_name_edit.setText("Corrente além dos parâmetros")
    view.voltage_min_spin.setValue(4.5)
    view.voltage_max_spin.setValue(5.5)
    view.nominal_voltage_spin.setValue(5.0)
    view.current_max_spin.setValue(1.0)
    view.add_step_button.click()  # passo 1: dentro dos limites (5V/1A)

    view.sequence_table.item(0, 1).setText("3.0")  # excede a Corrente máxima (1A) do ensaio

    with patch.object(QtWidgets.QMessageBox, "warning") as mock_warning:
        with qtbot.assertNotEmitted(view.parameters_submitted, wait=200):
            view.submit_button.click()
    mock_warning.assert_called_once()
    message = mock_warning.call_args.args[2]
    assert "limite superior" in message
    assert "Corrente máxima" in message
    db.close()


def test_single_step_mode_ignores_test_voltage_limits(qtbot, app_config, tmp_path: Path) -> None:
    """Sem sequência (passo único), Tensão mínima/máxima continuam sendo só
    referência visual do gráfico -- essa checagem nova só vale para ciclos."""
    view, db = _view(qtbot, tmp_path, app_config)
    view.config_name_edit.setText("Passo único fora da referência")
    view.voltage_min_spin.setValue(4.5)
    view.voltage_max_spin.setValue(5.5)
    view.current_max_spin.setValue(1.0)
    view.nominal_voltage_spin.setValue(8.0)  # fora da referência, mas cabe na fonte

    with qtbot.waitSignal(view.parameters_submitted, timeout=1000):
        view.submit_button.click()
    db.close()


def test_submit_persists_range_mode_and_forwards_to_run_config(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    high_index = next(i for i in range(view.range_combo.count()) if view.range_combo.itemData(i) == "HIGH")
    view.range_combo.setCurrentIndex(high_index)
    view.config_name_edit.setText("Config HIGH")
    view.nominal_voltage_spin.setValue(30.0)
    view.current_max_spin.setValue(2.0)

    with qtbot.waitSignal(view.parameters_submitted, timeout=1000) as blocker:
        view.submit_button.click()

    payload = blocker.args[0]
    assert payload["test_parameter_config"].range_mode == "HIGH"
    assert payload["run_config"].range_mode == "HIGH"

    reloaded = TestParameterConfigRepository(db).get(payload["test_parameter_config"].id)
    assert reloaded.range_mode == "HIGH"
    db.close()


def test_loading_history_restores_range_mode_selection(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    high_index = next(i for i in range(view.range_combo.count()) if view.range_combo.itemData(i) == "HIGH")
    view.range_combo.setCurrentIndex(high_index)
    view.config_name_edit.setText("Config para recarregar")
    view.nominal_voltage_spin.setValue(30.0)
    view.current_max_spin.setValue(2.0)
    with qtbot.waitSignal(view.parameters_submitted, timeout=1000):
        view.submit_button.click()

    # Simula reabrir a tela com "Automática" selecionada, depois carregar o histórico.
    view.range_combo.setCurrentIndex(0)
    view.history_combo.setCurrentIndex(
        next(i for i in range(view.history_combo.count()) if view.history_combo.itemText(i) == "Config para recarregar")
    )
    view.load_button.click()

    assert view.range_combo.currentData() == "HIGH"
    db.close()


def test_no_ranges_configured_disables_combo_and_never_warns(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config, ranges=())
    assert view.range_combo.isEnabled() is False
    view.current_max_spin.setValue(20.0)  # qualquer valor -- gerenciamento desligado
    assert view.range_warning_label.isVisible() is False
    db.close()
