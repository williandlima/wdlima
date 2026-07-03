"""Repository pattern: única camada que conhece SQL (seção 3.4).

Cada classe isola o acesso a uma tabela/agregado. Trocar SQLite por outro
banco no futuro significa reescrever só este arquivo — o resto do app
trabalha com os dataclasses de `models.py`.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from database.database import Database
from database.models import (
    Board,
    Evaluation,
    EvaluationResult,
    EventLogEntry,
    MonitoredSample,
    Operator,
    PowerStep,
    TestParameterConfig,
    TestSession,
    TestSessionStatus,
)


class RecordInUseError(Exception):
    """Excluir um registro referenciado por outro (ex.: config de parâmetros
    com ensaios reais vinculados) -- `PRAGMA foreign_keys=ON` (ver database.py)
    já impede a exclusão a nível de banco; aqui só se traduz o
    `sqlite3.IntegrityError` bruto numa mensagem acionável, sem vazar
    detalhe de SQL pra camada de GUI. OperatorRepository.delete() NÃO usa
    mais este bloqueio de propósito (decisão do usuário) -- ele apaga o
    operador e o histórico dele em cascata; a exceção fica só como fallback
    defensivo para qualquer FK inesperada."""


class OperatorRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_or_create(self, name: str, if_number: str | None = None) -> Operator:
        conn = self._db.connection
        row = conn.execute("SELECT * FROM operators WHERE name = ?", (name,)).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO operators (name, if_number) VALUES (?, ?)", (name, if_number)
            )
            conn.commit()
            row = conn.execute("SELECT * FROM operators WHERE id = ?", (cursor.lastrowid,)).fetchone()
        elif if_number is not None and if_number != row["if_number"]:
            conn.execute("UPDATE operators SET if_number = ? WHERE id = ?", (if_number, row["id"]))
            conn.commit()
            row = conn.execute("SELECT * FROM operators WHERE id = ?", (row["id"],)).fetchone()
        return self._to_model(row)

    def list_all(self) -> list[Operator]:
        rows = self._db.connection.execute("SELECT * FROM operators ORDER BY name").fetchall()
        return [self._to_model(r) for r in rows]

    def get(self, operator_id: int) -> Operator:
        row = self._db.connection.execute(
            "SELECT * FROM operators WHERE id = ?", (operator_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Operator {operator_id} não encontrado.")
        return self._to_model(row)

    def count_test_sessions(self, operator_id: int) -> int:
        """Quantos ensaios ficam vinculados a este operador (como quem rodou
        o teste) -- usado pela GUI para avisar a quantidade antes de excluir,
        já que `delete()` agora apaga tudo isso em cascata."""
        row = self._db.connection.execute(
            "SELECT COUNT(*) AS n FROM test_sessions WHERE operator_id = ?", (operator_id,)
        ).fetchone()
        return row["n"]

    def delete(self, operator_id: int) -> None:
        """Remove o operador e TODO o histórico vinculado a ele: os ensaios
        que ele rodou (e tudo que depende de cada um -- amostras monitoradas,
        avaliação, log de eventos, via ON DELETE CASCADE) e as avaliações em
        que ele foi o avaliador de um ensaio de outra pessoa. Decisão
        explícita do usuário: "o operador e o seu ensaio serão excluídos,
        não bloqueie a exclusão por causa do ensaio gravado" -- a GUI mostra
        a quantidade de ensaios afetados (via count_test_sessions) antes de
        chamar isto, e pede senha de confirmação. Não há mais volta atrás
        depois de chamado."""
        conn = self._db.connection
        try:
            # evaluations.operator_id (avaliador) não tem ON DELETE CASCADE
            # e é uma referência independente de test_sessions.operator_id --
            # precisa ser limpa à parte antes de apagar o operador.
            conn.execute("DELETE FROM evaluations WHERE operator_id = ?", (operator_id,))
            conn.execute("DELETE FROM test_sessions WHERE operator_id = ?", (operator_id,))
            conn.execute("DELETE FROM operators WHERE id = ?", (operator_id,))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise RecordInUseError(
                "Não foi possível excluir este operador (registro ainda referenciado)."
            ) from exc

    @staticmethod
    def _to_model(row: sqlite3.Row) -> Operator:
        return Operator(
            id=row["id"], name=row["name"], if_number=row["if_number"], created_at=row["created_at"]
        )


class BoardRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_or_create(self, code: str, part_number: str, revision: str) -> Board:
        conn = self._db.connection
        row = conn.execute(
            "SELECT * FROM boards WHERE code = ? AND part_number = ? AND revision = ?",
            (code, part_number, revision),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO boards (code, part_number, revision) VALUES (?, ?, ?)",
                (code, part_number, revision),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM boards WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._to_model(row)

    def list_all(self) -> list[Board]:
        rows = self._db.connection.execute("SELECT * FROM boards ORDER BY code").fetchall()
        return [self._to_model(r) for r in rows]

    def get(self, board_id: int) -> Board:
        row = self._db.connection.execute(
            "SELECT * FROM boards WHERE id = ?", (board_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"Board {board_id} não encontrado.")
        return self._to_model(row)

    @staticmethod
    def _to_model(row: sqlite3.Row) -> Board:
        return Board(
            id=row["id"],
            code=row["code"],
            part_number=row["part_number"],
            revision=row["revision"],
            created_at=row["created_at"],
        )


class TestParameterConfigRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def save(self, config: TestParameterConfig) -> TestParameterConfig:
        """Grava a configuração como um único registro por (board_id, name).

        Salvar de novo com o mesmo nome atualiza o registro existente em vez
        de duplicá-lo — a mesma semântica de "Ctrl+S" do Word sobre o mesmo
        arquivo. O `INSERT ... ON CONFLICT` é atômico: não há janela entre
        checar existência e gravar.
        """
        conn = self._db.connection
        sequence_json = json.dumps([asdict(step) for step in config.power_sequence])
        conn.execute(
            """
            INSERT INTO test_parameter_configs
                (board_id, name, nominal_voltage, voltage_min, voltage_max,
                 current_max, test_duration_s, power_sequence_json, range_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (board_id, name) DO UPDATE SET
                nominal_voltage = excluded.nominal_voltage,
                voltage_min = excluded.voltage_min,
                voltage_max = excluded.voltage_max,
                current_max = excluded.current_max,
                test_duration_s = excluded.test_duration_s,
                power_sequence_json = excluded.power_sequence_json,
                range_mode = excluded.range_mode
            """,
            (
                config.board_id,
                config.name,
                config.nominal_voltage,
                config.voltage_min,
                config.voltage_max,
                config.current_max,
                config.test_duration_s,
                sequence_json,
                config.range_mode,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM test_parameter_configs WHERE board_id IS ? AND name = ?",
            (config.board_id, config.name),
        ).fetchone()
        return self._to_model(row)

    def get(self, config_id: int) -> TestParameterConfig:
        row = self._db.connection.execute(
            "SELECT * FROM test_parameter_configs WHERE id = ?", (config_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"TestParameterConfig {config_id} não encontrado.")
        return self._to_model(row)

    def delete(self, config_id: int) -> None:
        """Remove a configuração salva do histórico -- ex.: preset de teste
        criado por engano. Bloqueado pelo próprio banco (RecordInUseError)
        se algum ensaio real já tiver usado essa configuração."""
        try:
            self._db.connection.execute(
                "DELETE FROM test_parameter_configs WHERE id = ?", (config_id,)
            )
            self._db.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise RecordInUseError(
                "Esta configuração já foi usada em algum ensaio e não pode ser excluída."
            ) from exc

    def list_for_board(self, board_id: int) -> list[TestParameterConfig]:
        rows = self._db.connection.execute(
            "SELECT * FROM test_parameter_configs WHERE board_id = ? ORDER BY created_at DESC",
            (board_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> TestParameterConfig:
        steps = [PowerStep(**step) for step in json.loads(row["power_sequence_json"])]
        return TestParameterConfig(
            id=row["id"],
            board_id=row["board_id"],
            name=row["name"],
            nominal_voltage=row["nominal_voltage"],
            voltage_min=row["voltage_min"],
            voltage_max=row["voltage_max"],
            current_max=row["current_max"],
            test_duration_s=row["test_duration_s"],
            power_sequence=steps,
            range_mode=row["range_mode"],
            created_at=row["created_at"],
        )


class TestSessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, session: TestSession) -> TestSession:
        conn = self._db.connection
        cursor = conn.execute(
            """
            INSERT INTO test_sessions
                (board_id, serial_number, operator_id, test_parameter_config_id,
                 config_snapshot_json, production_order, observations, status, started_at,
                 instrument_identity, app_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.board_id,
                session.serial_number,
                session.operator_id,
                session.test_parameter_config_id,
                session.config_snapshot_json,
                session.production_order,
                session.observations,
                session.status.value,
                session.started_at,
                session.instrument_identity,
                session.app_version,
            ),
        )
        conn.commit()
        return self.get(cursor.lastrowid)

    def update_status(
        self, session_id: int, status: TestSessionStatus, finished_at: str | None = None
    ) -> None:
        conn = self._db.connection
        conn.execute(
            "UPDATE test_sessions SET status = ?, finished_at = COALESCE(?, finished_at) WHERE id = ?",
            (status.value, finished_at, session_id),
        )
        conn.commit()

    def set_instrument_identity(self, session_id: int, identity: str | None) -> None:
        """Grava a string *IDN? da fonte usada no ensaio (rastreabilidade)."""
        if not identity:
            return
        conn = self._db.connection
        conn.execute(
            "UPDATE test_sessions SET instrument_identity = ? WHERE id = ?",
            (identity, session_id),
        )
        conn.commit()

    def get(self, session_id: int) -> TestSession:
        row = self._db.connection.execute(
            "SELECT * FROM test_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"TestSession {session_id} não encontrado.")
        return self._to_model(row)

    def list_recent(self, limit: int = 50) -> list[TestSession]:
        rows = self._db.connection.execute(
            "SELECT * FROM test_sessions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._to_model(r) for r in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> TestSession:
        return TestSession(
            id=row["id"],
            board_id=row["board_id"],
            serial_number=row["serial_number"],
            operator_id=row["operator_id"],
            test_parameter_config_id=row["test_parameter_config_id"],
            config_snapshot_json=row["config_snapshot_json"],
            production_order=row["production_order"],
            observations=row["observations"],
            status=TestSessionStatus(row["status"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            instrument_identity=row["instrument_identity"],
            app_version=row["app_version"],
            created_at=row["created_at"],
        )


class MonitoredSampleRepository:
    """Único repository chamado em alta frequência: sempre em lote (seção 10)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def insert_batch(self, samples: list[MonitoredSample]) -> None:
        rows = [
            (s.test_session_id, s.timestamp, s.step_index, s.voltage_measured, s.current_measured)
            for s in samples
        ]
        self._db.executemany_batch(
            """
            INSERT INTO monitored_samples
                (test_session_id, timestamp, step_index, voltage_measured, current_measured)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )

    def list_for_session(self, test_session_id: int) -> list[MonitoredSample]:
        rows = self._db.connection.execute(
            "SELECT * FROM monitored_samples WHERE test_session_id = ? ORDER BY id",
            (test_session_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> MonitoredSample:
        return MonitoredSample(
            id=row["id"],
            test_session_id=row["test_session_id"],
            timestamp=row["timestamp"],
            step_index=row["step_index"],
            voltage_measured=row["voltage_measured"],
            current_measured=row["current_measured"],
        )


class EvaluationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, evaluation: Evaluation) -> Evaluation:
        conn = self._db.connection
        cursor = conn.execute(
            """
            INSERT INTO evaluations (test_session_id, operator_id, result, comment)
            VALUES (?, ?, ?, ?)
            """,
            (
                evaluation.test_session_id,
                evaluation.operator_id,
                evaluation.result.value,
                evaluation.comment,
            ),
        )
        conn.commit()
        return self.get_for_session(evaluation.test_session_id)

    def get_for_session(self, test_session_id: int) -> Evaluation | None:
        row = self._db.connection.execute(
            "SELECT * FROM evaluations WHERE test_session_id = ?", (test_session_id,)
        ).fetchone()
        return self._to_model(row) if row is not None else None

    @staticmethod
    def _to_model(row: sqlite3.Row) -> Evaluation:
        return Evaluation(
            id=row["id"],
            test_session_id=row["test_session_id"],
            operator_id=row["operator_id"],
            result=EvaluationResult(row["result"]),
            comment=row["comment"],
            evaluated_at=row["evaluated_at"],
        )


class EventLogRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, entry: EventLogEntry) -> None:
        conn = self._db.connection
        conn.execute(
            "INSERT INTO event_log (test_session_id, level, source, message) VALUES (?, ?, ?, ?)",
            (entry.test_session_id, entry.level, entry.source, entry.message),
        )
        conn.commit()

    def list_for_session(self, test_session_id: int) -> list[EventLogEntry]:
        rows = self._db.connection.execute(
            "SELECT * FROM event_log WHERE test_session_id = ? ORDER BY id",
            (test_session_id,),
        ).fetchall()
        return [self._to_model(r) for r in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row) -> EventLogEntry:
        return EventLogEntry(
            id=row["id"],
            test_session_id=row["test_session_id"],
            timestamp=row["timestamp"],
            level=row["level"],
            source=row["source"],
            message=row["message"],
        )
