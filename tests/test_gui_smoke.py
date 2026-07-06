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
    # "Testar outra unidade desta placa?" -- "Não" preserva o comportamento
    # coberto por este teste (cadastro volta em branco).
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No),
    )

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


def test_evaluation_submitted_without_save_report_skips_file_dialog(
    qtbot, app_config, tmp_path: Path, monkeypatch
) -> None:
    """"Não" (não salvar e encerrar ensaio): finaliza e limpa a Tela 1 igual
    ao fluxo de salvar, mas sem abrir o diálogo "onde salvar" nem gerar
    nenhum arquivo de relatório."""
    from PySide6 import QtWidgets

    from database.models import (
        Evaluation,
        EvaluationResult,
        TestSession,
        TestSessionStatus,
    )
    from database.repositories import (
        BoardRepository,
        EvaluationRepository,
        OperatorRepository,
        TestSessionRepository,
    )
    from gui.main_window import MainWindow

    db = Database(tmp_path / "evaluation_flow_no_save.db")
    db.connect()

    operator = OperatorRepository(db).get_or_create("Joao Silva", "IF-1")
    board = BoardRepository(db).get_or_create("BRD-001", "PN-123", "A")
    session = TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-002",
            operator_id=operator.id,
            test_parameter_config_id=None,
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

    save_dialog_calls = []
    monkeypatch.setattr(
        QtWidgets.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: save_dialog_calls.append(1) or ("", "")),
    )
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No),
    )

    window._board = board
    window._operator = operator
    window._on_evaluation_submitted({"evaluation": None, "session": session, "save_report": False})

    assert save_dialog_calls == []  # nenhum diálogo "onde salvar" foi aberto
    assert window._session is None
    assert window.stack.currentWidget() is window.registration_view


def test_evaluation_view_asks_save_choice_before_committing(qtbot, tmp_path: Path, monkeypatch) -> None:
    """"Confirmar avaliação" pergunta Sim/Não/Cancelar ANTES de gravar a
    Evaluation e liberar o state machine -- Cancelar precisa voltar para a
    tela de ensaio sem nenhuma escrita no banco, não só pular o relatório."""
    from unittest.mock import MagicMock

    from PySide6 import QtWidgets

    from core.state_machine import TestState, TestStateMachine
    from database.models import TestSession, TestSessionStatus
    from database.repositories import (
        BoardRepository,
        EvaluationRepository,
        OperatorRepository,
        TestSessionRepository,
    )
    from gui.evaluation_view import EvaluationView

    db = Database(tmp_path / "evaluation_view.db")
    db.connect()
    board = BoardRepository(db).get_or_create("BRD-001", "PN-123", "A")
    operator = OperatorRepository(db).get_or_create("Joao Silva")
    evaluation_repo = EvaluationRepository(db)

    def build_view(serial: str):
        session = TestSessionRepository(db).create(
            TestSession(
                id=None,
                board_id=board.id,
                serial_number=serial,
                operator_id=operator.id,
                test_parameter_config_id=None,
                config_snapshot_json="{}",
                production_order=None,
                observations=None,
                status=TestSessionStatus.COMPLETED,
            )
        )
        state_machine = MagicMock(spec=TestStateMachine)
        state_machine.state = TestState.AWAITING_MANUAL_EVALUATION
        state_machine.termination_reason = None
        view = EvaluationView(evaluation_repo)
        qtbot.addWidget(view)
        view.load_session(session, operator, state_machine, [])
        view.approved_radio.setChecked(True)
        return view, state_machine, session

    # Cancelar: nenhuma gravação, nenhum sinal emitido, state machine intocado.
    view, sm, session = build_view("SN-CANCEL")
    emitted = []
    view.evaluation_submitted.connect(emitted.append)
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Cancel),
    )
    view._on_submit()
    assert emitted == []
    assert evaluation_repo.get_for_session(session.id) is None
    sm.mark_evaluated.assert_not_called()

    # Não: grava a avaliação e libera o state machine, mas save_report=False.
    view, sm, session = build_view("SN-NO")
    emitted = []
    view.evaluation_submitted.connect(emitted.append)
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No),
    )
    view._on_submit()
    assert len(emitted) == 1
    assert emitted[0]["save_report"] is False
    assert evaluation_repo.get_for_session(session.id) is not None
    sm.mark_evaluated.assert_called_once()

    # Sim: idem, mas save_report=True.
    view, sm, session = build_view("SN-YES")
    emitted = []
    view.evaluation_submitted.connect(emitted.append)
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes),
    )
    view._on_submit()
    assert len(emitted) == 1
    assert emitted[0]["save_report"] is True
    assert evaluation_repo.get_for_session(session.id) is not None
    sm.mark_evaluated.assert_called_once()


