"""Fonte simulada para o monitor serial/CAN opcional (modo demonstração).

Gera periodicamente um quadro de exemplo (formato slcan ilustrativo, ID
0x123) só para ter algo visível rolando na tela sem hardware conectado --
não representa o protocolo real do adaptador do operador, que ainda não foi
confirmado (ver `AuxSerialMonitor`). Serve para desenvolver/testar a tela sem
o conversor USB-CAN físico plugado.
"""
from __future__ import annotations

import random
import time


class SimulatedAuxSerial:
    """Fake mínimo de `serial.Serial` usado só pelo monitor auxiliar."""

    def __init__(self, *, seed: int | None = None) -> None:
        self.is_open = True
        self._rng = random.Random(seed)
        self._next_frame_at = time.monotonic()
        self._buffer = b""

    def close(self) -> None:
        self.is_open = False

    def read(self, size: int) -> bytes:
        now = time.monotonic()
        if not self._buffer:
            if now >= self._next_frame_at:
                self._buffer += self._make_heartbeat_frame()
                self._next_frame_at = now + self._rng.uniform(0.8, 1.5)
            else:
                time.sleep(min(0.05, max(0.0, self._next_frame_at - now)))
        chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk

    @property
    def in_waiting(self) -> int:
        return len(self._buffer)

    def _make_heartbeat_frame(self) -> bytes:
        # Quadro slcan de exemplo (ID std 0x123, 1 byte de dado) -- só
        # ilustrativo, para ver ALGO na tela em modo simulação.
        value = self._rng.randint(0, 255)
        return f"t1231{value:02X}\r".encode("ascii")
