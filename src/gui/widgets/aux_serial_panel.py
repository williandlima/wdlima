"""Monitor serial/CAN opcional da placa sob teste (seção X).

Painel independente do ensaio (state machine): o operador escolhe a porta
do conversor (ex.: um USB-CAN genérico plugado à placa), conecta e vê os
bytes brutos chegando ao vivo, numa telinha rolável -- sem decodificar
nenhum protocolo. Serve pra confirmar visualmente que a placa está "viva"
(mandando o quadro de heartbeat) sem precisar de ferramenta externa.
Independente porque o protocolo real do adaptador ainda não foi confirmado
(clones de "USB-CAN-A" variam) -- decodificação de quadro fica para depois.

"Simulação" gera um quadro de exemplo periodicamente, para testar esta tela
sem o conversor físico conectado (mesmo espírito do modo simulação da
fonte, ver drivers/simulated_aux_serial.py).
"""
from __future__ import annotations

import datetime as dt
import re

from PySide6 import QtCore, QtWidgets

from drivers.aux_serial_monitor import AuxSerialConfig, AuxSerialMonitor
from drivers.exceptions import SerialConnectionError
from drivers.serial_driver import list_available_ports

_BAUD_PRESETS = ("9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600", "2000000")
_DEFAULT_BAUD = "2000000"
_MAX_LINES = 500
_LINE_SPLIT_RE = re.compile(rb"[\r\n]+")
# Sem terminador de linha (\r/\n) o buffer nunca seria esvaziado -- protege
# contra um protocolo binário sem separador acumulando para sempre.
_FLUSH_WITHOUT_TERMINATOR_AT = 256


class AuxSerialReaderWorker(QtCore.QThread):
    """Roda `AuxSerialMonitor` fora da thread da GUI (leitura é bloqueante)."""

    data_received = QtCore.Signal(bytes)
    error_occurred = QtCore.Signal(str)

    def __init__(self, monitor: AuxSerialMonitor, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._monitor = monitor
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        try:
            self._monitor.connect()
        except SerialConnectionError as exc:
            self.error_occurred.emit(str(exc))
            return
        try:
            while not self._stop_requested:
                try:
                    chunk = self._monitor.read_available()
                except SerialConnectionError as exc:
                    self.error_occurred.emit(str(exc))
                    break
                if chunk:
                    self.data_received.emit(chunk)
        finally:
            self._monitor.disconnect()


class AuxSerialPanel(QtWidgets.QGroupBox):
    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__("Monitor serial/CAN da placa (opcional)", parent)
        self._worker: AuxSerialReaderWorker | None = None
        self._buffer = b""

        layout = QtWidgets.QVBoxLayout(self)

        controls_row = QtWidgets.QHBoxLayout()
        controls_row.addWidget(QtWidgets.QLabel("Porta:"))
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(200)
        controls_row.addWidget(self.port_combo)

        self.refresh_button = QtWidgets.QPushButton("Atualizar")
        self.refresh_button.clicked.connect(self.refresh_ports)
        controls_row.addWidget(self.refresh_button)

        controls_row.addWidget(QtWidgets.QLabel("Baud:"))
        self.baud_combo = QtWidgets.QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(_BAUD_PRESETS)
        self.baud_combo.setCurrentText(_DEFAULT_BAUD)
        controls_row.addWidget(self.baud_combo)

        self.simulate_check = QtWidgets.QCheckBox("Simulação")
        self.simulate_check.setToolTip(
            "Gera um quadro de exemplo periodicamente, sem o conversor conectado -- "
            "só para testar esta tela."
        )
        controls_row.addWidget(self.simulate_check)

        self.connect_button = QtWidgets.QPushButton("Conectar")
        self.connect_button.clicked.connect(self._on_connect_clicked)
        controls_row.addWidget(self.connect_button)

        self.clear_button = QtWidgets.QPushButton("Limpar")
        self.clear_button.clicked.connect(self._on_clear_clicked)
        controls_row.addWidget(self.clear_button)

        controls_row.addStretch()
        layout.addLayout(controls_row)

        self.output_edit = QtWidgets.QPlainTextEdit()
        self.output_edit.setReadOnly(True)
        self.output_edit.setMaximumBlockCount(_MAX_LINES)
        self.output_edit.setMaximumHeight(160)
        self.output_edit.setPlaceholderText(
            "Bytes brutos recebidos aparecem aqui (hex + ASCII) -- sem decodificação "
            "de protocolo por enquanto."
        )
        layout.addWidget(self.output_edit)

        self.refresh_ports()

    # -- portas ---------------------------------------------------------------

    def refresh_ports(self) -> None:
        current = self.port_combo.currentData()
        self.port_combo.clear()
        for port in list_available_ports():
            label = f"{port.device} — {port.description}" if port.description else port.device
            self.port_combo.addItem(label, userData=port.device)
        index = self.port_combo.findData(current)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)

    # -- conectar/desconectar ---------------------------------------------------

    def is_connected(self) -> bool:
        return self._worker is not None

    def _on_connect_clicked(self) -> None:
        if self._worker is not None:
            self._stop()
            return
        self._start()

    def _start(self) -> None:
        simulate = self.simulate_check.isChecked()
        port = self.port_combo.currentData() or ""
        if not simulate and not port:
            QtWidgets.QMessageBox.warning(
                self, "Nenhuma porta selecionada",
                "Escolha a porta do conversor ou marque \"Simulação\" para testar sem hardware.",
            )
            return
        try:
            baudrate = int(self.baud_combo.currentText())
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Baud rate inválido", "Informe um número inteiro de baud rate.")
            return

        monitor = AuxSerialMonitor(AuxSerialConfig(port=port, baudrate=baudrate, simulate=simulate))
        worker = AuxSerialReaderWorker(monitor)
        worker.data_received.connect(self._on_data_received)
        worker.error_occurred.connect(self._on_error)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

        self.connect_button.setText("Desconectar")
        self.port_combo.setEnabled(False)
        self.baud_combo.setEnabled(False)
        self.simulate_check.setEnabled(False)
        self.refresh_button.setEnabled(False)

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(2000)

    def _on_worker_finished(self) -> None:
        self._worker = None
        self.connect_button.setText("Conectar")
        self.port_combo.setEnabled(True)
        self.baud_combo.setEnabled(True)
        self.simulate_check.setEnabled(True)
        self.refresh_button.setEnabled(True)

    def _on_error(self, message: str) -> None:
        QtWidgets.QMessageBox.warning(self, "Erro no monitor serial/CAN", message)

    # -- exibição ---------------------------------------------------------------

    def _on_data_received(self, chunk: bytes) -> None:
        self._buffer += chunk
        parts = _LINE_SPLIT_RE.split(self._buffer)
        self._buffer = parts[-1]
        for part in parts[:-1]:
            if part:
                self._append_line(part)
        if len(self._buffer) > _FLUSH_WITHOUT_TERMINATOR_AT:
            self._append_line(self._buffer)
            self._buffer = b""

    def _append_line(self, data: bytes) -> None:
        timestamp = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        hex_preview = data.hex(" ")
        ascii_preview = "".join(chr(b) if 32 <= b <= 126 else "." for b in data)
        self.output_edit.appendPlainText(f"[{timestamp}] {hex_preview}   |{ascii_preview}|")

    def _on_clear_clicked(self) -> None:
        self.output_edit.clear()

    # -- encerramento -------------------------------------------------------

    def shutdown(self) -> None:
        """Chamado pelo MainWindow ao fechar a janela -- garante que a thread
        de leitura não fique presa segurando a porta aberta."""
        self._stop()
