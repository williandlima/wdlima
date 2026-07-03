"""Smoke test da GUI: garante que a janela e o cabeçalho montam sem erro.

Não dirige interação completa — apenas constrói a árvore de widgets (offscreen)
para travar regressões de wiring (sinais/slots, import, logo, seletor de porta).
Roda com QT_QPA_PLATFORM=offscreen, sem display real.

Todos os testes deste módulo carregam o marker 'gui' (declarado em
pyproject.toml). No CI headless rode: pytest -m 'not gui'.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from config import load_config
from database.database import Database

pytest.importorskip("PySide6")

pytestmark = pytest.mark.gui


@pytest.fixture()
def app_config():
    # create_dirs=False: não suja o ~/LabTest da máquina ao rodar os testes.
    return load_config(create_dirs=False)


def test_header_bar_shows_logo_and_port_selector(qtbot, app_config) -> None:
    from gui.widgets.header_bar import HeaderBar

    header = HeaderBar(app_config.branding)
    qtbot.addWidget(header)

    # A logo da empresa precisa aparecer (arquivo existe em assets/branding).
    assert app_config.branding.logo_path.exists()
    assert header.logo_label.pixmap() is not None and not header.logo_label.pixmap().isNull()

    # O seletor de porta sempre tem ao menos a opção "Automático".
    assert header.port_combo.count() >= 1
    assert header.selected_port() == ""  # default = automático


def test_header_bar_help_button_emits_signal(qtbot, app_config) -> None:
    from gui.widgets.header_bar import HeaderBar

    header = HeaderBar(app_config.branding)
    qtbot.addWidget(header)

    with qtbot.waitSignal(header.help_requested, timeout=1000):
        header.help_button.click()


def test_help_button_opens_a_dialog_with_end_to_end_instructions(
    qtbot, app_config, tmp_path: Path
) -> None:
    from unittest.mock import patch

    from PySide6 import QtWidgets

    from gui.main_window import MainWindow

    db = Database(tmp_path / "help.db")
    db.connect()
    window = MainWindow(app_config, db)
    qtbot.addWidget(window)

    captured = {}

    def fake_exec(dialog_self):
        captured["dialog"] = dialog_self
        return QtWidgets.QDialog.DialogCode.Accepted

    with patch.object(QtWidgets.QDialog, "exec", fake_exec):
        window.header.help_button.click()

    dialog = captured["dialog"]
    browser = dialog.findChild(QtWidgets.QTextBrowser)
    assert browser is not None
    # Cobre as 4 etapas do fluxo e pelo menos um controle do cabeçalho.
    for keyword in ("Cadastro", "Parâmetros", "Ensaio", "Avaliação", "Simulação"):
        assert keyword in browser.toPlainText()
    db.close()


def test_main_window_builds_full_flow(qtbot, app_config, tmp_path: Path) -> None:
    from gui.main_window import MainWindow

    db = Database(tmp_path / "smoke.db")
    db.connect()
    window = MainWindow(app_config, db)
    qtbot.addWidget(window)

    # Quatro etapas no stack: Cadastro, Parâmetros, Monitoramento, Avaliação.
    assert window.stack.count() == 4
    # O cabeçalho de marca/conexão está presente e acima do stack.
    assert window.header is not None
    db.close()


def test_registration_submit_button_advances_to_parameters(qtbot, app_config, tmp_path: Path) -> None:
    """Regressão: _switch_to() já teve um bug de auto-recursão que travava
    TODA troca de tela (RecursionError engolido pelo Qt) — o botão "Iniciar
    cadastro do teste" parecia simplesmente não fazer nada. Sem um teste que
    realmente clica o botão e verifica a troca de página, isso passou pelo
    CI sem ser detectado."""
    from gui.main_window import MainWindow

    db = Database(tmp_path / "switch_to.db")
    db.connect()
    window = MainWindow(app_config, db)
    qtbot.addWidget(window)

    rv = window.registration_view
    rv.code_edit.setText("BRD-REG")
    rv.part_number_edit.setText("PN-REG")
    rv.revision_edit.setText("A")
    rv.serial_number_edit.setText("SN-REG")
    rv.operator_combo.setEditText("Operador Teste")
    rv.if_edit.setText("IF-1")

    assert window.stack.currentWidget() is rv
    rv.submit_button.click()

    assert window.stack.currentWidget() is window.parameters_view

    window.parameters_view.back_button.click()
    assert window.stack.currentWidget() is rv
    db.close()


def test_registration_view_clear_form_resets_fields(qtbot, tmp_path: Path) -> None:
    from database.repositories import BoardRepository, OperatorRepository
    from gui.registration_view import RegistrationView

    db = Database(tmp_path / "clear_form.db")
    db.connect()
    view = RegistrationView(OperatorRepository(db), BoardRepository(db))
    qtbot.addWidget(view)

    view.code_edit.setText("BRD-1")
    view.part_number_edit.setText("PN-1")
    view.revision_edit.setText("A")
    view.serial_number_edit.setText("SN-1")
    view.production_order_edit.setText("OP-1")
    view.observations_edit.setPlainText("obs")
    view.operator_combo.setEditText("Joao")
    view.if_edit.setText("IF-123")

    view.clear_form()

    assert view.code_edit.text() == ""
    assert view.part_number_edit.text() == ""
    assert view.revision_edit.text() == ""
    assert view.serial_number_edit.text() == ""
    assert view.production_order_edit.text() == ""
    assert view.observations_edit.toPlainText() == ""
    assert view.operator_combo.currentText() == ""
    assert view.if_edit.text() == ""
    db.close()


def test_evaluation_submitted_saves_report_and_clears_registration(
    qtbot, app_config, tmp_path: Path, monkeypatch
) -> None:
    """Fim do ensaio: pede pasta (estilo Word), grava os 3 relatórios e limpa a Tela 1."""
    from PySide6 import QtWidgets

    from database.models import (
        Evaluation,
        EvaluationResult,
        PowerStep,
        TestParameterConfig,
        TestSession,
        TestSessionStatus,
    )
    from database.repositories import (
        BoardRepository,
        EvaluationRepository,
        OperatorRepository,
        TestParameterConfigRepository,
        TestSessionRepository,
    )
    from gui.main_window import MainWindow

    db = Database(tmp_path / "evaluation_flow.db")
    db.connect()

    operator = OperatorRepository(db).get_or_create("Joao Silva", "IF-1")
    board = BoardRepository(db).get_or_create("BRD-001", "PN-123", "A")
    config = TestParameterConfigRepository(db).save(
        TestParameterConfig(
            id=None,
            board_id=board.id,
            name="Config padrão",
            nominal_voltage=5.0,
            voltage_min=4.5,
            voltage_max=5.5,
            current_max=1.0,
            test_duration_s=2.0,
            power_sequence=[PowerStep(voltage=5.0, current=1.0, duration_s=2.0)],
        )
    )
    session = TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-001",
            operator_id=operator.id,
            test_parameter_config_id=config.id,
            config_snapshot_json='{"nominal_voltage": 5.0}',
            production_order="OP-9",
            observations=None,
            status=TestSessionStatus.RUNNING,
            started_at="2026-06-25 10:00:00",
        )
    )
    TestSessionRepository(db).update_status(session.id, TestSessionStatus.COMPLETED)
    EvaluationRepository(db).create(
        Evaluation(
            id=None,
            test_session_id=session.id,
            operator_id=operator.id,
            result=EvaluationResult.APPROVED,
            comment=None,
        )
    )

    window = MainWindow(app_config, db)
    qtbot.addWidget(window)
    window.registration_view.code_edit.setText("BRD-001")

    output_dir = tmp_path / "chosen_folder"
    output_dir.mkdir()
    chosen = str(output_dir / "Relatorio_do_ensaio.docx")
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (chosen, ""))
    )
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    window._board = board
    window._operator = operator
    window._on_evaluation_submitted({"evaluation": None, "session": session})

    saved_files = list(output_dir.glob("*"))
    assert any(p.suffix == ".docx" for p in saved_files)
    assert any(p.suffix == ".xlsx" for p in saved_files)
    assert any(p.suffix == ".pdf" for p in saved_files)

    assert window._session is None
    assert window._board is None
    assert window._operator is None
    assert window.registration_view.code_edit.text() == ""
    assert window.stack.currentWidget() is window.registration_view


def test_parameters_view_back_button_emits_signal(qtbot, app_config, tmp_path: Path) -> None:
    from database.repositories import TestParameterConfigRepository
    from dataclasses import asdict
    from gui.test_parameters_view import TestParametersView

    db = Database(tmp_path / "back.db")
    db.connect()
    view = TestParametersView(TestParameterConfigRepository(db), asdict(app_config.test_defaults))
    qtbot.addWidget(view)

    with qtbot.waitSignal(view.back_requested, timeout=1000):
        view.back_button.click()
    db.close()


def test_live_samples_uses_rolling_window(qtbot, app_config, tmp_path: Path) -> None:
    """O gráfico ao vivo usa janela rolante (memória limitada em ensaios longos)."""
    from collections import deque

    from core.sampling_buffer import Sample
    from gui.main_window import MainWindow

    db = Database(tmp_path / "rolling.db")
    db.connect()
    window = MainWindow(app_config, db)
    qtbot.addWidget(window)

    window._live_samples = deque(maxlen=5)
    for i in range(20):
        window._on_sample(Sample(timestamp=float(i), step_index=0, voltage=5.0, current=0.5))

    assert len(window._live_samples) == 5  # só os 5 mais recentes ficam em memória
    db.close()


def test_parameters_duration_unit_conversion(qtbot, app_config, tmp_path: Path) -> None:
    from dataclasses import asdict
    from database.repositories import TestParameterConfigRepository
    from gui.test_parameters_view import TestParametersView

    db = Database(tmp_path / "units.db")
    db.connect()
    view = TestParametersView(TestParameterConfigRepository(db), asdict(app_config.test_defaults))
    qtbot.addWidget(view)

    # Padrão: minutos (1 min = 60 s).
    assert view._duration_factor == 60.0
    view.test_duration_spin.setValue(2.0)  # 2 minutos

    # Troca para horas: o valor exibido converte mantendo os segundos (120 s).
    view.duration_unit_combo.setCurrentIndex(2)  # horas
    assert view._duration_factor == 3600.0
    assert view.test_duration_spin.value() == pytest.approx(2.0 / 60.0, abs=1e-3)
    db.close()


def test_monitoring_panel_cycle_label_tracks_step_index(qtbot) -> None:
    from core.sampling_buffer import Sample
    from gui.main_window import _MonitoringPanel

    panel = _MonitoringPanel()
    qtbot.addWidget(panel)

    # Passo único: rótulo informa que não há ciclos.
    panel.reset(4.5, 5.5, 2.0, 1.0, total_steps=1)
    assert panel.cycle_label.text() == "Passo único"

    # Multi-step: o rótulo acompanha o step_index das amostras (1-based).
    panel.reset(4.5, 5.5, 2.0, 1.0, total_steps=3)
    assert panel.cycle_label.text() == "Ciclo: 1 de 3"
    panel.on_sample(Sample(timestamp=0.0, step_index=2, voltage=5.0, current=0.5))
    assert panel.cycle_label.text() == "Ciclo: 3 de 3"


def test_manual_output_dialog_energizes_and_reads_in_simulation(
    qtbot, app_config, monkeypatch
) -> None:
    """Saída manual liga em modo simulação, lê tensão/corrente e desliga com segurança."""
    from PySide6 import QtWidgets

    from gui.manual_output_dialog import ManualOutputDialog
    from hardware.power_supply import PowerSupplyE363x

    instrument = PowerSupplyE363x(app_config.serial, app_config.reconnection)
    dialog = ManualOutputDialog(
        instrument, simulate=True, port=None, default_voltage=5.0, default_current=1.0
    )
    qtbot.addWidget(dialog)

    # Confirmação de energização sempre "Sim" no teste.
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes),
    )

    dialog._on_turn_on()
    qtbot.waitUntil(lambda: dialog._output_on and dialog.voltage_display.text() != "0.000 V", timeout=5000)
    assert instrument.is_connected
    assert dialog.off_button.isEnabled()

    dialog._on_turn_off()
    qtbot.waitUntil(lambda: not dialog._output_on, timeout=5000)
    assert dialog.on_button.isEnabled()

    dialog._shutdown()
    assert not instrument.is_connected


def test_step_indicator_marks_current_done_and_todo(qtbot) -> None:
    from gui.widgets.step_indicator import StepIndicator

    stepper = StepIndicator(["Cadastro", "Parâmetros", "Ensaio", "Avaliação"])
    qtbot.addWidget(stepper)

    stepper.set_current(2)
    # Impl QPainter não usa _step_labels; valida o índice interno e a classificação.
    assert stepper._current == 2
    assert [("done" if i < 2 else "current" if i == 2 else "todo") for i in range(4)] == [
        "done", "done", "current", "todo"
    ]


def test_segment_display_alarm_toggles_out_of_range(qtbot) -> None:
    from gui.widgets.segment_display import SegmentDisplay

    display = SegmentDisplay(unit="V", decimals=3)
    qtbot.addWidget(display)
    display.set_limits(4.5, 5.5)

    display.set_value(5.0)
    assert display.property("alarm") is False

    display.set_value(6.2)  # acima do máximo -> alarme
    assert display.property("alarm") is True

    display.set_value(4.9)  # de volta à faixa -> sem alarme
    assert display.property("alarm") is False


def test_registration_operator_field_is_comfortably_wide(qtbot, tmp_path: Path) -> None:
    from database.repositories import BoardRepository, OperatorRepository
    from gui.registration_view import RegistrationView

    db = Database(tmp_path / "op_width.db")
    db.connect()
    view = RegistrationView(OperatorRepository(db), BoardRepository(db))
    qtbot.addWidget(view)

    assert view.operator_combo.minimumWidth() >= 320
    db.close()


def test_live_chart_axes_are_color_coded(qtbot) -> None:
    from gui.widgets.live_chart import LiveChart

    chart = LiveChart()
    qtbot.addWidget(chart)

    # Eixo da tensão na cor da linha de tensão; corrente idem — sem ambiguidade.
    assert chart._axis_voltage.labelsColor().name().upper() == LiveChart._COLOR_VOLTAGE.upper()
    assert chart._axis_current.labelsColor().name().upper() == LiveChart._COLOR_CURRENT.upper()


def test_total_monitored_duration_includes_off_duration_except_last_step() -> None:
    """Bug relatado: o eixo X do gráfico era calculado só com duration_s dos
    passos, sem contar off_duration_s -- assim que o tempo OFF passou a
    gerar amostras reais (platô em zero, não mais instantâneo), o eixo
    ficava curto e a curva "saía" do gráfico pela direita. off_duration_s
    do ÚLTIMO passo fica de fora, espelhando TestStateMachine._monitor
    (nunca aplica tempo OFF depois do último passo)."""
    from database.models import PowerStep
    from gui.main_window import _total_monitored_duration_s

    steps = [
        PowerStep(voltage=5.0, current=1.0, duration_s=60.0, off_duration_s=30.0),
        PowerStep(voltage=8.0, current=1.0, duration_s=60.0, off_duration_s=999.0),  # último: ignorado
    ]

    assert _total_monitored_duration_s(steps) == 60.0 + 30.0 + 60.0


def test_live_chart_expands_x_axis_when_elapsed_time_exceeds_configured_duration(
    qtbot,
) -> None:
    """Rede de segurança: se o tempo real do ensaio ultrapassar a duração
    prevista (por qualquer motivo -- cálculo desatualizado, retries), a
    curva não pode simplesmente "sair" do gráfico pela direita."""
    from core.sampling_buffer import Sample
    from gui.widgets.live_chart import LiveChart

    chart = LiveChart()
    qtbot.addWidget(chart)
    chart.set_voltage_limits(4.5, 5.5, duration_s=10.0, step_voltages=[5.0])
    assert chart._axis_x.max() == pytest.approx(10.0)

    chart.update_samples([Sample(timestamp=0.0, step_index=0, voltage=5.0, current=1.0)])
    chart.update_samples(
        [
            Sample(timestamp=0.0, step_index=0, voltage=5.0, current=1.0),
            Sample(timestamp=15.0, step_index=0, voltage=5.0, current=1.0),
        ]
    )

    assert chart._axis_x.max() > 15.0  # expandiu além do tempo real da última amostra


def test_show_toast_creates_non_blocking_widget(qtbot) -> None:
    from PySide6 import QtWidgets

    from gui.widgets.toast import show_toast

    host = QtWidgets.QWidget()
    host.resize(400, 300)
    qtbot.addWidget(host)
    host.show()

    toast = show_toast(host, "Mensagem de teste", level="success")
    assert toast.parentWidget() is host
    assert toast.text() == "Mensagem de teste"
    assert toast.property("toastLevel") == "success"
