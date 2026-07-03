"""Testes 'gui' (offscreen) da coluna "Tempo OFF" na sequência multi-step de
Parâmetros do ensaio -- operador insere um tempo opcional de saída DESLIGADA
entre passos ("ciclos"), coberto em core/state_machine.py::_apply_off_period.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

pytestmark = pytest.mark.gui

from config import load_config
from database.database import Database
from database.repositories import BoardRepository, TestParameterConfigRepository
from gui.test_parameters_view import TestParametersView


@pytest.fixture()
def app_config():
    return load_config(create_dirs=False)


def _view(qtbot, tmp_path: Path, app_config) -> tuple[TestParametersView, Database]:
    db = Database(tmp_path / "off_duration.db")
    db.connect()
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    view = TestParametersView(TestParameterConfigRepository(db), asdict(app_config.test_defaults))
    qtbot.addWidget(view)
    view.set_board(board)
    return view, db


def test_sequence_table_has_a_time_off_column(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    headers = [view.sequence_table.horizontalHeaderItem(i).text() for i in range(view.sequence_table.columnCount())]
    assert any("Tempo OFF" in h for h in headers)
    db.close()


def test_add_step_defaults_time_off_to_zero(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    view.add_step_button.click()
    assert view.sequence_table.item(0, 3).text() == "0.0"
    db.close()


def test_negative_time_off_blocks_submit(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    view.config_name_edit.setText("Sequência com tempo OFF inválido")
    view.add_step_button.click()
    view.sequence_table.item(0, 3).setText("-5.0")

    from unittest.mock import patch

    from PySide6 import QtWidgets

    with patch.object(QtWidgets.QMessageBox, "warning") as mock_warning:
        with qtbot.assertNotEmitted(view.parameters_submitted, wait=200):
            view.submit_button.click()
    mock_warning.assert_called_once()
    db.close()


def test_time_off_survives_save_and_reload_roundtrip(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    view.config_name_edit.setText("Ciclo térmico")
    view.voltage_min_spin.setValue(4.5)
    view.voltage_max_spin.setValue(5.5)
    view.current_max_spin.setValue(1.0)
    view.add_step_button.click()
    view.sequence_table.item(0, 0).setText("5.0")
    view.sequence_table.item(0, 1).setText("1.0")
    view.sequence_table.item(0, 2).setText("2.0")
    view.sequence_table.item(0, 3).setText("1.5")

    with qtbot.waitSignal(view.parameters_submitted, timeout=1000) as blocker:
        view.submit_button.click()

    payload = blocker.args[0]
    saved_step = payload["test_parameter_config"].power_sequence[0]
    assert saved_step.off_duration_s == pytest.approx(1.5 * view._duration_factor)

    run_step = payload["run_config"].power_sequence[0]
    assert run_step.off_duration_s == pytest.approx(1.5 * view._duration_factor)
    db.close()


def test_changing_duration_unit_converts_time_off_column_too(qtbot, app_config, tmp_path: Path) -> None:
    view, db = _view(qtbot, tmp_path, app_config)
    view.duration_unit_combo.setCurrentIndex(0)  # segundos, pra partir de uma unidade conhecida
    view.add_step_button.click()
    view.sequence_table.item(0, 3).setText("120")  # 120 s

    view.duration_unit_combo.setCurrentIndex(1)  # minutos

    assert float(view.sequence_table.item(0, 3).text()) == pytest.approx(2.0)  # 120s = 2min
    db.close()
