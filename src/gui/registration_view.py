"""Tela de cadastro de teste (seção 3.2).

Coleta os dados de identificação da placa/operador e cria (ou reaproveita)
os registros de Board/Operator/TestSession no banco. Não conhece o
state machine nem a fonte — só persistência e validação de formulário.
"""
from __future__ import annotations

import datetime as dt

from PySide6 import QtCore, QtWidgets

from database.models import Operator
from database.repositories import BoardRepository, OperatorRepository, RecordInUseError
from version import APP_VERSION


class RegistrationView(QtWidgets.QWidget):
    registration_submitted = QtCore.Signal(dict)

    def __init__(
        self,
        operator_repo: OperatorRepository,
        board_repo: BoardRepository,
        operator_delete_password: str = "lab1",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._operator_repo = operator_repo
        self._board_repo = board_repo
        self._operator_delete_password = operator_delete_password

        title_label = QtWidgets.QLabel("Cadastro do ensaio")
        title_label.setObjectName("viewTitle")
        version_label = QtWidgets.QLabel(f"Versão {APP_VERSION}")
        version_label.setProperty("caption", "true")
        version_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        title_row = QtWidgets.QHBoxLayout()
        title_row.addWidget(title_label)
        title_row.addStretch()
        title_row.addWidget(version_label)

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.addLayout(title_row)
        outer_layout.addWidget(scroll)

        form_container = QtWidgets.QWidget()
        scroll.setWidget(form_container)

        operator_group = QtWidgets.QGroupBox("Cadastro do Operador")
        operator_form = QtWidgets.QFormLayout(operator_group)

        self.operator_combo = QtWidgets.QComboBox()
        self.operator_combo.setEditable(True)
        self.operator_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        # Qt liga um QCompleter em modo InlineCompletion por padrão em combos
        # editáveis: ele completa o texto digitado SILENCIOSAMENTE com o
        # candidato mais próximo (ex.: digitar "Willian" quando também existe
        # "Willian - 0132" no histórico pode preencher o campo com o nome
        # errado, com o trecho extra só destacado/selecionado -- fácil de não
        # perceber). Isso é especialmente perigoso pra "Excluir": o operador
        # acha que está prestes a apagar "Willian" mas na verdade seria
        # "Willian - 0132" (ou vice-versa). PopupCompletion mostra uma lista
        # em vez de alterar o campo sozinho -- só muda o texto se o operador
        # clicar numa sugestão explicitamente.
        completer = self.operator_combo.completer()
        if completer is not None:
            completer.setCompletionMode(QtWidgets.QCompleter.CompletionMode.PopupCompletion)
        self.operator_combo.currentTextChanged.connect(self._on_operator_changed)
        # Campo confortável para digitar o nome (era estreito demais).
        self.operator_combo.setMinimumWidth(360)
        self.operator_combo.setMinimumHeight(30)
        self.operator_combo.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed
        )
        self.if_edit = QtWidgets.QLineEdit()
        self.if_edit.setMinimumHeight(30)

        operator_row = QtWidgets.QWidget()
        operator_row_layout = QtWidgets.QHBoxLayout(operator_row)
        operator_row_layout.setContentsMargins(0, 0, 0, 0)
        operator_row_layout.addWidget(self.operator_combo, stretch=1)
        self.delete_operator_button = QtWidgets.QPushButton("Excluir")
        self.delete_operator_button.setToolTip(
            "Remove este operador do histórico -- só funciona se ele nunca "
            "tiver sido usado em um ensaio registrado."
        )
        self.delete_operator_button.clicked.connect(self._on_delete_operator)
        operator_row_layout.addWidget(self.delete_operator_button)

        operator_form.addRow("Operador:", operator_row)
        operator_form.addRow("IF:", self.if_edit)

        group = QtWidgets.QGroupBox("Identificação da placa e do teste")
        form = QtWidgets.QFormLayout(group)

        self.code_edit = QtWidgets.QLineEdit()
        self.part_number_edit = QtWidgets.QLineEdit()
        self.revision_edit = QtWidgets.QLineEdit()
        self.serial_number_edit = QtWidgets.QLineEdit()
        self.production_order_edit = QtWidgets.QLineEdit()
        self.observations_edit = QtWidgets.QTextEdit()
        self.observations_edit.setMaximumHeight(80)
        self.datetime_label = QtWidgets.QLabel()

        form.addRow("Código da placa:", self.code_edit)
        form.addRow("Part Number (P/N):", self.part_number_edit)
        form.addRow("Revisão:", self.revision_edit)
        form.addRow("Número de série (S/N):", self.serial_number_edit)
        form.addRow("Ordem de produção:", self.production_order_edit)
        form.addRow("Observações:", self.observations_edit)
        form.addRow("Data/hora:", self.datetime_label)

        self.submit_button = QtWidgets.QPushButton("Iniciar cadastro do teste")
        self.submit_button.clicked.connect(self._on_submit)

        form_layout = QtWidgets.QVBoxLayout(form_container)
        form_layout.addWidget(operator_group)
        form_layout.addWidget(group)
        form_layout.addWidget(self.submit_button)
        form_layout.addStretch()

        self._clock_timer = QtCore.QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

        self.refresh_operator_history()
        # addItem() no combo de operadores seleciona o índice 0 automaticamente
        # (histórico do último operador), deixando "Operador"/"IF" pré-preenchidos
        # já na abertura do app. clear_form() zera a seleção sem mexer na lista.
        self.clear_form()

    def refresh_operator_history(self) -> None:
        self.operator_combo.clear()
        for operator in self._operator_repo.list_all():
            self.operator_combo.addItem(operator.name, userData=operator)

    def _selected_operator(self) -> Operator | None:
        """Resolve o operador atualmente escolhido no combo pelo TEXTO
        exibido, não por currentIndex() -- o combo é editável
        (setEditable(True)), e currentIndex() fica desatualizado/-1 depois
        de digitação ou de clear_form(), mesmo com um nome válido ainda
        visível no campo. Usar currentIndex() diretamente (bug corrigido
        aqui) fazia "Excluir" achar que nada estava selecionado e não
        fazer nada, silenciosamente."""
        index = self.operator_combo.findText(self.operator_combo.currentText())
        return self.operator_combo.itemData(index) if index >= 0 else None

    def _on_delete_operator(self) -> None:
        """Remove um cadastro duplicado/errado -- ver "duvida" do usuário
        sobre limpar operadores/testes carregados: não existia forma de
        fazer isso pela GUI, só editando o banco direto. Bloqueado pelo
        próprio banco (RecordInUseError) se o operador já tiver ensaios
        registrados -- nunca apaga histórico de teste de verdade.

        Pede a senha do laboratório (config `security.operator_delete_password`)
        antes de excluir -- trava simples contra clique acidental, não
        autenticação de verdade; a proteção real contra perda de dados já é
        o bloqueio de FK do banco."""
        operator = self._selected_operator()
        if operator is None:
            QtWidgets.QMessageBox.warning(
                self, "Nenhum operador selecionado",
                "Escolha um operador já cadastrado no campo acima para excluir.",
            )
            return
        # Mostra nome + IF juntos (não só o nome) -- desambigua registros
        # parecidos no histórico (ex.: "Willian" x "Willian - 0132" cadastrados
        # em momentos diferentes), pra quem for excluir enxergar exatamente
        # qual registro vai sumir antes de confirmar com a senha.
        identity = operator.name
        if operator.if_number:
            identity = f"{operator.name} (IF: {operator.if_number})"
        password, confirmed = QtWidgets.QInputDialog.getText(
            self,
            "Senha necessária",
            f'Digite a senha para excluir o operador "{identity}":',
            QtWidgets.QLineEdit.EchoMode.Password,
        )
        if not confirmed:
            return
        if password != self._operator_delete_password:
            QtWidgets.QMessageBox.warning(
                self, "Senha incorreta", "Senha incorreta -- operador não foi excluído."
            )
            return
        try:
            self._operator_repo.delete(operator.id)
        except RecordInUseError as exc:
            QtWidgets.QMessageBox.warning(self, "Não é possível excluir", str(exc))
            return
        self.refresh_operator_history()
        self.clear_form()

    def clear_form(self) -> None:
        """Deixa a Tela 1 em branco para o próximo operador (fim do ensaio)."""
        self.operator_combo.setCurrentIndex(-1)
        self.operator_combo.clearEditText()
        self.if_edit.clear()
        self.code_edit.clear()
        self.part_number_edit.clear()
        self.revision_edit.clear()
        self.serial_number_edit.clear()
        self.production_order_edit.clear()
        self.observations_edit.clear()

    def _on_operator_changed(self, name: str) -> None:
        index = self.operator_combo.findText(name)
        operator: Operator | None = self.operator_combo.itemData(index) if index >= 0 else None
        self.if_edit.setText(operator.if_number or "" if operator else "")

    def _update_clock(self) -> None:
        self.datetime_label.setText(dt.datetime.now().strftime("%d/%m/%Y %H:%M:%S"))

    def _on_submit(self) -> None:
        code = self.code_edit.text().strip()
        part_number = self.part_number_edit.text().strip()
        revision = self.revision_edit.text().strip()
        serial_number = self.serial_number_edit.text().strip()
        operator_name = self.operator_combo.currentText().strip()
        if_number = self.if_edit.text().strip()

        missing = [
            label
            for label, value in (
                ("Código da placa", code),
                ("P/N", part_number),
                ("Revisão", revision),
                ("S/N", serial_number),
                ("Operador", operator_name),
                ("IF", if_number),
            )
            if not value
        ]
        if missing:
            QtWidgets.QMessageBox.warning(
                self, "Campos obrigatórios", "Preencha: " + ", ".join(missing)
            )
            return

        operator: Operator = self._operator_repo.get_or_create(operator_name, if_number)
        board = self._board_repo.get_or_create(code, part_number, revision)
        self.refresh_operator_history()

        self.registration_submitted.emit(
            {
                "board": board,
                "operator": operator,
                "serial_number": serial_number,
                "production_order": self.production_order_edit.text().strip() or None,
                "observations": self.observations_edit.toPlainText().strip() or None,
            }
        )
