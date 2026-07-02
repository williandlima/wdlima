"""Testes 'gui' (offscreen) do botão "Excluir" em operadores (Cadastro) e
configurações salvas (Parâmetros) -- resposta à dúvida do usuário sobre como
limpar operadores/testes carregados: não existia essa opção pela GUI.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6")

pytestmark = pytest.mark.gui

from PySide6 import QtWidgets

from database.database import Database
from database.models import TestSession, TestSessionStatus
from database.repositories import (
    BoardRepository,
    OperatorRepository,
    TestParameterConfigRepository,
    TestSessionRepository,
)
from database.models import TestParameterConfig
from gui.registration_view import RegistrationView
from gui.test_parameters_view import TestParametersView


def _confirm_yes():
    return patch.object(
        QtWidgets.QMessageBox, "question",
        return_value=QtWidgets.QMessageBox.StandardButton.Yes,
    )


def _enter_password(password: str, confirmed: bool = True):
    """Mocka o prompt de senha (RegistrationView._on_delete_operator) --
    sem isso o QInputDialog.getText() real trava esperando input do usuário
    e o teste nunca termina."""
    return patch.object(
        QtWidgets.QInputDialog, "getText", return_value=(password, confirmed)
    )


# -- Operador (Cadastro) ------------------------------------------------------

_PASSWORD = "lab1"  # default de RegistrationView (config security.operator_delete_password)


def test_delete_operator_removes_unused_entry_from_combo(qtbot, tmp_path: Path) -> None:
    db = Database(tmp_path / "del_operator.db")
    db.connect()
    operator_repo = OperatorRepository(db)
    operator_repo.get_or_create("Duplicado")
    view = RegistrationView(operator_repo, BoardRepository(db), _PASSWORD)
    qtbot.addWidget(view)
    assert view.operator_combo.count() == 1

    view.operator_combo.setCurrentIndex(0)
    with _enter_password(_PASSWORD):
        view.delete_operator_button.click()

    assert view.operator_combo.count() == 0
    assert operator_repo.list_all() == []
    db.close()


def test_delete_operator_resolves_by_typed_text_not_stale_current_index(
    qtbot, tmp_path: Path
) -> None:
    """Reproduz o bug relatado em campo: currentIndex() pode ficar -1/
    desatualizado num combo editável mesmo com um nome válido digitado no
    campo (ex.: depois de clear_form()) -- "Excluir" tinha ficado mudo
    (achava que nada estava selecionado) porque resolvia pelo índice em
    vez do texto exibido."""
    db = Database(tmp_path / "del_operator_by_text.db")
    db.connect()
    operator_repo = OperatorRepository(db)
    operator_repo.get_or_create("Digitado")
    view = RegistrationView(operator_repo, BoardRepository(db), _PASSWORD)
    qtbot.addWidget(view)

    view.clear_form()  # currentIndex() vira -1, como no fluxo real
    view.operator_combo.setEditText("Digitado")  # simula o operador digitando

    with _enter_password(_PASSWORD):
        view.delete_operator_button.click()

    assert operator_repo.list_all() == []
    db.close()


def test_delete_operator_with_nothing_selected_warns_and_does_not_crash(
    qtbot, tmp_path: Path
) -> None:
    db = Database(tmp_path / "del_operator_none.db")
    db.connect()
    view = RegistrationView(OperatorRepository(db), BoardRepository(db))
    qtbot.addWidget(view)
    view.operator_combo.setCurrentIndex(-1)

    with patch.object(QtWidgets.QMessageBox, "warning") as mock_warning:
        view.delete_operator_button.click()

    mock_warning.assert_called_once()
    db.close()


def test_delete_operator_with_wrong_password_is_not_deleted(qtbot, tmp_path: Path) -> None:
    db = Database(tmp_path / "del_operator_wrong_pw.db")
    db.connect()
    operator_repo = OperatorRepository(db)
    operator_repo.get_or_create("Protegido")
    view = RegistrationView(operator_repo, BoardRepository(db), _PASSWORD)
    qtbot.addWidget(view)
    view.operator_combo.setCurrentIndex(0)

    with _enter_password("senha-errada"), patch.object(QtWidgets.QMessageBox, "warning") as mock_warning:
        view.delete_operator_button.click()

    mock_warning.assert_called_once()
    assert operator_repo.list_all() != []  # não foi excluído
    db.close()


def test_delete_operator_cancelled_password_prompt_is_not_deleted(qtbot, tmp_path: Path) -> None:
    db = Database(tmp_path / "del_operator_cancel.db")
    db.connect()
    operator_repo = OperatorRepository(db)
    operator_repo.get_or_create("Protegido")
    view = RegistrationView(operator_repo, BoardRepository(db), _PASSWORD)
    qtbot.addWidget(view)
    view.operator_combo.setCurrentIndex(0)

    with _enter_password("", confirmed=False):
        view.delete_operator_button.click()

    assert operator_repo.list_all() != []
    db.close()


def test_delete_operator_blocked_when_used_by_a_test_session(qtbot, tmp_path: Path) -> None:
    db = Database(tmp_path / "del_operator_blocked.db")
    db.connect()
    operator_repo = OperatorRepository(db)
    board_repo = BoardRepository(db)
    operator = operator_repo.get_or_create("Com ensaio")
    board = board_repo.get_or_create("PCB-1", "PN-1", "A")
    TestSessionRepository(db).create(
        TestSession(
            id=None, board_id=board.id, serial_number="SN-1", operator_id=operator.id,
            test_parameter_config_id=None, config_snapshot_json="{}", production_order=None,
            observations=None, status=TestSessionStatus.COMPLETED,
        )
    )
    view = RegistrationView(operator_repo, board_repo, _PASSWORD)
    qtbot.addWidget(view)
    view.operator_combo.setCurrentIndex(0)

    with _enter_password(_PASSWORD), patch.object(QtWidgets.QMessageBox, "warning") as mock_warning:
        view.delete_operator_button.click()

    mock_warning.assert_called_once()
    assert view.operator_combo.count() == 1  # não foi removido
    db.close()


def test_operator_combo_uses_popup_completion_not_silent_inline_completion(
    qtbot, tmp_path: Path
) -> None:
    """Qt liga InlineCompletion por padrão em combos editáveis -- completa o
    texto digitado SILENCIOSAMENTE com o candidato mais próximo do histórico.
    Com nomes parecidos no histórico (ex.: "Willian" e "Willian - 0132"), isso
    pode preencher o campo com o nome ERRADO antes de excluir, sem o operador
    perceber. PopupCompletion nunca altera o campo sozinho."""
    db = Database(tmp_path / "completer_mode.db")
    db.connect()
    view = RegistrationView(OperatorRepository(db), BoardRepository(db))
    qtbot.addWidget(view)

    completer = view.operator_combo.completer()
    assert completer is not None
    assert completer.completionMode() == QtWidgets.QCompleter.CompletionMode.PopupCompletion
    db.close()


def test_delete_operator_with_similar_named_entries_only_deletes_the_exact_match(
    qtbot, tmp_path: Path
) -> None:
    """Reproduz o cenário relatado em campo: "Willian" e "Willian - 0132" são
    registros DIFERENTES no histórico (ex.: um digitado só com o nome, outro
    combinando nome+IF por engano no campo errado). Excluir "Willian" não
    pode apagar/afetar "Willian - 0132", e vice-versa."""
    db = Database(tmp_path / "similar_names.db")
    db.connect()
    operator_repo = OperatorRepository(db)
    operator_repo.get_or_create("Willian")
    operator_repo.get_or_create("Willian - 0132")
    view = RegistrationView(operator_repo, BoardRepository(db), _PASSWORD)
    qtbot.addWidget(view)

    view.operator_combo.setEditText("Willian")  # texto EXATO, sem o sufixo
    assert view._selected_operator().name == "Willian"  # resolve o registro certo

    with _enter_password(_PASSWORD):
        view.delete_operator_button.click()

    remaining = [o.name for o in operator_repo.list_all()]
    assert remaining == ["Willian - 0132"]  # só o digitado sumiu
    db.close()


def test_delete_operator_password_prompt_shows_if_number_to_disambiguate(
    qtbot, tmp_path: Path
) -> None:
    """A mensagem do prompt de senha mostra nome + IF (não só o nome) --
    dois registros parecidos ("Willian" x "Willian - 0132") viram
    inequívocos quando o IF aparece junto, antes de confirmar a exclusão."""
    db = Database(tmp_path / "prompt_identity.db")
    db.connect()
    operator_repo = OperatorRepository(db)
    operator_repo.get_or_create("Willian", if_number="0132")
    view = RegistrationView(operator_repo, BoardRepository(db), _PASSWORD)
    qtbot.addWidget(view)
    view.operator_combo.setCurrentIndex(0)

    with patch.object(
        QtWidgets.QInputDialog, "getText", return_value=(_PASSWORD, True)
    ) as mock_get_text:
        view.delete_operator_button.click()

    prompt_text = mock_get_text.call_args.args[2]
    assert "Willian" in prompt_text
    assert "0132" in prompt_text
    db.close()


# -- Configuração salva (Parâmetros) ------------------------------------------


def test_delete_history_config_removes_unused_entry_from_combo(qtbot, tmp_path: Path) -> None:
    db = Database(tmp_path / "del_config.db")
    db.connect()
    board_repo = BoardRepository(db)
    config_repo = TestParameterConfigRepository(db)
    board = board_repo.get_or_create("PCB-1", "PN-1", "A")
    config_repo.save(
        TestParameterConfig(
            id=None, board_id=board.id, name="Preset por engano", nominal_voltage=5.0,
            voltage_min=4.5, voltage_max=5.5, current_max=1.0, test_duration_s=60.0,
            power_sequence=[],
        )
    )
    view = TestParametersView(config_repo, {
        "polling_rate_hz": 1.0, "stabilization_timeout_s": 5.0,
        "stabilization_tolerance_v": 0.05, "monitoring_consecutive_failures_limit": 3,
    })
    qtbot.addWidget(view)
    view.set_board(board)
    assert view.history_combo.count() == 1

    view.history_combo.setCurrentIndex(0)
    with _confirm_yes():
        view.delete_history_button.click()

    assert view.history_combo.count() == 0
    assert config_repo.list_for_board(board.id) == []
    db.close()


def test_delete_history_config_blocked_when_used_by_a_test_session(qtbot, tmp_path: Path) -> None:
    db = Database(tmp_path / "del_config_blocked.db")
    db.connect()
    board_repo = BoardRepository(db)
    operator_repo = OperatorRepository(db)
    config_repo = TestParameterConfigRepository(db)
    board = board_repo.get_or_create("PCB-1", "PN-1", "A")
    operator = operator_repo.get_or_create("Op")
    config = config_repo.save(
        TestParameterConfig(
            id=None, board_id=board.id, name="Usado de verdade", nominal_voltage=5.0,
            voltage_min=4.5, voltage_max=5.5, current_max=1.0, test_duration_s=60.0,
            power_sequence=[],
        )
    )
    TestSessionRepository(db).create(
        TestSession(
            id=None, board_id=board.id, serial_number="SN-1", operator_id=operator.id,
            test_parameter_config_id=config.id, config_snapshot_json="{}",
            production_order=None, observations=None, status=TestSessionStatus.COMPLETED,
        )
    )
    view = TestParametersView(config_repo, {
        "polling_rate_hz": 1.0, "stabilization_timeout_s": 5.0,
        "stabilization_tolerance_v": 0.05, "monitoring_consecutive_failures_limit": 3,
    })
    qtbot.addWidget(view)
    view.set_board(board)
    view.history_combo.setCurrentIndex(0)

    with _confirm_yes(), patch.object(QtWidgets.QMessageBox, "warning") as mock_warning:
        view.delete_history_button.click()

    mock_warning.assert_called_once()
    assert view.history_combo.count() == 1  # não foi removido
    db.close()
