"""Testes do histórico do gráfico ao vivo (core/live_chart_history.py) --
memória limitada por NÚMERO DE BINS, não por um FIFO de amostras que
"apagava" o início da curva em ensaios mais longos que a janela do buffer
(ver test_gui_smoke.py::test_live_chart_history_keeps_full_duration_visible_in_long_tests
para a regressão original)."""
from __future__ import annotations

import pytest

from core.live_chart_history import LiveChartHistory
from core.sampling_buffer import Sample


def test_snapshot_skips_empty_bins() -> None:
    history = LiveChartHistory(duration_s=10.0, bin_count=10)
    history.add_sample(Sample(timestamp=0.5, step_index=0, voltage=5.0, current=1.0))
    history.add_sample(Sample(timestamp=9.5, step_index=0, voltage=6.0, current=2.0))

    snapshot = history.snapshot()
    assert len(snapshot) == 2  # só os 2 bins com amostra aparecem, não os 10


def test_bin_averages_multiple_samples() -> None:
    history = LiveChartHistory(duration_s=10.0, bin_count=1)
    history.add_sample(Sample(timestamp=1.0, step_index=0, voltage=4.0, current=1.0))
    history.add_sample(Sample(timestamp=2.0, step_index=0, voltage=6.0, current=3.0))

    snapshot = history.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].voltage == pytest.approx(5.0)
    assert snapshot[0].current == pytest.approx(2.0)


def test_samples_beyond_duration_clamp_into_last_bin_without_crashing() -> None:
    """Ensaio real mais longo que a duração prevista (retries/atrasos) não
    pode derrubar o app -- a amostra excedente cai no último bin."""
    history = LiveChartHistory(duration_s=10.0, bin_count=5)
    history.add_sample(Sample(timestamp=999.0, step_index=0, voltage=5.0, current=1.0))

    snapshot = history.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0].voltage == pytest.approx(5.0)


def test_memory_bounded_by_bin_count_not_sample_count() -> None:
    history = LiveChartHistory(duration_s=3600.0, bin_count=50)
    for i in range(50_000):
        history.add_sample(Sample(timestamp=i * 0.05, step_index=0, voltage=5.0, current=0.5))

    assert len(history.snapshot()) <= 50


def test_first_instant_of_test_stays_visible_no_matter_how_long_it_runs() -> None:
    """A regressão relatada: o início da curva não pode "apagar" conforme o
    ensaio avança, mesmo rodando por muito mais tempo do que os bins
    conseguiriam representar amostra a amostra."""
    history = LiveChartHistory(duration_s=100.0, bin_count=20)
    for i in range(10_000):
        history.add_sample(Sample(timestamp=i * 0.01, step_index=0, voltage=5.0, current=0.5))

    snapshot = history.snapshot()
    assert snapshot[0].timestamp < 5.0  # o primeiro bin (início do ensaio) segue presente
