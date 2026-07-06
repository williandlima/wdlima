"""Histórico do gráfico ao vivo, com memória limitada pelo NÚMERO DE BINS
(seção 3.3) -- não por um FIFO de amostras.

Antes, o gráfico ao vivo era alimentado por um `deque(maxlen=N)` de amostras
brutas: assim que o ensaio passava da janela coberta por N amostras (em
testes longos, com `live_buffer_maxlen` dimensionado para ~1h a 1 Hz), as
amostras mais antigas eram descartadas -- a curva "apagava" o começo, mesmo
com o eixo X do gráfico continuando a mostrar a duração TOTAL configurada,
deixando um vão em branco crescendo à esquerda enquanto o ensaio avançava.

Este histórico resolve isso dividindo a duração total do ensaio (conhecida
já no início, ver `TestRunConfig`/`_total_monitored_duration_s`) em
`bin_count` fatias fixas e mantendo a média corrente de cada uma. A memória
é O(bin_count), constante do início ao fim do ensaio, e a curva inteira
permanece visível durante todo o teste -- sem nunca "apagar" nada.

Se o ensaio real ultrapassar a duração prevista (retries, atrasos), as
amostras excedentes caem no último bin, que passa a representar uma fatia
de tempo maior -- ainda sem perder nenhuma amostra do agregado, só fica
mais "grosseiro" no final.
"""
from __future__ import annotations

from core.sampling_buffer import Sample


class LiveChartHistory:
    def __init__(self, duration_s: float, bin_count: int = 500) -> None:
        self._duration_s = max(duration_s, 1e-9)
        self._bin_count = max(1, bin_count)
        self._sum_voltage = [0.0] * self._bin_count
        self._sum_current = [0.0] * self._bin_count
        self._count = [0] * self._bin_count
        self._last_step_index = [0] * self._bin_count
        self._last_timestamp = [0.0] * self._bin_count

    def add_sample(self, sample: Sample) -> None:
        index = int(sample.timestamp / self._duration_s * self._bin_count)
        index = min(max(index, 0), self._bin_count - 1)
        self._sum_voltage[index] += sample.voltage
        self._sum_current[index] += sample.current
        self._count[index] += 1
        self._last_step_index[index] = sample.step_index
        self._last_timestamp[index] = sample.timestamp

    def snapshot(self) -> list[Sample]:
        """Um `Sample` (média) por bin já preenchido, em ordem cronológica."""
        return [
            Sample(
                timestamp=self._last_timestamp[i],
                step_index=self._last_step_index[i],
                voltage=self._sum_voltage[i] / self._count[i],
                current=self._sum_current[i] / self._count[i],
            )
            for i in range(self._bin_count)
            if self._count[i] > 0
        ]
