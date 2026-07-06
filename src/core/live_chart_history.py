"""Histórico do gráfico ao vivo, com memória limitada pelo NÚMERO DE BINS
(seção 3.3) e decimação min/máx (envelope) que PRESERVA PICOS.

Antes, o gráfico ao vivo era alimentado por um `deque(maxlen=N)` de amostras
brutas: assim que o ensaio passava da janela coberta por N amostras (em
testes longos), as amostras mais antigas eram descartadas -- a curva
"apagava" o começo.

Este histórico divide a duração total do ensaio em `bin_count` fatias fixas.
A memória é O(bin_count), constante do início ao fim do ensaio.

PICOS: cada bin NÃO guarda a média das amostras, e sim o MÍNIMO e o MÁXIMO
de tensão e corrente (envelope min/máx). Em testes longos, cada bin cobre
uma fatia de tempo larga (ex.: 1 h / 500 bins = ~7 s por bin, dezenas de
amostras cada); a média "engolia" um pico rápido de corrente dentro do bin,
e o ponto plotado ficava bem abaixo do pico real -- o valor de pico no
gráfico não batia com o do visor (que mostra o instantâneo). Guardando
min/máx, o bin emite dois pontos (o vale e o pico), então a corrente de
pico chega ao gráfico no MESMO valor observado no visor. É a mesma
decimação por envelope usada em osciloscópios/editores de onda.

Se o ensaio real ultrapassar a duração prevista (retries, atrasos), as
amostras excedentes caem no último bin -- ainda sem perder o pico.
"""
from __future__ import annotations

import math

from core.sampling_buffer import Sample


class LiveChartHistory:
    def __init__(self, duration_s: float, bin_count: int = 500) -> None:
        self._duration_s = max(duration_s, 1e-9)
        self._bin_count = max(1, bin_count)
        # Envelope por bin: mínimo e máximo de cada grandeza, não a média --
        # a média achatava picos rápidos em testes longos (ver docstring).
        self._v_min = [math.inf] * self._bin_count
        self._v_max = [-math.inf] * self._bin_count
        self._i_min = [math.inf] * self._bin_count
        self._i_max = [-math.inf] * self._bin_count
        self._count = [0] * self._bin_count
        self._last_step_index = [0] * self._bin_count
        # t_lo/t_hi: instantes da primeira e última amostra do bin, usados
        # como x do ponto de vale e do ponto de pico do envelope.
        self._t_lo = [0.0] * self._bin_count
        self._t_hi = [0.0] * self._bin_count
        # As amostras carregam timestamp ABSOLUTO (time.time(), ver
        # TestStateMachine), não o tempo decorrido do ensaio. O bin precisa
        # do tempo relativo ao início -- sem normalizar por este t0, todas as
        # amostras caíam no mesmo bin (índice estourava e era cortado no
        # último), e o gráfico virava um único ponto. t0 é o instante da
        # primeira amostra recebida.
        self._t0: float | None = None

    def add_sample(self, sample: Sample) -> None:
        if self._t0 is None:
            self._t0 = sample.timestamp
        elapsed = sample.timestamp - self._t0
        index = int(elapsed / self._duration_s * self._bin_count)
        index = min(max(index, 0), self._bin_count - 1)
        if self._count[index] == 0:
            self._t_lo[index] = sample.timestamp
        self._v_min[index] = min(self._v_min[index], sample.voltage)
        self._v_max[index] = max(self._v_max[index], sample.voltage)
        self._i_min[index] = min(self._i_min[index], sample.current)
        self._i_max[index] = max(self._i_max[index], sample.current)
        self._t_hi[index] = sample.timestamp
        self._last_step_index[index] = sample.step_index
        self._count[index] += 1

    def snapshot(self) -> list[Sample]:
        """Envelope min/máx por bin preenchido, em ordem cronológica.

        Um bin com um só valor (ou grandezas constantes) vira um único ponto;
        um bin com variação vira DOIS pontos (vale e pico), para a curva
        cobrir toda a amplitude -- é isso que faz o pico de corrente aparecer
        no gráfico no mesmo valor do visor. Os timestamps continuam ABSOLUTOS
        (como recebidos); o LiveChart normaliza por conta própria.
        """
        result: list[Sample] = []
        for i in range(self._bin_count):
            if self._count[i] == 0:
                continue
            step = self._last_step_index[i]
            if self._v_min[i] == self._v_max[i] and self._i_min[i] == self._i_max[i]:
                result.append(
                    Sample(
                        timestamp=self._t_hi[i],
                        step_index=step,
                        voltage=self._v_max[i],
                        current=self._i_max[i],
                    )
                )
            else:
                # Vale primeiro, pico depois (ordenados por t_lo <= t_hi): a
                # linha sobe até o pico dentro do bin, cobrindo a amplitude.
                result.append(
                    Sample(
                        timestamp=self._t_lo[i],
                        step_index=step,
                        voltage=self._v_min[i],
                        current=self._i_min[i],
                    )
                )
                result.append(
                    Sample(
                        timestamp=self._t_hi[i],
                        step_index=step,
                        voltage=self._v_max[i],
                        current=self._i_max[i],
                    )
                )
        return result