def test_evaluation_redo_button_asks_confirmation_before_emitting(qtbot, tmp_path: Path, monkeypatch) -> None:
    """"Refazer ensaio": só emite redo_requested se o operador confirmar --
    "Não" precisa deixar a tela intocada, sem descartar nada silenciosamente."""
    from PySide6 import QtWidgets

    from core.state_machine import TestState, TestStateMachine
    from database.models import TestSession, TestSessionStatus
    from database.repositories import (
        BoardRepository,
        EvaluationRepository,
        OperatorRepository,
        TestSessionRepository,
    )
    from gui.evaluation_view import EvaluationView
    from unittest.mock import MagicMock

    db = Database(tmp_path / "evaluation_redo.db")
    db.connect()
    board = BoardRepository(db).get_or_create("BRD-001", "PN-123", "A")
    operator = OperatorRepository(db).get_or_create("Joao Silva")
    session = TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-REDO",
            operator_id=operator.id,
            test_parameter_config_id=None,
            config_snapshot_json="{}",
            production_order=None,
            observations=None,
            status=TestSessionStatus.COMPLETED,
        )
    )
    state_machine = MagicMock(spec=TestStateMachine)
    state_machine.state = TestState.AWAITING_MANUAL_EVALUATION
    state_machine.termination_reason = None
    view = EvaluationView(EvaluationRepository(db))
    qtbot.addWidget(view)
    view.load_session(session, operator, state_machine, [])

    emitted = []
    view.redo_requested.connect(lambda: emitted.append(1))

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No),
    )
    view.redo_button.click()
    assert emitted == []
    state_machine.mark_evaluated.assert_not_called()  # não avaliou, só recusou refazer

    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes),
    )
    view.redo_button.click()
    assert emitted == [1]
    db.close()


def test_main_window_redo_requested_restarts_with_same_config(
    qtbot, app_config, tmp_path: Path, monkeypatch
) -> None:
    """MainWindow._on_redo_requested reaproveita placa/operador/S/N e a última
    config usada, sem passar de novo por Cadastro/Parâmetros -- verifica só o
    encaminhamento de dados (o disparo real do ensaio já é responsabilidade
    de _on_parameters_submitted, testado à parte)."""
    from database.models import Board, Operator, PowerStep, TestParameterConfig
    from gui.main_window import MainWindow

    db = Database(tmp_path / "redo.db")
    db.connect()
    window = MainWindow(app_config, db)
    qtbot.addWidget(window)

    board = Board(id=1, code="BRD-001", part_number="PN-123", revision="A")
    operator = Operator(id=1, name="Joao Silva", if_number="IF-1")
    config = TestParameterConfig(
        id=1, board_id=1, name="Config", nominal_voltage=5.0, voltage_min=4.5,
        voltage_max=5.5, current_max=1.0, test_duration_s=2.0,
        power_sequence=[PowerStep(voltage=5.0, current=1.0, duration_s=2.0)],
    )
    window._board = board
    window._operator = operator
    window._registration_data = {"serial_number": "SN-REDO", "production_order": None, "observations": None}
    window._last_test_parameter_config = config

    calls = []
    monkeypatch.setattr(window, "_on_parameters_submitted", lambda data: calls.append(data))
    from core.state_machine import TestRunConfig

    run_config = TestRunConfig(
        nominal_voltage=5.0, voltage_min=4.5, voltage_max=5.5, current_max=1.0,
        test_duration_s=2.0, power_sequence=config.power_sequence,
        polling_rate_hz=1.0, stabilization_timeout_s=5.0,
        stabilization_tolerance_v=0.05, monitoring_consecutive_failures_limit=3,
    )
    window._last_run_config = run_config

    window._on_redo_requested()

    assert len(calls) == 1
    assert calls[0]["test_parameter_config"] is config
    assert calls[0]["run_config"] is run_config
    db.close()


