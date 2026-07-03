"""Testes de persistência: schema, repositories e batch insert de amostras."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from database.database import Database
from database.models import (
    Evaluation,
    EvaluationResult,
    EventLogEntry,
    MonitoredSample,
    PowerStep,
    TestParameterConfig,
    TestSession,
    TestSessionStatus,
)
from database.repositories import (
    BoardRepository,
    EvaluationRepository,
    EventLogRepository,
    MonitoredSampleRepository,
    OperatorRepository,
    RecordInUseError,
    TestParameterConfigRepository,
    TestSessionRepository,
)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "fct_test.db")
    database.connect()
    yield database
    database.close()


def test_operator_get_or_create_is_idempotent(db: Database) -> None:
    repo = OperatorRepository(db)
    first = repo.get_or_create("Willian Lima")
    second = repo.get_or_create("Willian Lima")
    assert first.id == second.id
    assert len(repo.list_all()) == 1


def test_operator_delete_removes_an_unused_entry(db: Database) -> None:
    """Cadastro duplicado/errado, sem nenhum ensaio vinculado -- deve poder
    ser removido do histórico normalmente."""
    repo = OperatorRepository(db)
    operator = repo.get_or_create("Duplicado")

    repo.delete(operator.id)

    assert repo.list_all() == []


def test_operator_delete_cascades_to_test_session_and_dependents(db: Database) -> None:
    """Decisão explícita do usuário: "o operador e o seu ensaio serão
    excluídos, não bloqueie a exclusão por causa do ensaio gravado".
    Apagar um operador com ensaio vinculado apaga o ensaio e tudo que
    depende dele (amostras monitoradas, avaliação, log de eventos)."""
    operator_repo = OperatorRepository(db)
    operator = operator_repo.get_or_create("Com ensaio")
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    session = TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-0001",
            operator_id=operator.id,
            test_parameter_config_id=None,
            config_snapshot_json=json.dumps({}),
            production_order=None,
            observations=None,
            status=TestSessionStatus.COMPLETED,
        )
    )
    MonitoredSampleRepository(db).insert_batch(
        [
            MonitoredSample(
                id=None, test_session_id=session.id, timestamp="2026-01-01T00:00:00",
                step_index=0, voltage_measured=5.0, current_measured=1.0,
            )
        ]
    )
    EvaluationRepository(db).create(
        Evaluation(
            id=None, test_session_id=session.id, operator_id=operator.id,
            result=EvaluationResult.APPROVED, comment=None,
        )
    )
    EventLogRepository(db).add(
        EventLogEntry(
            id=None, test_session_id=session.id, timestamp="2026-01-01T00:00:00",
            level="INFO", source="test", message="msg",
        )
    )

    assert operator_repo.count_test_sessions(operator.id) == 1

    operator_repo.delete(operator.id)

    with pytest.raises(LookupError):
        operator_repo.get(operator.id)
    with pytest.raises(LookupError):
        TestSessionRepository(db).get(session.id)
    assert MonitoredSampleRepository(db).list_for_session(session.id) == []
    assert EvaluationRepository(db).get_for_session(session.id) is None
    assert EventLogRepository(db).list_for_session(session.id) == []


def test_operator_delete_cascades_evaluations_where_operator_was_only_the_evaluator(
    db: Database,
) -> None:
    """evaluations.operator_id (quem avaliou) é uma referência independente
    de test_sessions.operator_id (quem rodou o teste) -- pode ser uma
    pessoa diferente. Apagar o operador precisa limpar essa referência
    também, mesmo quando o ensaio em si pertence a outro operador."""
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    runner = OperatorRepository(db).get_or_create("Quem rodou")
    evaluator = OperatorRepository(db).get_or_create("Quem avaliou")
    session = TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-0002",
            operator_id=runner.id,
            test_parameter_config_id=None,
            config_snapshot_json=json.dumps({}),
            production_order=None,
            observations=None,
            status=TestSessionStatus.COMPLETED,
        )
    )
    EvaluationRepository(db).create(
        Evaluation(
            id=None, test_session_id=session.id, operator_id=evaluator.id,
            result=EvaluationResult.APPROVED, comment=None,
        )
    )

    OperatorRepository(db).delete(evaluator.id)

    assert TestSessionRepository(db).get(session.id) is not None  # ensaio do runner intacto
    assert EvaluationRepository(db).get_for_session(session.id) is None  # avaliação some


def test_board_get_or_create_distinguishes_revisions(db: Database) -> None:
    repo = BoardRepository(db)
    rev_a = repo.get_or_create("PCB-001", "PN-123", "RevA")
    rev_b = repo.get_or_create("PCB-001", "PN-123", "RevB")
    assert rev_a.id != rev_b.id


def test_test_parameter_config_roundtrips_power_sequence(db: Database) -> None:
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    repo = TestParameterConfigRepository(db)
    created = repo.save(
        TestParameterConfig(
            id=None,
            board_id=board.id,
            name="Padrao 12V",
            nominal_voltage=12.0,
            voltage_min=11.5,
            voltage_max=12.5,
            current_max=2.0,
            test_duration_s=60.0,
            power_sequence=[PowerStep(5.0, 1.0, 10.0), PowerStep(12.0, 2.0, 50.0)],
        )
    )
    fetched = repo.get(created.id)
    assert fetched.power_sequence == [PowerStep(5.0, 1.0, 10.0), PowerStep(12.0, 2.0, 50.0)]
    assert repo.list_for_board(board.id) == [fetched]
    assert fetched.range_mode is None  # não informado -- default é seleção automática


def test_test_parameter_config_roundtrips_forced_range_mode(db: Database) -> None:
    """O operador pode travar a faixa V/A do preset (ver TestParametersView) --
    precisa sobreviver a salvar/recarregar, senão a escolha se perde a cada sessão."""
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    repo = TestParameterConfigRepository(db)
    created = repo.save(
        TestParameterConfig(
            id=None,
            board_id=board.id,
            name="Fixo em HIGH",
            nominal_voltage=30.0,
            voltage_min=29.5,
            voltage_max=30.5,
            current_max=2.0,
            test_duration_s=60.0,
            power_sequence=[],
            range_mode="HIGH",
        )
    )
    fetched = repo.get(created.id)
    assert fetched.range_mode == "HIGH"


def test_test_parameter_config_roundtrips_off_duration_per_step(db: Database) -> None:
    """Tempo OFF entre ciclos é por PASSO (PowerStep.off_duration_s), guardado
    dentro do JSON da sequência -- não precisa de coluna/migração própria,
    mas precisa sobreviver ao roundtrip como qualquer outro campo do passo."""
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    repo = TestParameterConfigRepository(db)
    created = repo.save(
        TestParameterConfig(
            id=None,
            board_id=board.id,
            name="Ciclo térmico",
            nominal_voltage=12.0,
            voltage_min=11.5,
            voltage_max=12.5,
            current_max=2.0,
            test_duration_s=60.0,
            power_sequence=[
                PowerStep(5.0, 1.0, 10.0, off_duration_s=30.0),
                PowerStep(12.0, 2.0, 50.0),  # sem off_duration_s explícito -- default 0.0
            ],
        )
    )
    fetched = repo.get(created.id)
    assert fetched.power_sequence == [
        PowerStep(5.0, 1.0, 10.0, off_duration_s=30.0),
        PowerStep(12.0, 2.0, 50.0, off_duration_s=0.0),
    ]


def test_test_parameter_config_delete_removes_an_unused_entry(db: Database) -> None:
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    repo = TestParameterConfigRepository(db)
    config = repo.save(
        TestParameterConfig(
            id=None,
            board_id=board.id,
            name="Preset por engano",
            nominal_voltage=12.0,
            voltage_min=11.5,
            voltage_max=12.5,
            current_max=2.0,
            test_duration_s=60.0,
            power_sequence=[],
        )
    )

    repo.delete(config.id)

    assert repo.list_for_board(board.id) == []


def test_test_parameter_config_delete_is_blocked_when_used_by_a_test_session(db: Database) -> None:
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    operator = OperatorRepository(db).get_or_create("Op")
    config_repo = TestParameterConfigRepository(db)
    config = config_repo.save(
        TestParameterConfig(
            id=None,
            board_id=board.id,
            name="Usado de verdade",
            nominal_voltage=12.0,
            voltage_min=11.5,
            voltage_max=12.5,
            current_max=2.0,
            test_duration_s=60.0,
            power_sequence=[],
        )
    )
    TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-0001",
            operator_id=operator.id,
            test_parameter_config_id=config.id,
            config_snapshot_json=json.dumps({}),
            production_order=None,
            observations=None,
            status=TestSessionStatus.COMPLETED,
        )
    )

    with pytest.raises(RecordInUseError):
        config_repo.delete(config.id)

    assert config_repo.get(config.id) is not None  # não foi excluído


def test_test_parameter_config_save_overwrites_same_name_instead_of_duplicating(
    db: Database,
) -> None:
    """Salvar de novo com o mesmo nome deve atualizar, não duplicar (semântica Word)."""
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    repo = TestParameterConfigRepository(db)
    first = repo.save(
        TestParameterConfig(
            id=None,
            board_id=board.id,
            name="Padrao 12V",
            nominal_voltage=12.0,
            voltage_min=11.5,
            voltage_max=12.5,
            current_max=2.0,
            test_duration_s=60.0,
            power_sequence=[],
        )
    )
    second = repo.save(
        TestParameterConfig(
            id=None,
            board_id=board.id,
            name="Padrao 12V",
            nominal_voltage=13.0,
            voltage_min=12.5,
            voltage_max=13.5,
            current_max=3.0,
            test_duration_s=90.0,
            power_sequence=[PowerStep(13.0, 3.0, 90.0)],
        )
    )
    assert second.id == first.id
    assert second.nominal_voltage == 13.0
    assert repo.list_for_board(board.id) == [second]


def test_monitored_sample_batch_insert_and_list(db: Database) -> None:
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    operator = OperatorRepository(db).get_or_create("Willian Lima")
    session = TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-0001",
            operator_id=operator.id,
            test_parameter_config_id=None,
            config_snapshot_json=json.dumps({}),
            production_order=None,
            observations=None,
            status=TestSessionStatus.RUNNING,
        )
    )
    repo = MonitoredSampleRepository(db)
    repo.insert_batch(
        [
            MonitoredSample(None, session.id, "t1", 0, 12.01, 1.0),
            MonitoredSample(None, session.id, "t2", 0, 12.02, 1.01),
        ]
    )
    samples = repo.list_for_session(session.id)
    assert len(samples) == 2
    assert [s.voltage_measured for s in samples] == [12.01, 12.02]


def test_evaluation_is_unique_per_session(db: Database) -> None:
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    operator = OperatorRepository(db).get_or_create("Willian Lima")
    session = TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-0001",
            operator_id=operator.id,
            test_parameter_config_id=None,
            config_snapshot_json=json.dumps({}),
            production_order=None,
            observations=None,
            status=TestSessionStatus.COMPLETED,
        )
    )
    repo = EvaluationRepository(db)
    assert repo.get_for_session(session.id) is None
    created = repo.create(
        Evaluation(None, session.id, operator.id, EvaluationResult.APPROVED, "OK")
    )
    assert repo.get_for_session(session.id) == created


def test_event_log_records_failures_tied_to_session(db: Database) -> None:
    board = BoardRepository(db).get_or_create("PCB-001", "PN-123", "RevA")
    operator = OperatorRepository(db).get_or_create("Willian Lima")
    session = TestSessionRepository(db).create(
        TestSession(
            id=None,
            board_id=board.id,
            serial_number="SN-0001",
            operator_id=operator.id,
            test_parameter_config_id=None,
            config_snapshot_json=json.dumps({}),
            production_order=None,
            observations=None,
            status=TestSessionStatus.FAULTED,
        )
    )
    repo = EventLogRepository(db)
    repo.add(EventLogEntry(None, session.id, None, "ERROR", "state_machine", "Falha de comunicação"))
    entries = repo.list_for_session(session.id)
    assert len(entries) == 1
    assert entries[0].message == "Falha de comunicação"
