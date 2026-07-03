"""Tela de avaliação manual pós-teste (seção 3.3).

O sistema nunca decide Aprovado/Reprovado automaticamente: esta tela só
mostra o que foi medido (resumo + min/max observados) e exige que o
operador escolha o resultado e, opcionalmente, registre um comentário.
O status técnico da sessão (COMPLETED/ABORTED/COMM_ERROR/FAULTED) já foi
fixado pela `main_window` no instante em que o worker terminou — reflete
o motivo de encerramento do state machine, não o veredito do operador.
Esta tela só grava a `Evaluation` (julgamento manual, campo separado) e
libera o state machine via `mark_evaluated()`, nunca antes da confirmação.
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from core.state_machine import TestState, TestStateMachine
from database.models import (
    Evaluation,
    EvaluationResult,
    MonitoredSample,
    Operator,
    TestSession,
)
from database.repositories import EvaluationRepository


class EvaluationView(QtWidgets.QWidget):
    evaluation_submitted = QtCore.Signal(dict)

    def __init__(
        self,
        evaluation_repo: EvaluationRepository,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._evaluation_repo = evaluation_repo

        self._session: TestSession | None = None
        self._operator: Operator | None = None
        self._state_machine: TestStateMachine | None = None

        title_label = QtWidgets.QLabel("Avaliação manual do ensaio")
        title_label.setObjectName("viewTitle")

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.addWidget(title_label)
        outer_layout.addWidget(scroll)

        container = QtWidgets.QWidget()
        scroll.setWidget(container)
        layout = QtWidgets.QVBoxLayout(container)

        summary_group = QtWidgets.QGroupBox("Resumo do teste executado")
        summary_form = QtWidgets.QFormLayout(summary_group)
        self.session_label = QtWidgets.QLabel("Nenhum teste carregado.")
        self.termination_label = QtWidgets.QLabel("—")
        self.sample_count_label = QtWidgets.QLabel("—")
        self.voltage_range_label = QtWidgets.QLabel("—")
        self.current_range_label = QtWidgets.QLabel("—")
        summary_form.addRow("Sessão:", self.session_label)
        summary_form.addRow("Encerramento do state machine:", self.termination_label)
        summary_form.addRow("Amostras coletadas:", self.sample_count_label)
        summary_form.addRow("Tensão observada (mín/máx):", self.voltage_range_label)
        summary_form.addRow("Corrente observada (mín/máx):", self.current_range_label)
        layout.addWidget(summary_group)

        decision_group = QtWidgets.QGroupBox("Avaliação manual (decisão exclusiva do operador)")
        decision_layout = QtWidgets.QVBoxLayout(decision_group)

        self.result_button_group = QtWidgets.QButtonGroup(self)
        self.approved_radio = QtWidgets.QRadioButton("Aprovado")
        self.rejected_radio = QtWidgets.QRadioButton("Reprovado")
        self.observation_radio = QtWidgets.QRadioButton("Observação")
        for radio in (self.approved_radio, self.rejected_radio, self.observation_radio):
            self.result_button_group.addButton(radio)
            decision_layout.addWidget(radio)

        decision_layout.addWidget(QtWidgets.QLabel("Comentário:"))
        self.comment_edit = QtWidgets.QTextEdit()
        self.comment_edit.setMaximumHeight(100)
        decision_layout.addWidget(self.comment_edit)

        self.submit_button = QtWidgets.QPushButton("Confirmar avaliação")
        self.submit_button.clicked.connect(self._on_submit)
        decision_layout.addWidget(self.submit_button)

        layout.addWidget(decision_group)
        layout.addStretch()

    def load_session(
        self,
        session: TestSession,
        operator: Operator,
        state_machine: TestStateMachine,
        samples: list[MonitoredSample],
    ) -> None:
        self._session = session
        self._operator = operator
        self._state_machine = state_machine

        self.session_label.setText(f"#{session.id} — S/N {session.serial_number}")
        termination = state_machine.termination_reason
        self.termination_label.setText(termination.value if termination else "—")
        self.sample_count_label.setText(str(len(samples)))

        if samples:
            voltages = [s.voltage_measured for s in samples]
            currents = [s.current_measured for s in samples]
            self.voltage_range_label.setText(f"{min(voltages):.3f} V / {max(voltages):.3f} V")
            self.current_range_label.setText(f"{min(currents):.3f} A / {max(currents):.3f} A")
        else:
            self.voltage_range_label.setText("—")
            self.current_range_label.setText("—")

        self.result_button_group.setExclusive(False)
        for radio in (self.approved_radio, self.rejected_radio, self.observation_radio):
            radio.setChecked(False)
        self.result_button_group.setExclusive(True)
        self.comment_edit.clear()

    def _selected_result(self) -> EvaluationResult | None:
        if self.approved_radio.isChecked():
            return EvaluationResult.APPROVED
        if self.rejected_radio.isChecked():
            return EvaluationResult.REJECTED
        if self.observation_radio.isChecked():
            return EvaluationResult.OBSERVATION
        return None

    def _on_submit(self) -> None:
        if self._session is None or self._operator is None or self._state_machine is None:
            QtWidgets.QMessageBox.warning(self, "Nenhum teste carregado", "Não há teste para avaliar.")
            return

        result = self._selected_result()
        if result is None:
            QtWidgets.QMessageBox.warning(
                self, "Resultado obrigatório", "Selecione Aprovado, Reprovado ou Observação."
            )
            return

        if self._state_machine.state != TestState.AWAITING_MANUAL_EVALUATION:
            QtWidgets.QMessageBox.warning(
                self,
                "Teste não está pronto para avaliação",
                f"Estado atual do teste: {self._state_machine.state.value}.",
            )
            return

        # Pergunta ANTES de gravar a avaliação/liberar o state machine --
        # "Cancelar" precisa realmente voltar para a tela de ensaio, sem
        # nenhuma escrita no banco, não só pular a geração do relatório.
        choice = QtWidgets.QMessageBox.question(
            self,
            "Salvar relatório do ensaio?",
            "Deseja salvar os dados deste ensaio (gerar relatório em Word/Excel/PDF)?\n\n"
            "Sim: escolher onde salvar\n"
            "Não: não salvar e encerrar o ensaio\n"
            "Cancelar: voltar para a tela de ensaio",
            QtWidgets.QMessageBox.StandardButton.Yes
            | QtWidgets.QMessageBox.StandardButton.No
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if choice == QtWidgets.QMessageBox.StandardButton.Cancel:
            return
        save_report = choice == QtWidgets.QMessageBox.StandardButton.Yes

        evaluation = self._evaluation_repo.create(
            Evaluation(
                id=None,
                test_session_id=self._session.id,
                operator_id=self._operator.id,
                result=result,
                comment=self.comment_edit.toPlainText().strip() or None,
            )
        )

        self._state_machine.mark_evaluated()

        self.evaluation_submitted.emit(
            {"evaluation": evaluation, "session": self._session, "save_report": save_report}
        )