def test_evaluation_submitted_offers_next_unit_of_same_board(
    qtbot, app_config, tmp_path: Path, monkeypatch
) -> None:
    """"Testar outra unidade desta placa?" -> "Sim" reaproveita placa/operador
    (só o S/N fica em branco) em vez de voltar ao cadastro totalmente vazio."""
    from PySide6 import QtWidgets

    from database.models import TestSession, TestSessionStatus
    from database.repositories import BoardRepository, OperatorRepository, TestSessionRepository
    from gui.main_window import MainWindow

    db = Database(tmp_path / "next_unit.db")
    db.connect()
    operator = OperatorRepository(db).get_or_create("Joao Silva", "IF-1")
    board = BoardRepository(db).get_or_create("BRD-001", "PN-123", "A")
    session = TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-001",
            operator_id=operator.id,
            test_parameter_config_id=None,
            config_snapshot_json="{}",
            production_order=None,
            observations=None,
            status=TestSessionStatus.COMPLETED,
        )
    )

    window = MainWindow(app_config, db)
    qtbot.addWidget(window)
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes),
    )

    window._board = board
    window._operator = operator
    window._on_evaluation_submitted({"evaluation": None, "session": session, "save_report": False})

    assert window.stack.currentWidget() is window.registration_view
    assert window.registration_view.code_edit.text() == "BRD-001"
    assert window.registration_view.part_number_edit.text() == "PN-123"
    assert window.registration_view.serial_number_edit.text() == ""
    assert window.registration_view.operator_combo.currentText() == "Joao Silva"
    db.close()


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


def test_save_only_button_saves_without_starting_test(qtbot, app_config, tmp_path: Path) -> None:
    """"Salvar configuração": grava no histórico e avisa via config_saved,
    mas NÃO dispara parameters_submitted -- o operador pode montar os
    parâmetros agora e rodar o ensaio depois, sem preencher tudo de novo."""
    from dataclasses import asdict

    from database.repositories import BoardRepository, TestParameterConfigRepository
    from gui.test_parameters_view import TestParametersView

    db = Database(tmp_path / "save_only.db")
    db.connect()
    board = BoardRepository(db).get_or_create("BRD-SAVE", "PN-1", "A")
    view = TestParametersView(TestParameterConfigRepository(db), asdict(app_config.test_defaults))
    qtbot.addWidget(view)
    view.set_board(board)

    view.config_name_edit.setText("Config rascunho")
    view.nominal_voltage_spin.setValue(12.0)
    view.current_max_spin.setValue(1.5)

    started = []
    saved_names = []
    view.parameters_submitted.connect(lambda data: started.append(data))
    view.config_saved.connect(saved_names.append)

    view.save_only_button.click()

    assert saved_names == ["Config rascunho"]
    assert started == []  # não iniciou o ensaio
    assert view.history_combo.count() == 1
    assert view.history_combo.itemText(0) == "Config rascunho"
    db.close()


