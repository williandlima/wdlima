"""Testes do monitor serial/CAN opcional (fase 1: leitura bruta, sem decodificar
protocolo -- ver docstring de drivers/aux_serial_monitor.py). Não depende de Qt
nem de hardware: modo simulação (SimulatedAuxSerial) cobre o caminho de leitura.
"""
from __future__ import annotations

import time

import pytest

from drivers.aux_serial_monitor import AuxSerialConfig, AuxSerialMonitor
from drivers.exceptions import SerialConnectionError


def test_simulate_mode_eventually_yields_bytes() -> None:
    monitor = AuxSerialMonitor(AuxSerialConfig(port="", simulate=True))
    monitor.connect()
    try:
        deadline = time.monotonic() + 3.0
        received = b""
        while time.monotonic() < deadline and not received:
            received += monitor.read_available()
        assert received  # o heartbeat simulado chega em até ~1.5s
    finally:
        monitor.disconnect()


def test_real_mode_without_port_raises_before_touching_hardware() -> None:
    monitor = AuxSerialMonitor(AuxSerialConfig(port="", simulate=False))
    with pytest.raises(SerialConnectionError):
        monitor.connect()


def test_read_available_without_connect_raises() -> None:
    monitor = AuxSerialMonitor(AuxSerialConfig(port="", simulate=True))
    with pytest.raises(SerialConnectionError):
        monitor.read_available()


def test_disconnect_is_idempotent_and_closes_port() -> None:
    monitor = AuxSerialMonitor(AuxSerialConfig(port="", simulate=True))
    monitor.connect()
    assert monitor.is_open is True
    monitor.disconnect()
    assert monitor.is_open is False
    monitor.disconnect()  # chamar de novo não deve levantar
    assert monitor.is_open is False
