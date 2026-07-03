"""Máquina de estados do teste funcional (seção 3.3).

Importante: esta classe NÃO decide PASS/FAIL. Ela só controla a fonte,
monitora e persiste tensão/corrente. A avaliação é sempre manual,
registrada via `mark_evaluated()` depois que o operador observa os dados.

É deliberadamente independente de PySide6: `run()` é bloqueante e deve ser
chamado de dentro de um worker thread (QThread) pela camada de GUI — nunca
da thread principal. Os hooks `on_state_changed`/`on_sample`/`on_event` são
callbacks simples, não Qt Signals, para que esta classe seja testável com
pytest puro e instrumento mockado (tests/test_state_machine.py).

Diagrama de estados aprovado:

    Idle -> Initializing -> CheckingCommunication -> ConfiguringSource
    -> ApplyingVoltage -> Stabilizing -> Monitoring -> ShuttingDownOutput
    -> AwaitingManualEvaluation -> Completed

Qualquer falha (CommError/Faulted/Aborted) também converge em
ShuttingDownOutput antes de chegar a AwaitingManualEvaluation — o
desligamento de saída é sempre tentado, mesmo em erro (failsafe).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from database.models import PowerStep
from drivers.exceptions import InstrumentCommunicationError
from hardware.power_supply import PowerSupplyE363x
from core.sampling_buffer import Sample, SamplingBuffer

_logger = logging.getLogger("state_machine")


class TestState(str, Enum):
    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    CHECKING_COMMUNICATION = "CHECKING_COMMUNICATION"
    CONFIGURING_SOURCE = "CONFIGURING_SOURCE"
    APPLYING_VOLTAGE = "APPLYING_VOLTAGE"
    STABILIZING = "STABILIZING"
    MONITORING = "MONITORING"
    PROTECTION_TRIPPED = "PROTECTION_TRIPPED"
    SHUTTING_DOWN_OUTPUT = "SHUTTING_DOWN_OUTPUT"
    AWAITING_MANUAL_EVALUATION = "AWAITING_MANUAL_EVALUATION"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    COMM_ERROR = "COMM_ERROR"
    FAULTED = "FAULTED"


# Estados terminais possíveis para o retorno de run() / _finish().
_TERMINATION_STATES = (
    TestState.COMPLETED,
    TestState.ABORTED,
    TestState.COMM_ERROR,
    TestState.FAULTED,
)


@dataclass(frozen=True)
class TestRunConfig:
    nominal_voltage: float
    voltage_min: float
    voltage_max: float
    current_max: float
    test_duration_s: float
    power_sequence: list[PowerStep]
    polling_rate_hz: float
    stabilization_timeout_s: float
    stabilization_tolerance_v: float
    monitoring_consecutive_failures_limit: int
    # Intervalo entre capturas GRAVADAS (s). 0 = grava toda leitura. Distinto da
    # taxa de polling (display): evita overdata no relatório em ensaios longos.
    capture_interval_s: float = 0.0
    # Nível de disparo de OVP/OCP (V/A), definido diretamente pelo operador.
    # 0 = não configura a proteção (a fonte mantém seu próprio default e ela
    # não atua durante o ensaio). Calcular esse nível automaticamente a
    # partir de voltage_max/current_max já causou dois problemas reais:
    # disparo em overshoot/inrush normal, e SCPI -222 (Data out of range)
    # quando a margem ultrapassava a faixa aceita pelo instrumento — por
    # isso agora é o operador quem escolhe o valor exato.
    ovp_level_v: float = 0.0
    ocp_level_a: float = 0.0
    # Faixa V/A forçada pelo operador nos Parâmetros do ensaio (ver
    # PowerSupplyE363x.set_forced_range). None = seleção automática
    # (padrão/recomendado): cada passo troca de faixa sozinho conforme
    # necessário. Um nome de faixa trava o ensaio INTEIRO nela — se algum
    # passo da sequência não couber, falha com erro acionável em vez de
    # tentar outra faixa sozinho.
    range_mode: str | None = None

    def steps(self) -> list[PowerStep]:
        """Sequência efetiva: usa power_sequence se houver, senão 1 passo único."""
        if self.power_sequence:
            return self.power_sequence
        return [PowerStep(self.nominal_voltage, self.current_max, self.test_duration_s)]


class TestStateMachine:
    def __init__(
        self,
        instrument: PowerSupplyE363x,
        sampling_buffer: SamplingBuffer,
        config: TestRunConfig,
        on_state_changed: Callable[[TestState], None] = lambda state: None,
        on_sample: Callable[[Sample], None] = lambda sample: None,
        on_event: Callable[[str, str], None] = lambda level, message: None,
    ) -> None:
        self._instrument = instrument
        self._buffer = sampling_buffer
        self._config = config
        self._on_state_changed = on_state_changed
        self._on_sample = on_sample
        self._on_event = on_event
        self._abort_requested = threading.Event()
        self._state = TestState.IDLE
        self._termination_reason: TestState | None = None
        self._protection_choice_event = threading.Event()
        self._protection_restart: bool = False

    @property
    def state(self) -> TestState:
        return self._state

    @property
    def termination_reason(self) -> TestState | None:
        return self._termination_reason

    def request_abort(self) -> None:
        """Thread-safe: chamado da GUI thread para cancelar um teste em curso."""
        self._abort_requested.set()

    def set_protection_choice(self, restart: bool) -> None:
        """Thread-safe: chamado da GUI thread após o operador escolher reiniciar
        ou encerrar quando a proteção de hardware (OVP/OCP) disparou."""
        self._protection_restart = restart
        self._protection_choice_event.set()

    def _set_state(self, state: TestState) -> None:
        self._state = state
        self._on_event("INFO", f"Estado: {state.value}")
        self._on_state_changed(state)
        _logger.info("Transição de estado: %s", state.value)

    def run(self) -> TestState:
        """Executa o fluxo completo de teste. Bloqueante — chamar em worker thread.

        O `except Exception` aqui é deliberado: este é o limite de segurança
        (failsafe) de todo o sistema. Qualquer falha não prevista nas etapas
        abaixo ainda precisa garantir OUTPUT OFF antes de propagar — por
        isso a captura é ampla, mas só neste ponto único e mais externo.
        """
        try:
            self._set_state(TestState.INITIALIZING)
            if not self._initialize():
                return self._finish(TestState.COMM_ERROR)

            self._set_state(TestState.CHECKING_COMMUNICATION)
            if not self._check_communication():
                return self._finish(TestState.COMM_ERROR)

            # Loop de reinício após disparo de proteção (OVP/OCP): o operador
            # pode escolher reiniciar sem perder a sessão e os dados já coletados.
            is_restart = False
            while True:
                self._set_state(TestState.CONFIGURING_SOURCE)
                if not self._configure_source(clear_protection_latch=is_restart):
                    return self._finish(TestState.FAULTED)

                self._set_state(TestState.APPLYING_VOLTAGE)
                if not self._apply_voltage():
                    return self._finish(TestState.FAULTED)

                self._set_state(TestState.STABILIZING)
                stabilize_result = self._stabilize()
                if stabilize_result == "comm_error":
                    return self._finish(TestState.COMM_ERROR)
                if stabilize_result == "aborted":
                    return self._finish(TestState.ABORTED)
                if stabilize_result == "timeout":
                    # Estabilização é uma cortesia de espera, NÃO um veredito: se a
                    # tensão não assenta na tolerância a tempo, seguimos para o
                    # monitoramento mesmo assim, para que o operador VEJA as leituras
                    # e decida manualmente. Abortar aqui deixava o gráfico vazio e a
                    # tela "abrindo e fechando" em hardware que estabiliza devagar.
                    self._on_event(
                        "WARNING",
                        "Tensão não estabilizou dentro da tolerância/tempo; prosseguindo "
                        "para o monitoramento (avaliação é manual).",
                    )

                self._set_state(TestState.MONITORING)
                monitor_result = self._monitor()
                if monitor_result == "comm_error":
                    return self._finish(TestState.COMM_ERROR)
                if monitor_result == "aborted":
                    return self._finish(TestState.ABORTED)
                if monitor_result == "protection_tripped":
                    self._set_state(TestState.PROTECTION_TRIPPED)
                    restart = self._wait_for_protection_choice()
                    if restart:
                        self._on_event("INFO", "Operador optou por reiniciar o ensaio.")
                        is_restart = True  # próxima iteração deve limpar o latch
                        continue  # volta ao início do while: reconfigura e reaplica
                    else:
                        self._on_event("INFO", "Operador optou por encerrar o ensaio.")
                        return self._finish(TestState.COMPLETED)

                return self._finish(TestState.COMPLETED)
        except Exception as exc:
            self._on_event("ERROR", f"Falha inesperada na state machine: {exc}")
            _logger.exception("Falha inesperada na state machine")
            return self._finish(TestState.FAULTED)

    def _wait_for_protection_choice(self, timeout_s: float = 600.0) -> bool:
        """Bloqueia o worker até o operador escolher reiniciar (True) ou
        encerrar (False). Timeout de 10 min → encerra automaticamente."""
        self._protection_restart = False
        self._protection_choice_event.clear()
        self._protection_choice_event.wait(timeout=timeout_s)
        return self._protection_restart

    def mark_evaluated(self) -> None:
        """Transição final, disparada quando o operador salva a avaliação manual."""
        if self._state != TestState.AWAITING_MANUAL_EVALUATION:
            raise RuntimeError(
                "Avaliação só pode ser registrada após o teste atingir "
                f"AWAITING_MANUAL_EVALUATION (estado atual: {self._state.value})."
            )
        self._set_state(TestState.COMPLETED)

    # -- Etapas individuais -------------------------------------------------

    def _initialize(self) -> bool:
        try:
            self._instrument.connect()
            return True
        except InstrumentCommunicationError as exc:
            self._on_event("WARNING", f"Falha ao conectar, tentando reconexão: {exc}")
            return self._instrument.reconnect()

    def _check_communication(self) -> bool:
        """*IDN? + checagem de erros, com retry curto (instabilidade transitória)."""
        for attempt in range(1, 4):
            if self._instrument.heartbeat():
                return True
            self._on_event("WARNING", f"Heartbeat falhou (tentativa {attempt}/3).")
            time.sleep(1.0 * attempt)
        return False

    def _configure_source(self, clear_protection_latch: bool = False) -> bool:
        """Arma OVP/OCP somente se o operador definir um nível (> 0) nos
        parâmetros do ensaio — proteção de hardware real, independente da
        regra de não-avaliação automática (não é veredito de PASS/FAIL).

        Sem nível definido (0), nada é configurado aqui: a fonte mantém sua
        própria proteção padrão e ela não atua durante o ensaio, como pedido
        pelo operador. Calcular o nível automaticamente (versão anterior) já
        causou disparo em overshoot/inrush normal e SCPI -222 (Data out of
        range) quando o valor calculado ultrapassava a faixa do instrumento.

        clear_protection_latch=True somente nas iterações de reinício após
        disparo de OVP/OCP — nesse caso o CLEar é necessário e válido porque
        a proteção definitivamente disparou. Na primeira configuração (latch
        limpo), enviar CLEar pode gerar erro 521 em alguns firmwares da E363x.

        Também reafirma a faixa V/A forçada (se houver) ANTES do primeiro
        `apply()` do ensaio — não é I/O por si só (só grava a intenção no
        driver), mas precisa ocorrer aqui e não em __init__/on_connected
        porque `range_mode` é escolha do preset de parâmetros, não do
        instrumento em si.
        """
        self._instrument.set_forced_range(self._config.range_mode)
        if self._config.ovp_level_v <= 0 and self._config.ocp_level_a <= 0:
            return True
        for attempt in range(1, 3):
            try:
                # Limpa fila de erros residuais (do heartbeat, shutdown anterior
                # ou outras operações) antes de reconfigurar, para que o
                # check_error() ao final reflita apenas esta sequência.
                self._instrument.clear_status()
                if self._config.ovp_level_v > 0:
                    self._instrument.set_overvoltage_protection(
                        self._config.ovp_level_v,
                        clear_latch=clear_protection_latch,
                    )
                if self._config.ocp_level_a > 0:
                    self._instrument.set_overcurrent_protection(
                        self._config.ocp_level_a,
                        clear_latch=clear_protection_latch,
                    )
                return True
            except InstrumentCommunicationError as exc:
                self._on_event(
                    "WARNING", f"Erro ao configurar fonte (tentativa {attempt}/2): {exc}"
                )
        return False

    def _apply_voltage(self) -> bool:
        """Sem retry: aplicar tensão errada numa placa é mais grave que abortar."""
        first_step = self._config.steps()[0]
        try:
            self._instrument.apply(first_step.voltage, first_step.current)
            self._instrument.output_on()
            return True
        except InstrumentCommunicationError as exc:
            self._on_event("ERROR", f"Falha ao aplicar tensão: {exc}")
            return False

    def _stabilize(self) -> str:
        target_voltage = self._config.steps()[0].voltage
        poll_interval = 1.0 / self._config.polling_rate_hz
        deadline = time.monotonic() + self._config.stabilization_timeout_s

        while time.monotonic() < deadline:
            if self._abort_requested.is_set():
                return "aborted"
            try:
                measured = self._instrument.measure_voltage()
            except InstrumentCommunicationError as exc:
                self._on_event("ERROR", f"Erro de leitura durante estabilização: {exc}")
                return "comm_error"
            if abs(measured - target_voltage) <= self._config.stabilization_tolerance_v:
                return "stable"
            time.sleep(poll_interval)
        return "timeout"

    def _monitor(self) -> str:
        """Polling síncrono e pausado (seção 10): um comando por vez, nunca
        enfileirado, para não disparar -521 Input buffer overflow na fonte."""
        consecutive_failures = 0
        poll_interval = 1.0 / self._config.polling_rate_hz
        capture_interval = self._config.capture_interval_s
        last_capture_monotonic: float | None = None
        last_capture_step: int | None = None

        steps = self._config.steps()
        for step_index, step in enumerate(steps):
            if step_index > 0:
                try:
                    self._instrument.apply(step.voltage, step.current)
                except InstrumentCommunicationError as exc:
                    self._on_event("ERROR", f"Falha ao aplicar passo {step_index}: {exc}")
                    return "comm_error"

            step_deadline = time.monotonic() + step.duration_s
            while time.monotonic() < step_deadline:
                if self._abort_requested.is_set():
                    return "aborted"

                loop_start = time.monotonic()
                try:
                    voltage = self._instrument.measure_voltage()
                    current = self._instrument.measure_current()
                except InstrumentCommunicationError as exc:
                    consecutive_failures += 1
                    self._on_event(
                        "WARNING",
                        f"Falha de leitura ({consecutive_failures}/"
                        f"{self._config.monitoring_consecutive_failures_limit}): {exc}",
                    )
                    # Ressincroniza: descarta resposta atrasada no buffer para a
                    # próxima leitura não casar a resposta com o comando errado.
                    self._instrument.reset_io_buffers()
                    if consecutive_failures >= self._config.monitoring_consecutive_failures_limit:
                        return "comm_error"
                    time.sleep(poll_interval)
                    continue

                consecutive_failures = 0
                sample = Sample(
                    timestamp=time.time(),
                    step_index=step_index,
                    voltage=voltage,
                    current=current,
                )
                # Display SEMPRE em tempo real; gravação só na taxa de captura
                # (evita overdata). Garante a 1ª amostra de cada ciclo gravada.
                self._on_sample(sample)
                if self._should_capture(loop_start, step_index, last_capture_monotonic, last_capture_step):
                    self._buffer.add_sample(sample)
                    last_capture_monotonic = loop_start
                    last_capture_step = step_index

                # Detecção de disparo de OVP/OCP: só verifica quando a proteção
                # está armada E a tensão medida cai abruptamente a menos de 10%
                # do setpoint (com setpoint >= 2 V para evitar falso-positivo em
                # passos de tensão intrinsecamente baixa). Só então faz 1-2
                # queries extras ao instrumento, mantendo overhead zero no caso
                # normal.
                prot_armed = self._config.ovp_level_v > 0 or self._config.ocp_level_a > 0
                if prot_armed and step.voltage >= 2.0 and voltage < 0.1 * step.voltage:
                    try:
                        ovp_tripped = (
                            self._config.ovp_level_v > 0
                            and self._instrument.is_overvoltage_protection_tripped()
                        )
                        ocp_tripped = (
                            not ovp_tripped
                            and self._config.ocp_level_a > 0
                            and self._instrument.is_overcurrent_protection_tripped()
                        )
                        if ovp_tripped or ocp_tripped:
                            prot_name = "OVP" if ovp_tripped else "OCP"
                            self._on_event(
                                "WARNING",
                                f"Proteção de hardware disparou ({prot_name}) — "
                                "saída desligada pelo instrumento.",
                            )
                            return "protection_tripped"
                    except InstrumentCommunicationError:
                        pass  # best-effort: não interrompe o ensaio por falha de leitura de status

                elapsed = time.monotonic() - loop_start
                time.sleep(max(0.0, poll_interval - elapsed))

            # Tempo OFF entre ciclos (opcional, por passo — seção "ciclos
            # térmicos"): nunca depois do ÚLTIMO passo, já que o desligamento
            # de saída ao fim do ensaio (SHUTTING_DOWN_OUTPUT) já cobre isso.
            if step.off_duration_s > 0 and step_index < len(steps) - 1:
                off_result = self._apply_off_period(step_index, step.off_duration_s)
                if off_result is not None:
                    return off_result

        return "completed"

    def _apply_off_period(self, step_index: int, off_duration_s: float) -> str | None:
        """Desliga a saída por `off_duration_s` antes do próximo passo.

        Continua medindo e publicando amostras reais nesse intervalo (mesma
        taxa de poll do monitoramento normal) -- sem isso, o gráfico ao vivo
        ficava "congelado" no último valor do passo anterior durante todo o
        tempo OFF, em vez de refletir a tensão real caindo para perto de
        zero com a saída desligada.

        Retorna None se completou normalmente, ou "aborted"/"comm_error" se
        a espera foi interrompida — mesmo protocolo dos outros métodos desta
        classe (nunca levanta, quem chama decide como encerrar o ensaio).
        Em caso de abort, a saída já está DESLIGADA (estado seguro) — não
        tenta religar antes de retornar, ao contrário do caminho normal.
        """
        self._on_event(
            "INFO", f"Tempo OFF ({off_duration_s:.1f}s) após o passo {step_index + 1}."
        )
        try:
            self._instrument.output_off()
        except InstrumentCommunicationError as exc:
            self._on_event("ERROR", f"Falha ao desligar saída no tempo OFF: {exc}")
            return "comm_error"

        poll_interval = 1.0 / self._config.polling_rate_hz
        last_capture_monotonic: float | None = None
        deadline = time.monotonic() + off_duration_s
        while time.monotonic() < deadline:
            if self._abort_requested.is_set():
                return "aborted"

            loop_start = time.monotonic()
            try:
                voltage = self._instrument.measure_voltage()
                current = self._instrument.measure_current()
            except InstrumentCommunicationError as exc:
                # Saída já está desligada (estado seguro) -- uma falha de
                # leitura aqui não justifica abortar o ensaio, só fica sem
                # amostra neste instante.
                self._on_event("WARNING", f"Falha ao medir durante o tempo OFF: {exc}")
                self._instrument.reset_io_buffers()
                time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
                continue

            sample = Sample(
                timestamp=time.time(), step_index=step_index, voltage=voltage, current=current
            )
            self._on_sample(sample)
            if self._should_capture(loop_start, step_index, last_capture_monotonic, step_index):
                self._buffer.add_sample(sample)
                last_capture_monotonic = loop_start

            elapsed = time.monotonic() - loop_start
            time.sleep(max(0.0, min(poll_interval - elapsed, deadline - time.monotonic())))

        try:
            self._instrument.output_on()
        except InstrumentCommunicationError as exc:
            self._on_event("ERROR", f"Falha ao religar saída após o tempo OFF: {exc}")
            return "comm_error"
        return None

    def _should_capture(
        self,
        now_monotonic: float,
        step_index: int,
        last_capture_monotonic: float | None,
        last_capture_step: int | None,
    ) -> bool:
        """Decide se a amostra atual deve ser GRAVADA (não só exibida).

        Grava se: captura está desativada (intervalo <= 0, grava todas), é a
        primeira amostra, mudou de ciclo (garante 1 ponto por passo), ou já
        passou o intervalo de captura desde a última gravação.
        """
        if self._config.capture_interval_s <= 0:
            return True
        if last_capture_monotonic is None or step_index != last_capture_step:
            return True
        return (now_monotonic - last_capture_monotonic) >= self._config.capture_interval_s

    def _finish(self, termination_reason: TestState) -> TestState:
        self._termination_reason = termination_reason
        self._set_state(TestState.SHUTTING_DOWN_OUTPUT)
        self._safe_shutdown_output()
        self._buffer.flush()
        self._set_state(TestState.AWAITING_MANUAL_EVALUATION)
        return termination_reason

    def _safe_shutdown_output(self) -> None:
        """Failsafe: tentativa best-effort de desligar a saída, sempre."""
        try:
            self._instrument.output_off()
        except InstrumentCommunicationError as exc:
            self._on_event(
                "ERROR",
                f"Não foi possível confirmar desligamento da saída via SCPI: {exc}. "
                "Desligamento manual da fonte pode ser necessário.",
            )
