"""Janela principal: orquestra o fluxo Cadastro -> Parâmetros -> Monitoramento
-> Avaliação manual (seção 3.2/3.3).

O state machine roda em uma QThread dedicada (`TestRunWorker`) para nunca
bloquear a GUI (seção 4). Os hooks `on_state_changed`/`on_sample`/`on_event`
de `core/state_machine.py` são callbacks simples — aqui são conectados a
sinais Qt do worker, que o framework já entrega na thread da GUI via
conexão em fila quando emitidos de outra thread.

O status técnico da sessão (`TestSessionStatus`) é fixado por esta classe
no instante em que o worker termina, a partir do `termination_reason` do
state machine — nunca pela tela de avaliação, que só grava o julgamento
manual do operador (`Evaluation`), um campo deliberadamente separado.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import deque
from dataclasses import asdict
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from config import AppConfig
from core.sampling_buffer import Sample, SamplingBuffer
from core.state_machine import TestRunConfig, TestState, TestStateMachine
from database.database import Database
from database.models import (
    Board,
    EventLogEntry,
    MonitoredSample,
    Operator,
    PowerStep,
    TestSession,
    TestSessionStatus,
)
from database.repositories import (
    BoardRepository,
    EvaluationRepository,
    EventLogRepository,
    MonitoredSampleRepository,
    OperatorRepository,
    TestParameterConfigRepository,
    TestSessionRepository,
)
from drivers.exceptions import InstrumentCommunicationError
from gui.evaluation_view import EvaluationView
from gui.manual_output_dialog import ManualOutputDialog
from gui.registration_view import RegistrationView
from gui.styles import load_theme
from gui.test_parameters_view import TestParametersView
from gui.widgets.header_bar import HeaderBar
from gui.widgets.live_chart import LiveChart
from gui.widgets.segment_display import SegmentDisplay
from gui.widgets.status_badge import StatusBadge
from gui.widgets.step_indicator import StepIndicator
from gui.widgets.toast import show_toast
from hardware.power_supply import PowerSupplyE363x
from logger import UI_LOG_BUFFER
from version import APP_VERSION
from reports.excel_report import generate_excel_report
from reports.pdf_report import generate_pdf_report
from reports.report_data import assemble_report_data
from reports.template_engine import report_filename
from reports.word_report import generate_word_report

_logger = logging.getLogger("app")

_TERMINATION_TO_SESSION_STATUS = {
    TestState.COMPLETED: TestSessionStatus.COMPLETED,
    TestState.ABORTED: TestSessionStatus.ABORTED,
    TestState.COMM_ERROR: TestSessionStatus.COMM_ERROR,
    TestState.FAULTED: TestSessionStatus.FAULTED,
}

# Conteúdo estático do botão "Ajuda" do cabeçalho (ver HeaderBar/_on_help) --
# texto único de ponta a ponta em vez de ajuda contextual por tela, para
# manter o impacto no código mínimo (um QDialog, sem estado por página).
_HELP_HTML = """
<h3>Fluxo do ensaio</h3>
<ol>
<li><b>Cadastro</b>: informe o operador e os dados da placa (código, part
number, revisão).</li>
<li><b>Parâmetros</b>: defina tensão nominal, corrente máxima e duração —
ou monte uma sequência multi-step com "Adicionar passo" (cada passo pode
ter um "Tempo OFF" opcional, saída desligada, antes do próximo). Escolha a
"Faixa da fonte" (Automática é o recomendado). Campos ficam com borda
colorida e uma mensagem se o valor não couber na faixa da fonte.</li>
<li><b>Ensaio</b>: acompanha tensão/corrente ao vivo, com gráfico e o passo
atual da sequência. Pode abortar a qualquer momento.</li>
<li><b>Avaliação</b>: registre manualmente Aprovado / Reprovado /
Observação — o veredito é sempre humano, nunca automático — e salve o
relatório (Word, Excel e PDF de uma vez).</li>
</ol>
<h3>Cabeçalho (sempre visível)</h3>
<ul>
<li><b>Porta da fonte</b>: escolha manual, ou deixe em "Automático" para
detectar pelo VID/PID configurado.</li>
<li><b>Testar conexão</b>: sonda a fonte antes de iniciar qualquer ensaio
— use sempre que trocar de porta ou religar o cabo.</li>
<li><b>Saída manual…</b>: liga/desliga a saída fora da sequência do
ensaio, para uma verificação rápida em bancada. Sempre desliga a saída
ao fechar.</li>
<li><b>Simulação</b>: roda o app inteiro com uma fonte virtual, sem
hardware conectado — útil para treinar ou testar parâmetros.</li>
</ul>
<p><i>Em caso de dúvida, consulte o responsável técnico do FCT.</i></p>
"""


def _total_monitored_duration_s(steps: list[PowerStep]) -> float:
    """Duração total exibida no eixo X do gráfico ao vivo (seção 3.3).

    Soma `duration_s` de todos os passos MAIS `off_duration_s` de todos
    menos o último -- espelha exatamente a regra de
    `TestStateMachine._monitor()` (off_duration_s do ÚLTIMO passo nunca é
    aplicado, o desligamento de saída ao fim do ensaio já cobre isso).
    Sem incluir o tempo OFF aqui, o eixo X ficava curto demais sempre que
    algum passo tinha tempo OFF configurado, e a curva "saía" do gráfico
    pela direita assim que o tempo OFF passou a gerar amostras reais
    (platô em zero) em vez de ser instantâneo.
    """
    return sum(step.duration_s for step in steps) + sum(
        step.off_duration_s for step in steps[:-1]
    )


class TestRunWorker(QtCore.QThread):
    """Executa `TestStateMachine.run()` fora da thread da GUI."""

    state_changed = QtCore.Signal(str)
    sample_received = QtCore.Signal(object)
    event_logged = QtCore.Signal(str, str)

    def __init__(
        self, state_machine: TestStateMachine | None = None, parent: QtCore.QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.state_machine = state_machine

    def run(self) -> None:
        self.state_machine.run()


class ConnectionProbeWorker(QtCore.QThread):
    """Roda `instrument.test_connection()` fora da GUI (a sonda é bloqueante).

    Emite `succeeded` com a string de identificação ou `failed` com a mensagem
    diagnóstica já tratada (timeout vs framing), nunca um traceback cru.
    """

    succeeded = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(
        self,
        instrument: PowerSupplyE363x,
        port: str,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._instrument = instrument
        self._port = port

    def run(self) -> None:
        try:
            self._instrument.set_port(self._port or None)
            identity = self._instrument.test_connection()
            self.succeeded.emit(identity)
        except InstrumentCommunicationError as exc:
            self.failed.emit(str(exc))


class _MonitoringPanel(QtWidgets.QWidget):
    """Tela de status/leituras ao vivo (seção 11.1) — readouts VFD + badges + gráfico."""

    abort_requested = QtCore.Signal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        outer_layout = QtWidgets.QVBoxLayout(self)

        title_label = QtWidgets.QLabel("Monitoramento do ensaio")
        title_label.setObjectName("viewTitle")
        outer_layout.addWidget(title_label)

        # QScrollArea: sem isto, com a janela restaurada (não maximizada) o
        # conteúdo (gráfico + readouts + log) é cortado sem como rolar, já
        # que o QStackedWidget dimensiona a janela pela maior página — as
        # outras 3 páginas (cadastro/parâmetros/avaliação) já têm scroll.
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer_layout.addWidget(scroll)

        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        layout = QtWidgets.QVBoxLayout(content)

        self._total_steps = 1
        status_row = QtWidgets.QHBoxLayout()
        self.state_label = QtWidgets.QLabel("Estado: —")
        self.cycle_label = QtWidgets.QLabel("Ciclo: —")
        self.cycle_label.setObjectName("cycleLabel")
        status_row.addWidget(self.state_label)
        status_row.addStretch()
        status_row.addWidget(self.cycle_label)
        layout.addLayout(status_row)

        # Barra de progresso do ensaio (tempo decorrido / duração configurada).
        self._test_duration_ms: int = 1
        self._elapsed_timer = QtCore.QElapsedTimer()
        self._progress_timer = QtCore.QTimer(self)
        self._progress_timer.setInterval(250)
        self._progress_timer.timeout.connect(self._update_progress)
        self._progress_bar = QtWidgets.QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setFixedHeight(6)
        self._progress_bar.setStyleSheet(
            "QProgressBar { background: #1A2B4A; border: none; border-radius: 3px; }"
            "QProgressBar::chunk { background: #FF7A29; border-radius: 3px; }"
        )
        layout.addWidget(self._progress_bar)

        badges_layout = QtWidgets.QHBoxLayout()
        self.remote_badge = StatusBadge("REMOTO")
        self.output_badge = StatusBadge("SAÍDA")
        self.protection_badge = StatusBadge("PROTEÇÃO")
        for badge in (self.remote_badge, self.output_badge, self.protection_badge):
            badges_layout.addWidget(badge)
        badges_layout.addStretch()
        layout.addLayout(badges_layout)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        readouts_widget = QtWidgets.QWidget()
        readouts_layout = QtWidgets.QVBoxLayout(readouts_widget)
        readouts_layout.setSpacing(4)

        # Corrente — grandeza primária do ensaio (maior destaque visual).
        _lbl_current = QtWidgets.QLabel("CORRENTE")
        _lbl_current.setProperty("displayRole", "primary")
        self.current_display = SegmentDisplay(unit="A", decimals=3, font_size=36)
        self.current_display.setObjectName("segmentDisplayCurrent")
        readouts_layout.addWidget(_lbl_current)
        readouts_layout.addWidget(self.current_display)

        readouts_layout.addSpacing(8)

        # Tensão — preset de procedimento (menor destaque visual).
        _lbl_voltage = QtWidgets.QLabel("Tensão")
        _lbl_voltage.setProperty("displayRole", "secondary")
        self.voltage_display = SegmentDisplay(unit="V", decimals=3, font_size=22)
        self.voltage_display.setObjectName("segmentDisplayVoltage")
        readouts_layout.addWidget(_lbl_voltage)
        readouts_layout.addWidget(self.voltage_display)

        readouts_layout.addStretch()
        splitter.addWidget(readouts_widget)

        self.live_chart = LiveChart()
        splitter.addWidget(self.live_chart)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)

        self.abort_button = QtWidgets.QPushButton("Abortar teste")
        self.abort_button.setObjectName("dangerButton")
        self.abort_button.clicked.connect(self.abort_requested.emit)
        layout.addWidget(self.abort_button)

        self.event_log_edit = QtWidgets.QPlainTextEdit()
        self.event_log_edit.setReadOnly(True)
        self.event_log_edit.setMaximumHeight(80)
        layout.addWidget(self.event_log_edit)

    def reset(
        self,
        voltage_min: float,
        voltage_max: float,
        duration_s: float,
        current_max: float,
        step_voltages: list[float] | None = None,
        total_steps: int = 1,
        protection_armed: bool = False,
    ) -> None:
        self._test_duration_ms = max(1, int(duration_s * 1000))
        self._progress_bar.setValue(0)
        self._elapsed_timer.restart()
        self._progress_timer.start()
        self._total_steps = max(1, total_steps)
        self.state_label.setText("Estado: —")
        self.cycle_label.setText(
            f"Ciclo: 1 de {self._total_steps}" if self._total_steps > 1 else "Passo único"
        )
        self.voltage_display.set_limits(voltage_min, voltage_max)
        self.current_display.set_limits(None, current_max)
        self.remote_badge.set_unknown()
        self.output_badge.set_unknown()
        # Reflete se o operador realmente armou OVP/OCP (seção 3.3): com
        # ovp_level_v/ocp_level_a = 0 a fonte não tem proteção configurada
        # por este app, e o badge fixo em "True" estava mentindo isso pro
        # operador (sempre verde, mesmo sem nada armado).
        self.protection_badge.set_active(protection_armed)
        self.live_chart.clear()
        self.live_chart.set_voltage_limits(voltage_min, voltage_max, duration_s, step_voltages)
        self.live_chart.set_current_range(current_max)
        self.event_log_edit.clear()

    def on_state_changed(self, state_value: str) -> None:
        self.state_label.setText(f"Estado: {state_value}")
        state = TestState(state_value)
        self.remote_badge.set_active(
            state
            not in (TestState.IDLE, TestState.INITIALIZING, TestState.CHECKING_COMMUNICATION)
        )
        self.output_badge.set_active(
            state in (TestState.APPLYING_VOLTAGE, TestState.STABILIZING, TestState.MONITORING)
        )
        _terminal = (TestState.COMPLETED, TestState.ABORTED, TestState.COMM_ERROR, TestState.FAULTED)
        if state in _terminal:
            self._progress_timer.stop()
            if state == TestState.COMPLETED:
                self._progress_bar.setValue(100)

    def _update_progress(self) -> None:
        pct = min(100, int(self._elapsed_timer.elapsed() * 100 // self._test_duration_ms))
        self._progress_bar.setValue(pct)

    def on_sample(self, sample: Sample) -> None:
        self.voltage_display.set_value(sample.voltage)
        self.current_display.set_value(sample.current)
        if self._total_steps > 1:
            self.cycle_label.setText(f"Ciclo: {sample.step_index + 1} de {self._total_steps}")

    def append_event(self, level: str, message: str) -> None:
        self.event_log_edit.appendPlainText(f"[{level}] {message}")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, app_config: AppConfig, database: Database, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._app_config = app_config
        self._db = database

        self._operator_repo = OperatorRepository(database)
        self._board_repo = BoardRepository(database)
        self._config_repo = TestParameterConfigRepository(database)
        self._session_repo = TestSessionRepository(database)
        self._sample_repo = MonitoredSampleRepository(database)
        self._evaluation_repo = EvaluationRepository(database)
        self._event_repo = EventLogRepository(database)

        self._instrument = PowerSupplyE363x(
            app_config.serial, app_config.reconnection, ranges=app_config.instrument.ranges
        )

        self._board: Board | None = None
        self._operator: Operator | None = None
        self._registration_data: dict | None = None
        self._session: TestSession | None = None
        self._state_machine: TestStateMachine | None = None
        self._worker: TestRunWorker | None = None
        self._probe_worker: ConnectionProbeWorker | None = None
        # Escolha do operador na confirmação de abortar (seção 3.3): mantém
        # os dados para avaliação/relatório ou descarta a sessão abortada.
        self._discard_aborted_session = False
        # Janela rolante: limita a memória do gráfico ao vivo em ensaios longos.
        # O relatório usa as amostras GRAVADAS no banco, não esta lista.
        self._live_samples: deque[Sample] = deque(
            maxlen=app_config.test_defaults.live_buffer_maxlen
        )
        self._last_log_text = ""
        # Última mensagem de erro/aviso do ensaio, para diagnóstico ao terminar.
        self._last_error_message: str | None = None

        self.setWindowTitle(f"{app_config.branding.company_name} — FCT")
        self.setStyleSheet(load_theme(app_config.branding))

        self.header = HeaderBar(app_config.branding)
        self.header.test_connection_requested.connect(self._on_test_connection)
        self.header.manual_output_requested.connect(self._on_manual_output)
        self.header.help_requested.connect(self._on_help)
        # Estado inicial do botão "Simulação" segue o config (serial.simulate).
        self.header.set_simulation_enabled(app_config.serial.simulate)

        self.registration_view = RegistrationView(
            self._operator_repo, self._board_repo, app_config.security.operator_delete_password
        )
        self.parameters_view = TestParametersView(
            self._config_repo, asdict(app_config.test_defaults), ranges=app_config.instrument.ranges
        )
        self.monitoring_panel = _MonitoringPanel()
        self.evaluation_view = EvaluationView(self._evaluation_repo)

        self.step_indicator = StepIndicator(["Cadastro", "Parâmetros", "Ensaio", "Avaliação"])

        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.registration_view)
        self.stack.addWidget(self.parameters_view)
        self.stack.addWidget(self.monitoring_panel)
        self.stack.addWidget(self.evaluation_view)
        self.stack.currentChanged.connect(self._update_step_indicator)

        # Log da aplicação: faixa fina sempre visível (espaço reduzido).
        self.app_log_edit = QtWidgets.QPlainTextEdit()
        self.app_log_edit.setReadOnly(True)
        self.app_log_edit.setFixedHeight(44)

        log_row = QtWidgets.QHBoxLayout()
        log_row.setContentsMargins(0, 0, 0, 0)
        log_label = QtWidgets.QLabel("Log:")
        log_row.addWidget(log_label)
        log_row.addWidget(self.app_log_edit, stretch=1)

        central = QtWidgets.QWidget()
        central_layout = QtWidgets.QVBoxLayout(central)
        central_layout.addWidget(self.header)
        central_layout.addWidget(self.step_indicator)
        central_layout.addWidget(self.stack, stretch=1)
        central_layout.addLayout(log_row)
        self.setCentralWidget(central)

        self.registration_view.registration_submitted.connect(self._on_registration_submitted)
        self.parameters_view.parameters_submitted.connect(self._on_parameters_submitted)
        self.parameters_view.back_requested.connect(self._on_parameters_back)
        self.monitoring_panel.abort_requested.connect(self._on_abort_requested)
        self.evaluation_view.evaluation_submitted.connect(self._on_evaluation_submitted)

        self._update_step_indicator(self.stack.currentIndex())

        self._log_timer = QtCore.QTimer(self)
        self._log_timer.timeout.connect(self._refresh_log_panel)
        self._log_timer.start(1000)

        # Sombra suave nos painéis QGroupBox dos formulários.
        for _gb in self.findChildren(QtWidgets.QGroupBox):
            _sh = QtWidgets.QGraphicsDropShadowEffect(_gb)
            _sh.setBlurRadius(12)
            _sh.setOffset(0.0, 3.0)
            _sh.setColor(QtGui.QColor(0, 0, 0, 70))
            _gb.setGraphicsEffect(_sh)

    def _on_registration_submitted(self, data: dict) -> None:
        self._board = data["board"]
        self._operator = data["operator"]
        self._registration_data = data
        self.parameters_view.set_board(self._board)
        self._switch_to(self.parameters_view)

    def _on_parameters_back(self) -> None:
        """Volta aos dados de cadastro sem perder nada (edição não-destrutiva).

        Os campos do cadastro permanecem preenchidos; o operador pode ajustar e
        seguir de novo. A config em si é gravada por upsert no 'Salvar e
        continuar', então voltar e reentrar apenas sobrescreve a mesma config.
        """
        self._switch_to(self.registration_view)

    def _on_parameters_submitted(self, data: dict) -> None:
        config = data["test_parameter_config"]
        run_config: TestRunConfig = data["run_config"]
        registration = self._registration_data
        assert self._board is not None and self._operator is not None and registration is not None

        # Congela no snapshot os dados de rastreabilidade do instrumento válidos
        # NESTE ensaio (não o valor futuro do YAML).
        snapshot = asdict(run_config)
        instrument_cfg = self._app_config.instrument
        snapshot["instrument_model"] = instrument_cfg.model
        snapshot["instrument_asset_id"] = instrument_cfg.asset_id
        snapshot["instrument_calibration_due"] = instrument_cfg.calibration_due

        session = self._session_repo.create(
            TestSession(
                id=None,
                board_id=self._board.id,
                serial_number=registration["serial_number"],
                operator_id=self._operator.id,
                test_parameter_config_id=config.id,
                config_snapshot_json=json.dumps(snapshot),
                production_order=registration["production_order"],
                observations=registration["observations"],
                status=TestSessionStatus.RUNNING,
                started_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                app_version=APP_VERSION,
            )
        )
        self._session = session
        self._last_error_message = None
        self._live_samples = deque(maxlen=self._app_config.test_defaults.live_buffer_maxlen)

        buffer = SamplingBuffer(
            live_buffer_maxlen=self._app_config.test_defaults.live_buffer_maxlen,
            batch_size=self._app_config.test_defaults.sample_batch_size,
            batch_interval_s=self._app_config.test_defaults.sample_batch_interval_s,
            on_flush=lambda samples: self._persist_samples(session.id, samples),
        )

        worker = TestRunWorker()
        state_machine = TestStateMachine(
            instrument=self._instrument,
            sampling_buffer=buffer,
            config=run_config,
            on_state_changed=lambda state: worker.state_changed.emit(state.value),
            on_sample=lambda sample: worker.sample_received.emit(sample),
            on_event=lambda level, message: worker.event_logged.emit(level, message),
        )
        worker.state_machine = state_machine
        self._state_machine = state_machine
        self._worker = worker

        worker.state_changed.connect(self.monitoring_panel.on_state_changed)
        worker.state_changed.connect(self._on_worker_state_changed)
        worker.sample_received.connect(self._on_sample)
        worker.event_logged.connect(self._on_event)
        worker.finished.connect(self._on_worker_finished)

        # A porta escolhida pelo operador no cabeçalho vale para o teste real.
        self._instrument.set_port(self.header.selected_port() or None)
        # Modo simulação (fonte virtual) segue o botão do cabeçalho.
        self._instrument.set_simulate(self.header.simulation_enabled())

        steps = run_config.steps()
        total_duration_s = _total_monitored_duration_s(steps)
        self.monitoring_panel.reset(
            run_config.voltage_min,
            run_config.voltage_max,
            total_duration_s,
            run_config.current_max,
            step_voltages=[step.voltage for step in steps],
            total_steps=len(steps),
            protection_armed=run_config.ovp_level_v > 0 or run_config.ocp_level_a > 0,
        )
        self.header.test_button.setEnabled(False)  # sem sondar a porta durante o teste
        self._switch_to(self.monitoring_panel)
        worker.start()

    def _persist_samples(self, session_id: int, samples: list[Sample]) -> None:
        self._sample_repo.insert_batch(
            [
                MonitoredSample(
                    id=None,
                    test_session_id=session_id,
                    timestamp=dt.datetime.fromtimestamp(s.timestamp).strftime("%Y-%m-%d %H:%M:%S.%f"),
                    step_index=s.step_index,
                    voltage_measured=s.voltage,
                    current_measured=s.current,
                )
                for s in samples
            ]
        )

    def _on_sample(self, sample: Sample) -> None:
        self._live_samples.append(sample)  # deque limitado: descarta os mais antigos
        self.monitoring_panel.on_sample(sample)
        decimated = SamplingBuffer.decimate(list(self._live_samples), max_points=500)
        self.monitoring_panel.live_chart.update_samples(decimated)

    def _on_event(self, level: str, message: str) -> None:
        if level in ("ERROR", "WARNING"):
            self._last_error_message = message
        self.monitoring_panel.append_event(level, message)
        self._event_repo.add(
            EventLogEntry(
                id=None,
                test_session_id=self._session.id if self._session else None,
                timestamp=None,
                level=level,
                source="state_machine",
                message=message,
            )
        )

    def _on_abort_requested(self) -> None:
        if self._state_machine is None:
            return
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Question)
        box.setWindowTitle("Abortar ensaio")
        box.setText(
            "O ensaio será interrompido agora.\n\n"
            "Deseja manter os dados já coletados para avaliação e geração de relatório?"
        )
        keep_button = box.addButton("Manter dados e avaliar", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton("Descartar dados", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Cancelar", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (keep_button, discard_button):
            return  # Cancelar: o ensaio continua rodando
        self._discard_aborted_session = clicked is discard_button
        self._state_machine.request_abort()

    def _on_worker_state_changed(self, state_value: str) -> None:
        if state_value == TestState.PROTECTION_TRIPPED.value:
            self._on_protection_tripped()

    def _on_protection_tripped(self) -> None:
        if self._state_machine is None:
            return
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Proteção de hardware disparou")
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setText(
            "A proteção de hardware (OVP ou OCP) foi acionada e a saída da fonte "
            "foi desligada automaticamente.\n\n"
            "Verifique se o nível de OVP/OCP configurado é adequado para a tensão/corrente "
            "do ensaio e o que deseja fazer:"
        )
        restart_button = box.addButton(
            "Reiniciar ensaio", QtWidgets.QMessageBox.ButtonRole.AcceptRole
        )
        end_button = box.addButton(
            "Encerrar e avaliar dados", QtWidgets.QMessageBox.ButtonRole.DestructiveRole
        )
        box.setDefaultButton(restart_button)
        box.exec()
        restart = box.clickedButton() is restart_button
        self._state_machine.set_protection_choice(restart)

    def _on_worker_finished(self) -> None:
        assert self._state_machine is not None and self._session is not None
        termination = self._state_machine.termination_reason
        status = _TERMINATION_TO_SESSION_STATUS.get(termination, TestSessionStatus.FAULTED)
        self._session_repo.update_status(
            self._session.id, status, finished_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        # Rastreabilidade: grava a identidade (*IDN?) da fonte usada no ensaio.
        self._session_repo.set_instrument_identity(self._session.id, self._instrument.last_identity)

        # Fecha a porta entre ensaios (libera a COM para outro app / próximo ensaio).
        # Seguro aqui: o worker já terminou (este slot roda no 'finished'), então
        # não há acesso concorrente à porta. disconnect() também reforça OUTPUT OFF.
        try:
            if self._instrument.is_connected:
                self._instrument.disconnect()
        except InstrumentCommunicationError as exc:
            _logger.warning("Falha ao desconectar a fonte ao fim do ensaio: %s", exc)

        self.header.test_button.setEnabled(True)
        self.header.set_connection_unknown("Porta fechada após o ensaio; reconfirme a conexão.")

        # COMM_ERROR = o teste NÃO chegou a comunicar com a fonte (falha já na
        # 1ª etapa). Não faz sentido ir para a avaliação manual com uma sessão
        # sem amostras — isso é o que fazia a tela "abrir e já encerrar". Em vez
        # disso, avisa o operador com uma mensagem acionável e volta aos
        # parâmetros para nova tentativa (a placa/operador continuam carregados).
        detail = f"\n\nÚltimo erro registrado:\n{self._last_error_message}" if self._last_error_message else ""

        if status == TestSessionStatus.COMM_ERROR:
            self.header.set_connection_state(False, "Erro de comunicação durante o teste.")
            self._session = None
            self._state_machine = None
            self._worker = None
            QtWidgets.QMessageBox.warning(
                self,
                "O teste não executou",
                "Falha de comunicação com a fonte — o ensaio não chegou a iniciar.\n\n"
                "Verifique:\n"
                "• a porta COM e o cabo/adaptador (use \"Testar conexão\");\n"
                "• o baudrate/paridade iguais ao painel frontal da fonte;\n"
                "• ou marque \"Simulação\" no cabeçalho para rodar sem hardware."
                + detail,
            )
            self._switch_to(self.parameters_view)
            return

        # Abortado com "Descartar dados": o operador já decidiu na confirmação
        # de abortar (seção 3.3) que não quer avaliar/gerar relatório desta
        # sessão. Volta direto aos parâmetros, sem passar pela avaliação.
        if status == TestSessionStatus.ABORTED and self._discard_aborted_session:
            self._discard_aborted_session = False
            discarded_count = len(self._sample_repo.list_for_session(self._session.id))
            self._session = None
            self._state_machine = None
            self._worker = None
            QtWidgets.QMessageBox.information(
                self,
                "Ensaio abortado",
                f"O ensaio foi abortado. {discarded_count} amostra(s) coletada(s) foram descartadas "
                "(sem avaliação nem relatório).",
            )
            self.stack.setCurrentWidget(self.parameters_view)
            return

        self._discard_aborted_session = False
        samples = self._sample_repo.list_for_session(self._session.id)

        # FAULTED = falhou durante a execução (config/aplicação/erro inesperado).
        # Mostra o motivo antes de seguir — nunca terminar "em silêncio".
        if status == TestSessionStatus.FAULTED:
            QtWidgets.QMessageBox.warning(
                self,
                "Ensaio interrompido por falha",
                f"O ensaio terminou em FALHA ({len(samples)} amostra(s) gravada(s))."
                + (detail or "\n\nConsulte o log de eventos para detalhes."),
            )

        self.evaluation_view.load_session(self._session, self._operator, self._state_machine, samples)
        self._switch_to(self.evaluation_view)

    def _on_evaluation_submitted(self, data: dict) -> None:
        session: TestSession = data["session"]
        # "save_report" já reflete a escolha Sim/Não do operador (EvaluationView
        # pergunta isso ANTES de emitir o sinal) -- aqui só decide se abre o
        # diálogo "onde salvar" ou pula a geração do relatório de vez.
        if data.get("save_report", True):
            self._save_report(session.id)
        else:
            show_toast(self, "Ensaio encerrado sem salvar relatório.", level="info", duration_ms=4000)

        self._session = None
        self._state_machine = None
        self._worker = None
        self._board = None
        self._operator = None
        self._registration_data = None
        self.registration_view.refresh_operator_history()
        self.registration_view.clear_form()
        self._switch_to(self.registration_view)

    def _save_report(self, test_session_id: int) -> None:
        """Salva o relatório do ensaio com diálogo "Salvar como" do Windows.

        O operador escolhe pasta E nome do arquivo no diálogo nativo (mesmo
        modelo do Word). São gerados os três formatos (Word/Excel/PDF) com o
        nome escolhido. Cancelar o diálogo não impede o avanço para o próximo
        cadastro — só pula a geração do relatório.
        """
        data = assemble_report_data(
            test_session_id,
            self._session_repo,
            self._board_repo,
            self._operator_repo,
            self._sample_repo,
            self._evaluation_repo,
            self._event_repo,
        )

        default_name = report_filename(data, "docx")
        default_path = str(self._app_config.paths.exports_dir / default_name)
        chosen, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Salvar relatório do ensaio",
            default_path,
            "Relatório FCT (*.docx *.xlsx *.pdf)",
        )
        if not chosen:
            return

        chosen_path = Path(chosen)
        output_dir = chosen_path.parent
        base_name = chosen_path.stem
        try:
            saved_paths = [
                generate_word_report(data, self._app_config.branding, output_dir, base_name),
                generate_excel_report(data, self._app_config.branding, output_dir, base_name),
                generate_pdf_report(data, self._app_config.branding, output_dir, base_name),
            ]
        except Exception as exc:  # noqa: BLE001 — falha de geração não pode travar o fluxo
            _logger.exception("Falha ao gerar relatório do ensaio")
            QtWidgets.QMessageBox.warning(self, "Erro ao salvar relatório", str(exc))
            return

        show_toast(self, f"Relatório salvo em {output_dir}", level="success", duration_ms=4000)

    def _switch_to(self, widget: QtWidgets.QWidget) -> None:
        """Troca de página com fade-in de 180 ms; sem efeito se já é a página ativa."""
        if self.stack.currentWidget() is widget:
            return
        self.stack.setCurrentWidget(widget)
        effect = QtWidgets.QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
        anim = QtCore.QPropertyAnimation(effect, b"opacity", self)
        anim.setDuration(180)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: widget.setGraphicsEffect(None))
        anim.start(QtCore.QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    def _update_step_indicator(self, index: int) -> None:
        self.step_indicator.set_current(index)

    def _on_manual_output(self) -> None:
        """Abre o controle de saída manual (teste rápido), isolado do ensaio.

        Bloqueado enquanto um ensaio automático ou uma sondagem estiver em
        curso — o instrumento tem um dono por vez. O diálogo é modal e garante
        OUTPUT OFF ao fechar (failsafe).
        """
        if self._worker is not None and self._worker.isRunning():
            show_toast(self, "Aguarde o término do ensaio para usar a saída manual.", level="warning")
            return
        if self._probe_worker is not None and self._probe_worker.isRunning():
            show_toast(self, "Aguarde o término da sondagem da porta.", level="warning")
            return
        dialog = ManualOutputDialog(
            self._instrument,
            simulate=self.header.simulation_enabled(),
            port=self.header.selected_port() or None,
            parent=self,
        )
        dialog.exec()
        # Após o uso manual, a porta foi fechada pelo failsafe do diálogo.
        self.header.set_connection_unknown("Saída manual encerrada; reconfirme a conexão.")

    def _on_help(self) -> None:
        """Ajuda estática (sem estado/thread) — QDialog com QTextBrowser
        rolável, mais adequado que um QMessageBox para um texto longo de
        ponta a ponta sem cortar conteúdo em telas menores."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("Ajuda — Como usar o app")
        dialog.resize(560, 520)
        layout = QtWidgets.QVBoxLayout(dialog)
        browser = QtWidgets.QTextBrowser()
        browser.setOpenExternalLinks(False)
        browser.setHtml(_HELP_HTML)
        layout.addWidget(browser)
        close_button = QtWidgets.QPushButton("Fechar")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, alignment=QtCore.Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def _on_test_connection(self, port: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            show_toast(self, "Aguarde o término do teste para sondar a porta.", level="warning")
            return
        if self._probe_worker is not None and self._probe_worker.isRunning():
            return
        self.header.set_testing(True)
        self.header.set_connection_unknown("Sondando a fonte…")
        self._instrument.set_simulate(self.header.simulation_enabled())
        probe = ConnectionProbeWorker(self._instrument, port)
        probe.succeeded.connect(self._on_probe_success)
        probe.failed.connect(self._on_probe_failure)
        probe.finished.connect(lambda: self.header.set_testing(False))
        self._probe_worker = probe
        probe.start()

    def _on_probe_success(self, identity: str) -> None:
        self.header.set_connection_state(True, f"Conectada: {identity}")
        show_toast(self, f"Conexão OK — {identity}", level="success")

    def _on_probe_failure(self, message: str) -> None:
        self.header.set_connection_state(False, message)
        QtWidgets.QMessageBox.warning(self, "Falha na conexão", message)

    def _refresh_log_panel(self) -> None:
        # Só redesenha quando o conteúdo muda (o deque satura em 200 linhas, então
        # comparar por tamanho não basta): evita reescrever todo o QPlainTextEdit
        # a cada segundo e o salto de scroll que isso causava.
        text = "\n".join(UI_LOG_BUFFER)
        if text == self._last_log_text:
            return
        self._last_log_text = text
        self.app_log_edit.setPlainText(text)
        self.app_log_edit.verticalScrollBar().setValue(self.app_log_edit.verticalScrollBar().maximum())

    def closeEvent(self, event: QtCore.QEvent) -> None:
        # Aborta o ensaio ANTES de esperar: sem isso, num ensaio longo o wait
        # estourava, a thread seguia medindo, e a GUI desconectava a porta em
        # paralelo (acesso concorrente + saída podendo ficar ligada).
        if self._worker is not None and self._worker.isRunning():
            if self._state_machine is not None:
                self._state_machine.request_abort()
            self._worker.wait(5000)
        if self._probe_worker is not None and self._probe_worker.isRunning():
            self._probe_worker.wait(3000)
        if self._instrument.is_connected:
            self._instrument.disconnect()
        super().closeEvent(event)