def test_set_board_autoloads_most_recent_saved_config(qtbot, app_config, tmp_path: Path) -> None:
    """Voltar para uma placa que já tem configuração salva pré-preenche o
    formulário sozinho -- o operador não precisa clicar em "Carregar" nem
    redigitar nada que já tinha configurado antes."""
    from dataclasses import asdict

    from database.models import PowerStep, TestParameterConfig
    from database.repositories import BoardRepository, TestParameterConfigRepository
    from gui.test_parameters_view import TestParametersView

    db = Database(tmp_path / "autoload.db")
    db.connect()
    board = BoardRepository(db).get_or_create("BRD-AUTO", "PN-1", "A")
    config_repo = TestParameterConfigRepository(db)
    config_repo.save(
        TestParameterConfig(
            id=None, board_id=board.id, name="Última usada", nominal_voltage=9.0,
            voltage_min=8.5, voltage_max=9.5, current_max=2.0, test_duration_s=120.0,
            power_sequence=[PowerStep(voltage=9.0, current=2.0, duration_s=120.0)],
        )
    )

    view = TestParametersView(config_repo, asdict(app_config.test_defaults))
    qtbot.addWidget(view)

    view.set_board(board)  # sem clicar em "Carregar"

    assert view.config_name_edit.text() == "Última usada"
    assert view.nominal_voltage_spin.value() == pytest.approx(9.0)
    assert view.current_max_spin.value() == pytest.approx(2.0)
    db.close()


def test_live_chart_draws_multiple_points_from_absolute_timestamps(
    qtbot, app_config, tmp_path: Path
) -> None:
    """Erro crítico (gráfico "parou de funcionar"): as amostras reais têm
    timestamp ABSOLUTO (time.time()), não decorrido. Ao alimentar o gráfico
    via _on_sample (o caminho real), a curva PRECISA ganhar vários pontos --
    o bug fazia todas as amostras caírem num bin só, e o gráfico virava um
    único ponto. Testa o pipeline de ponta a ponta (não só o histórico)."""
    import time

    from core.live_chart_history import LiveChartHistory
    from core.sampling_buffer import Sample
    from gui.main_window import MainWindow

    db = Database(tmp_path / "live_chart_draw.db")
    db.connect()
    window = MainWindow(app_config, db)
    qtbot.addWidget(window)

    t0 = time.time()
    window._live_chart_history = LiveChartHistory(duration_s=100.0, bin_count=100)
    window.monitoring_panel.live_chart.clear()
    for i in range(100):  # 1 amostra/s, timestamp absoluto como no app real
        window._on_sample(
            Sample(timestamp=t0 + i, step_index=0, voltage=5.0, current=0.5)
        )

    # A série de tensão do gráfico tem que ter MUITOS pontos, não 1.
    assert window.monitoring_panel.live_chart._voltage_series.count() > 50
    db.close()


def test_live_chart_history_keeps_full_duration_visible_in_long_tests(
    qtbot, app_config, tmp_path: Path
) -> None:
    """Regressão: o gráfico ao vivo usava um FIFO de amostras brutas com
    tamanho máximo -- em ensaios mais longos que essa janela, as amostras
    mais antigas eram descartadas e a curva "apagava" o início, mesmo com
    o eixo X ainda mostrando a duração total configurada. O histórico por
    bins (core/live_chart_history.py) tem memória limitada pelo NÚMERO DE
    BINS, não pela quantidade de amostras -- a curva inteira (do início ao
    fim) precisa continuar visível não importa há quanto tempo o ensaio
    esteja rodando."""
    import time

    from core.live_chart_history import LiveChartHistory
    from core.sampling_buffer import Sample
    from gui.main_window import MainWindow

    db = Database(tmp_path / "live_chart_history.db")
    db.connect()
    window = MainWindow(app_config, db)
    qtbot.addWidget(window)

    # Duração total pequena (10 bins) simulando um ensaio "longo" com muito
    # mais amostras do que bins -- exatamente o cenário do bug relatado.
    # Timestamp ABSOLUTO (base epoch), como o app real produz.
    t0 = time.time()
    window._live_chart_history = LiveChartHistory(duration_s=10.0, bin_count=10)
    for i in range(1000):
        window._on_sample(
            Sample(timestamp=t0 + i * 0.01, step_index=0, voltage=5.0, current=0.5)
        )

    snapshot = window._live_chart_history.snapshot()
    assert len(snapshot) == 10  # memória limitada pelo Nº DE BINS, não pelas 1000 amostras
    # A amostra do PRIMEIRO instante do ensaio continua representada -- não
    # foi descartada como seria com um FIFO de tamanho fixo.
    assert snapshot[0].timestamp - t0 < 1.0
    db.close()


