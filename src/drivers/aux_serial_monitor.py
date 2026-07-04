"""Leitura bruta e contínua de uma porta serial auxiliar (seção X).

Usada pelo monitor opcional de serial/CAN da placa sob teste -- ex.: um
conversor USB-CAN genérico ("USB-CAN-A") plugado à placa, que fica emitindo
periodicamente um quadro de "estou vivo". Diferente de `SerialTransport`
(que fala SCPI e sabe exatamente que comando esperar de resposta), este
transporte NÃO conhece nenhum protocolo: só abre a porta e devolve os bytes
brutos que chegarem, para exibição ao vivo na GUI. Adaptadores "USB-CAN-A"
genéricos variam de protocolo entre clones (alguns falam slcan padrão,
outros um binário proprietário do fabricante) -- decodificar o quadro fica
para quando o protocolo real for confirmado; por ora o objetivo é só
confirmar visualmente que algo está chegando.

Nunca deve ser instanciado/chamado na thread da GUI: toda leitura é
bloqueante até o timeout configurado (ver AuxSerialReaderWorker em
gui/widgets/aux_serial_panel.py).
"""
from __future__ import annotations

from dataclasses import dataclass

import serial

from drivers.exceptions import SerialConnectionError

# Timeout curto: o loop de leitura (ver AuxSerialReaderWorker) precisa
# verificar periodicamente se foi pedido para parar, então a porta não pode
# ficar bloqueada por muito tempo numa leitura sem dados.
_READ_TIMEOUT_S = 0.2
_READ_CHUNK_SIZE = 256


@dataclass
class AuxSerialConfig:
    port: str
    baudrate: int = 2_000_000
    simulate: bool = False


class AuxSerialMonitor:
    """Transporte bruto (sem protocolo) para o monitor serial/CAN opcional."""

    def __init__(self, config: AuxSerialConfig) -> None:
        self._config = config
        self._serial: serial.Serial | None = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def connect(self) -> None:
        if self._config.simulate:
            from drivers.simulated_aux_serial import SimulatedAuxSerial

            self._serial = SimulatedAuxSerial()
            return
        if not self._config.port:
            raise SerialConnectionError("Nenhuma porta selecionada para o monitor serial/CAN.")
        try:
            self._serial = serial.Serial(
                port=self._config.port,
                baudrate=self._config.baudrate,
                timeout=_READ_TIMEOUT_S,
            )
        except serial.SerialException as exc:
            raise SerialConnectionError(
                f"Falha ao abrir porta {self._config.port}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self._serial = None

    def read_available(self) -> bytes:
        """Lê o que estiver disponível, bloqueando até `_READ_TIMEOUT_S` se a
        porta estiver ociosa -- devolve b"" nesse caso (não é erro, só nada
        chegou ainda; o chamador simplesmente tenta de novo)."""
        if self._serial is None or not self._serial.is_open:
            raise SerialConnectionError("Tentativa de leitura em porta não conectada.")
        waiting = getattr(self._serial, "in_waiting", 0)
        size = waiting if waiting > 0 else _READ_CHUNK_SIZE
        try:
            return self._serial.read(size)
        except serial.SerialException as exc:
            raise SerialConnectionError(f"Falha ao ler porta: {exc}") from exc
