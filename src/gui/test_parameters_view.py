"""Tela de parâmetros de teste (seção 3.2).

Define tensão nominal, limites de tensão (referência visual, nunca gatilho
automático — seção 3.3), corrente máxima, duração e a sequência multi-step
opcional de potência. Persiste como `TestParameterConfig` associado à placa
e traduz o config persistido em um `TestRunConfig` consumível pelo state
machine. Não conhece o instrumento nem o state machine além desse DTO.
"""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from config import VoltageRange
from core.state_machine import TestRunConfig
from database.models import Board, PowerStep, TestParameterConfig
from database.repositories import RecordInUseError, TestParameterConfigRepository
from gui.widgets.range_feedback import (
    RangeFitState,
    apply_spin_feedback,
    apply_table_item_feedback,
    build_range_combo,
    build_range_warning_label,
    evaluate_range_fit,
    evaluate_test_limit_fit,
)


class TestParametersView(QtWidgets.QWidget):
    parameters_submitted = QtCore.Signal(dict)
    back_requested = QtCore.Signal()

    _COLUMN_LABELS = ("Tensão (V)", "Corrente máx. (A)", "Duração (s)", "Tempo OFF (s)")

    def __init__(
        self,
        config_repo: TestParameterConfigRepository,
        test_defaults: dict,
        ranges: tuple[VoltageRange, ...] = (),
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config_repo = config_repo
        self._test_defaults = test_defaults
        self._ranges = ranges
        self._board: Board | None = None

        title_label = QtWidgets.QLabel("Parâmetros do ensaio")
        title_label.setObjectName("viewTitle")

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer_layout = QtWidgets.QVBoxLayout(self)
        outer_layout.addWidget(title_label)
        outer_layout.addWidget(scroll)

        form_container = QtWidgets.QWidget()
        scroll.setWidget(form_container)
        form_layout = QtWidgets.QVBoxLayout(form_container)

        self.board_label = QtWidgets.QLabel("Nenhuma placa selecionada.")
        form_layout.addWidget(self.board_label)

        history_group = QtWidgets.QGroupBox("Configurações salvas para esta placa")
        history_layout = QtWidgets.QHBoxLayout(history_group)
        self.history_combo = QtWidgets.QComboBox()
        self.load_button = QtWidgets.QPushButton("Carregar")
        self.load_button.clicked.connect(self._on_load_history)
        self.delete_history_button = QtWidgets.QPushButton("Excluir")
        self.delete_history_button.setToolTip(
            "Remove esta configuração do histórico -- só funciona se ela nunca "
            "tiver sido usada em um ensaio registrado."
        )
        self.delete_history_button.clicked.connect(self._on_delete_history)
        history_layout.addWidget(self.history_combo, stretch=1)
        history_layout.addWidget(self.load_button)
        history_layout.addWidget(self.delete_history_button)
        form_layout.addWidget(history_group)

        limits_group = QtWidgets.QGroupBox("Tensão, corrente e duração")
        limits_form = QtWidgets.QFormLayout(limits_group)

        self.config_name_edit = QtWidgets.QLineEdit()
        self.nominal_voltage_spin = self._make_double_spin(0.0, 50.0, 2, " V")
        self.voltage_min_spin = self._make_double_spin(0.0, 50.0, 2, " V")
        self.voltage_max_spin = self._make_double_spin(0.0, 50.0, 2, " V")
        self.current_max_spin = self._make_double_spin(0.0, 20.0, 3, " A")

        # Duração em segundos/minutos/horas (internamente sempre convertida p/ s).
        # 3 casas: preserva precisão ao alternar a unidade (ex.: 2 min = 0,033 h).
        self.test_duration_spin = self._make_double_spin(0.0, 100000.0, 3, "")
        self.duration_unit_combo = QtWidgets.QComboBox()
        for label, factor in (("segundos", 1.0), ("minutos", 60.0), ("horas", 3600.0)):
            self.duration_unit_combo.addItem(label, userData=factor)
        self.duration_unit_combo.setCurrentIndex(1)  # minutos por padrão
        self._duration_factor = 60.0
        self.test_duration_spin.setValue(1.0)  # 1 min = 60 s (default equivalente ao antigo)
        self.duration_unit_combo.currentIndexChanged.connect(self._on_duration_unit_changed)

        duration_row = QtWidgets.QWidget()
        duration_row_layout = QtWidgets.QHBoxLayout(duration_row)
        duration_row_layout.setContentsMargins(0, 0, 0, 0)
        duration_row_layout.addWidget(self.test_duration_spin, stretch=1)
        duration_row_layout.addWidget(self.duration_unit_combo)

        # Faixa da fonte: "Automática" (padrão/recomendado) deixa o driver
        # escolher a faixa mais "justa" a cada passo (ver
        # PowerSupplyE363x._ensure_range); escolher uma faixa nomeada trava
        # o ensaio INTEIRO nela — se um passo não couber, falha com erro
        # acionável em vez de trocar de faixa sozinho.
        self.range_combo = build_range_combo(self._ranges)

        limits_form.addRow("Nome da configuração:", self.config_name_edit)
        limits_form.addRow("Tensão nominal:", self.nominal_voltage_spin)
        limits_form.addRow("Tensão mínima (referência):", self.voltage_min_spin)
        limits_form.addRow("Tensão máxima (referência):", self.voltage_max_spin)
        limits_form.addRow("Corrente máxima:", self.current_max_spin)
        limits_form.addRow("Faixa da fonte:", self.range_combo)
        limits_form.addRow("Duração do teste (passo único):", duration_row)
        form_layout.addWidget(limits_group)

        self.range_warning_label = build_range_warning_label()
        form_layout.addWidget(self.range_warning_label)

        self.nominal_voltage_spin.valueChanged.connect(self._update_single_step_range_feedback)
        self.current_max_spin.valueChanged.connect(self._update_single_step_range_feedback)
        self.range_combo.currentIndexChanged.connect(self._update_single_step_range_feedback)
        self.range_combo.currentIndexChanged.connect(self._update_all_row_range_feedback)
        # Cada passo do ciclo é amarrado a estes 3 campos -- mudar qualquer um
        # precisa reavaliar as linhas já digitadas na sequência, senão a cor
        # da célula fica desatualizada em relação ao limite atual.
        self.voltage_min_spin.valueChanged.connect(self._update_all_row_range_feedback)
        self.voltage_max_spin.valueChanged.connect(self._update_all_row_range_feedback)
        self.current_max_spin.valueChanged.connect(self._update_all_row_range_feedback)

        advanced_group = QtWidgets.QGroupBox("Parâmetros avançados de monitoramento")
        advanced_form = QtWidgets.QFormLayout(advanced_group)
        self.polling_rate_spin = self._make_double_spin(0.1, 10.0, 2, " Hz")
        self.polling_rate_spin.setValue(self._test_defaults["polling_rate_hz"])
        self.stabilization_timeout_spin = self._make_double_spin(0.5, 300.0, 1, " s")
        self.stabilization_timeout_spin.setValue(self._test_defaults["stabilization_timeout_s"])
        self.stabilization_tolerance_spin = self._make_double_spin(0.0, 5.0, 3, " V")
        self.stabilization_tolerance_spin.setValue(self._test_defaults["stabilization_tolerance_v"])
        self.consecutive_failures_spin = QtWidgets.QSpinBox()
        self.consecutive_failures_spin.setRange(1, 100)
        self.consecutive_failures_spin.setValue(self._test_defaults["monitoring_consecutive_failures_limit"])
        # Intervalo de captura (gravação no relatório), distinto da taxa de
        # monitoramento ao vivo — evita overdata em ensaios longos. 0 = grava todas.
        self.capture_interval_spin = self._make_double_spin(0.0, 3600.0, 2, " s")
        self.capture_interval_spin.setValue(self._test_defaults.get("capture_interval_s", 1.0))
        # Nível de disparo de OVP/OCP definido diretamente pelo operador.
        # 0 = não configura proteção (a fonte mantém seu próprio default e
        # ela não atua durante o ensaio).
        self.ovp_level_spin = self._make_double_spin(0.0, 60.0, 2, " V")
        self.ovp_level_spin.setValue(self._test_defaults.get("ovp_level_v", 0.0))
        self.ocp_level_spin = self._make_double_spin(0.0, 20.0, 3, " A")
        self.ocp_level_spin.setValue(self._test_defaults.get("ocp_level_a", 0.0))

        advanced_form.addRow("Taxa de amostragem (display):", self.polling_rate_spin)
        advanced_form.addRow("Intervalo de captura (gravação):", self.capture_interval_spin)
        advanced_form.addRow("Timeout de estabilização:", self.stabilization_timeout_spin)
        advanced_form.addRow("Tolerância de estabilização:", self.stabilization_tolerance_spin)
        advanced_form.addRow("Falhas consecutivas até erro de comunicação:", self.consecutive_failures_spin)
        advanced_form.addRow("OVP — nível de disparo (0 = desativado):", self.ovp_level_spin)
        advanced_form.addRow("OCP — nível de disparo (0 = desativado):", self.ocp_level_spin)
        form_layout.addWidget(advanced_group)

        sequence_group = QtWidgets.QGroupBox(
            "Sequência multi-step de potência (opcional — vazio usa o passo único acima)"
        )
        sequence_layout = QtWidgets.QVBoxLayout(sequence_group)
        sequence_hint = QtWidgets.QLabel(
            '"Tempo OFF": opcional, saída DESLIGADA após o passo por esse tempo antes '
            "do próximo (ex.: ciclo térmico ON/OFF). 0 = segue direto pro próximo passo."
        )
        sequence_hint.setWordWrap(True)
        sequence_layout.addWidget(sequence_hint)
        self.sequence_table = QtWidgets.QTableWidget(0, len(self._COLUMN_LABELS))
        self.sequence_table.setHorizontalHeaderLabels(self._COLUMN_LABELS)
        self.sequence_table.horizontalHeader().setStretchLastSection(True)
        self.sequence_table.itemChanged.connect(self._on_sequence_item_changed)
        sequence_layout.addWidget(self.sequence_table)

        sequence_buttons = QtWidgets.QHBoxLayout()
        self.add_step_button = QtWidgets.QPushButton("Adicionar passo")
        self.add_step_button.clicked.connect(self._on_add_step)
        self.remove_step_button = QtWidgets.QPushButton("Remover passo selecionado")
        self.remove_step_button.clicked.connect(self._on_remove_step)
        sequence_buttons.addWidget(self.add_step_button)
        sequence_buttons.addWidget(self.remove_step_button)
        sequence_buttons.addStretch()
        sequence_layout.addLayout(sequence_buttons)
        form_layout.addWidget(sequence_group)

        actions_row = QtWidgets.QHBoxLayout()
        self.back_button = QtWidgets.QPushButton("Voltar")
        self.back_button.clicked.connect(self.back_requested.emit)
        self.submit_button = QtWidgets.QPushButton("Salvar e continuar")
        self.submit_button.clicked.connect(self._on_submit)
        actions_row.addWidget(self.back_button)
        actions_row.addStretch()
        actions_row.addWidget(self.submit_button)
        form_layout.addLayout(actions_row)
        form_layout.addStretch()

        self._refresh_duration_unit()
        self._update_single_step_range_feedback()

    # -- unidade de tempo (s/min/h) -----------------------------------------

    def _unit_suffix(self) -> str:
        return {1.0: "s", 60.0: "min", 3600.0: "h"}[self._duration_factor]

    def _refresh_duration_unit(self) -> None:
        """Atualiza sufixo do spin e os cabeçalhos de Duração/Tempo OFF da tabela."""
        suffix = self._unit_suffix()
        self.test_duration_spin.setSuffix(f" {suffix}")
        headers = list(self._COLUMN_LABELS[:2]) + [f"Duração ({suffix})", f"Tempo OFF ({suffix})"]
        self.sequence_table.setHorizontalHeaderLabels(headers)

    def _on_duration_unit_changed(self) -> None:
        """Converte os valores exibidos para a nova unidade, mantendo os segundos."""
        new_factor = self.duration_unit_combo.currentData()
        ratio = self._duration_factor / new_factor
        self.test_duration_spin.setValue(self.test_duration_spin.value() * ratio)
        for row in range(self.sequence_table.rowCount()):
            for column in (2, 3):  # Duração e Tempo OFF -- as duas na mesma unidade exibida
                item = self.sequence_table.item(row, column)
                if item is None:
                    continue
                try:
                    item.setText(str(round(float(item.text()) * ratio, 4)))
                except ValueError:
                    pass  # célula em edição/ inválida: deixa como está
        self._duration_factor = new_factor
        self._refresh_duration_unit()

    @staticmethod
    def _make_double_spin(minimum: float, maximum: float, decimals: int, suffix: str) -> QtWidgets.QDoubleSpinBox:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        return spin

    def set_board(self, board: Board) -> None:
        self._board = board
        self.board_label.setText(f"Placa: {board.code} (P/N {board.part_number}, rev. {board.revision})")
        # Sem isto, a sequência multi-step de um ensaio ANTERIOR (ex.: outra
        # placa, ou um teste já concluído) ficava na tabela e era aplicada
        # silenciosamente neste novo ensaio — mesmo o operador preenchendo
        # "Tensão nominal"/"Corrente máxima" com valores totalmente
        # diferentes, já que TestRunConfig.steps() prioriza power_sequence
        # quando não-vazio. A fonte real chegou a receber tensão/corrente
        # de um ensaio antigo por causa disso. "Carregar" (histórico) ainda
        # repopula a tabela explicitamente quando o operador pedir.
        self.sequence_table.setRowCount(0)
        self.refresh_history()

    def refresh_history(self) -> None:
        self.history_combo.clear()
        if self._board is None or self._board.id is None:
            return
        for config in self._config_repo.list_for_board(self._board.id):
            self.history_combo.addItem(config.name, userData=config)

    def _on_delete_history(self) -> None:
        """Remove uma configuração criada por engano -- ver "duvida" do
        usuário sobre limpar operadores/testes carregados: não existia
        forma de fazer isso pela GUI, só editando o banco direto. Bloqueado
        pelo próprio banco (RecordInUseError) se a configuração já tiver
        sido usada num ensaio registrado -- nunca apaga histórico de teste
        de verdade."""
        config: TestParameterConfig | None = self.history_combo.currentData()
        if config is None:
            QtWidgets.QMessageBox.warning(
                self, "Nenhuma configuração selecionada",
                "Escolha uma configuração salva no campo acima para excluir.",
            )
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Confirmar exclusão",
            f'Excluir a configuração "{config.name}" do histórico?\n\n'
            "Só é possível se ela nunca tiver sido usada em um ensaio registrado.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            self._config_repo.delete(config.id)
        except RecordInUseError as exc:
            QtWidgets.QMessageBox.warning(self, "Não é possível excluir", str(exc))
            return
        self.refresh_history()

    def _on_load_history(self) -> None:
        config: TestParameterConfig | None = self.history_combo.currentData()
        if config is None:
            return
        self.config_name_edit.setText(config.name)
        self.nominal_voltage_spin.setValue(config.nominal_voltage)
        self.voltage_min_spin.setValue(config.voltage_min)
        self.voltage_max_spin.setValue(config.voltage_max)
        self.current_max_spin.setValue(config.current_max)
        range_index = self.range_combo.findData(config.range_mode)
        self.range_combo.setCurrentIndex(range_index if range_index >= 0 else 0)
        # Config guarda segundos; converte para a unidade exibida no momento.
        factor = self._duration_factor
        self.test_duration_spin.setValue(config.test_duration_s / factor)
        self.sequence_table.setRowCount(0)
        for step in config.power_sequence:
            self._append_step_row(
                step.voltage, step.current, step.duration_s / factor, step.off_duration_s / factor
            )
        # setCurrentIndex() só emite currentIndexChanged se o índice mudou; força
        # a atualização do feedback mesmo quando a faixa carregada é a mesma que
        # já estava selecionada (ex.: "Automática" nos dois casos).
        self._update_single_step_range_feedback()
        self._update_all_row_range_feedback()

    def _on_add_step(self) -> None:
        self._append_step_row(
            self.nominal_voltage_spin.value(),
            self.current_max_spin.value(),
            self.test_duration_spin.value(),
            0.0,  # Tempo OFF: opcional, o operador edita a célula se quiser
        )

    def _append_step_row(
        self, voltage: float, current: float, duration: float, off_duration: float = 0.0
    ) -> None:
        """`duration`/`off_duration` estão na unidade exibida (s/min/h); viram
        segundos só na leitura (`_read_power_sequence`)."""
        row = self.sequence_table.rowCount()
        self.sequence_table.insertRow(row)
        for column, value in enumerate((voltage, current, duration, off_duration)):
            self.sequence_table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value)))
        self._update_row_range_feedback(row)

    # -- faixa V/A ------------------------------------------------------------

    def _update_single_step_range_feedback(self) -> None:
        """Colore Tensão nominal/Corrente máxima ANTES do -222 do instrumento
        -- usados quando não há sequência multi-step (ver TestRunConfig.steps())."""
        result = evaluate_range_fit(
            self.nominal_voltage_spin.value(),
            self.current_max_spin.value(),
            self._ranges,
            self.range_combo.currentData(),
        )
        apply_spin_feedback(self.nominal_voltage_spin, result)
        apply_spin_feedback(self.current_max_spin, result)
        self.range_warning_label.setText(result.message)
        self.range_warning_label.setVisible(result.state is not RangeFitState.OK)

    def _on_sequence_item_changed(self, item: QtWidgets.QTableWidgetItem) -> None:
        if item.column() in (0, 1):  # tensão/corrente -- duração (2) não afeta faixa
            self._update_row_range_feedback(item.row())

    def _update_row_range_feedback(self, row: int) -> None:
        voltage_item = self.sequence_table.item(row, 0)
        current_item = self.sequence_table.item(row, 1)
        if voltage_item is None or current_item is None:
            return  # linha ainda sendo montada por _append_step_row
        try:
            voltage = float(voltage_item.text())
            current = float(current_item.text())
        except ValueError:
            return  # célula em edição/inválida -- validado de verdade em _read_power_sequence
        result = evaluate_range_fit(voltage, current, self._ranges, self.range_combo.currentData())
        if result.state is RangeFitState.OK:
            # Faixa de hardware OK -- ainda falta conferir se o passo respeita
            # os limites que o operador definiu para ESTE ensaio (não é só
            # "a fonte aceita", é "está dentro do que foi documentado").
            result = evaluate_test_limit_fit(
                voltage,
                current,
                self.voltage_min_spin.value(),
                self.voltage_max_spin.value(),
                self.current_max_spin.value(),
            )
        # setBackground()/setToolTip() disparam itemChanged de novo -- sem
        # bloquear, cada edição de célula reentraria aqui indefinidamente.
        with QtCore.QSignalBlocker(self.sequence_table):
            apply_table_item_feedback(voltage_item, result)
            apply_table_item_feedback(current_item, result)

    def _update_all_row_range_feedback(self) -> None:
        for row in range(self.sequence_table.rowCount()):
            self._update_row_range_feedback(row)

    def _on_remove_step(self) -> None:
        row = self.sequence_table.currentRow()
        if row >= 0:
            self.sequence_table.removeRow(row)

    def _read_power_sequence(self) -> list[PowerStep] | None:
        factor = self._duration_factor
        steps: list[PowerStep] = []
        for row in range(self.sequence_table.rowCount()):
            try:
                voltage = float(self.sequence_table.item(row, 0).text())
                current = float(self.sequence_table.item(row, 1).text())
                duration = float(self.sequence_table.item(row, 2).text())
                off_duration = float(self.sequence_table.item(row, 3).text())
            except (AttributeError, ValueError):
                QtWidgets.QMessageBox.warning(
                    self, "Sequência inválida", f"Linha {row + 1} da sequência tem valor inválido."
                )
                return None
            if off_duration < 0:
                QtWidgets.QMessageBox.warning(
                    self, "Sequência inválida", f"Linha {row + 1}: Tempo OFF não pode ser negativo."
                )
                return None
            # Tabela em s/min/h -> PowerStep sempre em segundos.
            steps.append(
                PowerStep(
                    voltage=voltage,
                    current=current,
                    duration_s=duration * factor,
                    off_duration_s=off_duration * factor,
                )
            )
        return steps

    def _on_submit(self) -> None:
        if self._board is None:
            QtWidgets.QMessageBox.warning(self, "Placa não definida", "Cadastre a placa antes de definir os parâmetros.")
            return

        config_name = self.config_name_edit.text().strip()
        if not config_name:
            QtWidgets.QMessageBox.warning(self, "Campo obrigatório", "Informe um nome para a configuração.")
            return

        if self.voltage_min_spin.value() > self.voltage_max_spin.value():
            QtWidgets.QMessageBox.warning(
                self, "Limites inválidos", "Tensão mínima não pode ser maior que a tensão máxima."
            )
            return

        power_sequence = self._read_power_sequence()
        if power_sequence is None:
            return

        range_mode = self.range_combo.currentData()
        # Bloqueia ANTES de gravar/aplicar -- descobrir só com o -222 do
        # instrumento (ou pior, no meio de um ensaio já em andamento) é
        # exatamente o que o feedback visual em tempo real tenta evitar.
        effective_steps = power_sequence or [
            PowerStep(self.nominal_voltage_spin.value(), self.current_max_spin.value(), 0.0)
        ]
        for index, step in enumerate(effective_steps, start=1):
            result = evaluate_range_fit(step.voltage, step.current, self._ranges, range_mode)
            if result.state is RangeFitState.OK and power_sequence:
                # Só se aplica a ciclos (sequência multi-step) -- o passo
                # único usa Tensão mínima/máxima como referência visual do
                # gráfico, não como limite de configuração (ver docstring
                # do módulo e do LiveChart).
                result = evaluate_test_limit_fit(
                    step.voltage,
                    step.current,
                    self.voltage_min_spin.value(),
                    self.voltage_max_spin.value(),
                    self.current_max_spin.value(),
                )
            if result.state is not RangeFitState.OK:
                where = f"Passo {index} da sequência" if power_sequence else "Tensão nominal/Corrente máxima"
                QtWidgets.QMessageBox.warning(
                    self, "Faixa V/A inválida", f"{where}: {result.message}"
                )
                return

        config = TestParameterConfig(
            id=None,
            board_id=self._board.id,
            name=config_name,
            nominal_voltage=self.nominal_voltage_spin.value(),
            voltage_min=self.voltage_min_spin.value(),
            voltage_max=self.voltage_max_spin.value(),
            current_max=self.current_max_spin.value(),
            test_duration_s=self.test_duration_spin.value() * self._duration_factor,
            power_sequence=power_sequence,
            range_mode=range_mode,
        )
        saved_config = self._config_repo.save(config)
        self.refresh_history()

        run_config = TestRunConfig(
            nominal_voltage=saved_config.nominal_voltage,
            voltage_min=saved_config.voltage_min,
            voltage_max=saved_config.voltage_max,
            current_max=saved_config.current_max,
            test_duration_s=saved_config.test_duration_s,
            power_sequence=saved_config.power_sequence,
            polling_rate_hz=self.polling_rate_spin.value(),
            stabilization_timeout_s=self.stabilization_timeout_spin.value(),
            stabilization_tolerance_v=self.stabilization_tolerance_spin.value(),
            monitoring_consecutive_failures_limit=self.consecutive_failures_spin.value(),
            capture_interval_s=self.capture_interval_spin.value(),
            ovp_level_v=self.ovp_level_spin.value(),
            ocp_level_a=self.ocp_level_spin.value(),
            range_mode=saved_config.range_mode,
        )

        self.parameters_submitted.emit(
            {"test_parameter_config": saved_config, "run_config": run_config}
        )
