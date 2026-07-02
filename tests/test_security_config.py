"""Testa o carregamento de security.operator_delete_password -- trava simples
contra clique acidental ao excluir um operador (ver gui/registration_view.py:
_on_delete_operator). Não é autenticação de verdade, só uma segunda trava
além do bloqueio de FK do banco (RecordInUseError)."""
from __future__ import annotations

from pathlib import Path

import yaml

from config import load_config


def test_shipped_config_resolves_operator_delete_password() -> None:
    app_config = load_config(create_dirs=False)

    assert app_config.security.operator_delete_password == "lab1"


def test_operator_delete_password_falls_back_to_default_when_not_configured(
    tmp_path: Path,
) -> None:
    """Sem a seção `security` no YAML, cai para o default "lab1" -- não
    quebra configs antigos gerados antes desta opção existir."""
    shipped = Path(__file__).resolve().parent.parent / "config" / "app_config.yaml"
    raw = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    del raw["security"]
    custom_config = tmp_path / "app_config.yaml"
    custom_config.write_text(yaml.dump(raw), encoding="utf-8")

    app_config = load_config(config_path=custom_config, create_dirs=False)

    assert app_config.security.operator_delete_password == "lab1"


def test_operator_delete_password_is_configurable(tmp_path: Path) -> None:
    shipped = Path(__file__).resolve().parent.parent / "config" / "app_config.yaml"
    raw = yaml.safe_load(shipped.read_text(encoding="utf-8"))
    raw["security"]["operator_delete_password"] = "outra-senha"
    custom_config = tmp_path / "app_config.yaml"
    custom_config.write_text(yaml.dump(raw), encoding="utf-8")

    app_config = load_config(config_path=custom_config, create_dirs=False)

    assert app_config.security.operator_delete_password == "outra-senha"