def test_live_chart_history_bounds_memory_regardless_of_sample_count() -> None:
    import time

    from core.live_chart_history import LiveChartHistory
    from core.sampling_buffer import Sample

    t0 = time.time()
    history = LiveChartHistory(duration_s=3600.0, bin_count=500)
    for i in range(100_000):
        history.add_sample(Sample(timestamp=t0 + i * 0.05, step_index=0, voltage=5.0, current=0.5))

    assert len(history.snapshot()) <= 500


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


def test_aux_serial_panel_simulate_mode_shows_incoming_data(qtbot) -> None:
    """Monitor serial/CAN opcional: em modo Simulação, conectar deve mostrar
    algo chegando na telinha sem precisar do conversor físico, e desconectar
    deve encerrar a thread de leitura de forma limpa (sem travar o teste)."""
    from gui.widgets.aux_serial_panel import AuxSerialPanel

    panel = AuxSerialPanel()
    qtbot.addWidget(panel)

    panel.simulate_check.setChecked(True)
    panel.connect_button.click()
    assert panel.is_connected() is True
    assert panel.connect_button.text() == "Desconectar"

    qtbot.waitUntil(lambda: bool(panel.output_edit.toPlainText().strip()), timeout=3000)
    assert "|" in panel.output_edit.toPlainText()  # moldura hex + ascii de _append_line

    panel.connect_button.click()  # desconectar
    qtbot.waitUntil(lambda: panel.is_connected() is False, timeout=3000)
    assert panel.connect_button.text() == "Conectar"
    assert panel.port_combo.isEnabled() is True


def test_aux_serial_panel_warns_when_no_port_selected(qtbot, monkeypatch) -> None:
    from PySide6 import QtWidgets

    from gui.widgets.aux_serial_panel import AuxSerialPanel

    panel = AuxSerialPanel()
    qtbot.addWidget(panel)
    panel.port_combo.clear()  # simula ambiente sem nenhuma porta COM disponível

    warned = []
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(1))
    )
    panel.connect_button.click()

    assert warned == [1]
    assert panel.is_connected() is False


def test_aux_serial_panel_clear_button_empties_output(qtbot) -> None:
    from gui.widgets.aux_serial_panel import AuxSerialPanel

    panel = AuxSerialPanel()
    qtbot.addWidget(panel)

    panel.output_edit.setPlainText("linha antiga")
    panel.clear_button.click()

    assert panel.output_edit.toPlainText() == ""


def test_main_window_close_event_shuts_down_aux_serial_panel(qtbot, app_config, tmp_path: Path) -> None:
    """closeEvent precisa parar a thread do monitor serial/CAN -- sem isso, a
    porta ficaria presa aberta (ou a thread travando o encerramento do app)."""
    from gui.main_window import MainWindow

    db = Database(tmp_path / "aux_serial_close.db")
    db.connect()
    window = MainWindow(app_config, db)
    qtbot.addWidget(window)

    panel = window.monitoring_panel.aux_serial_panel
    panel.simulate_check.setChecked(True)
    panel.connect_button.click()
    qtbot.waitUntil(lambda: panel.is_connected() is True, timeout=2000)
    worker = panel._worker

    window.close()

    # shutdown() chama wait(): quando close() retorna, a thread já terminou
    # de verdade (isRunning() não depende do sinal 'finished' ter sido
    # processado pela GUI ainda, ao contrário de is_connected()).
    assert worker.isRunning() is False
    db.close()
