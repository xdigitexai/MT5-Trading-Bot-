"""The migration chain must build exactly the schema the models describe.

No database server is required: the migrations are executed against a throwaway SQLite file.
"""
import importlib.util
from pathlib import Path

import sqlalchemy as sa

from app.core.config import Settings
from app.database.base import ExecutionGuardRecord, NewsEventRecord, NewsProviderStateRecord, OneTradeAuthorizationRecord, RiskStateRecord, SignalRecord, TradeRecord

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "migrations" / "versions"
NEW_TABLES = (SignalRecord, RiskStateRecord, ExecutionGuardRecord, OneTradeAuthorizationRecord, NewsEventRecord, NewsProviderStateRecord)
NEW_TRADE_COLUMNS = {"exit_price", "commission", "swap", "profit", "open_time", "close_time", "mt5_deal_ticket", "volume_closed", "reconciled_at", "close_reason"}


def load_migration(name: str):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alembic_config():
    from alembic.config import Config
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def test_revision_chain_is_linear():
    initial = load_migration("0001_initial")
    second = load_migration("0002_signals_risk_execution")
    assert initial.revision == "0001_initial" and initial.down_revision is None
    assert second.revision == "0002_signals_risk_execution"
    assert second.down_revision == "0001_initial"
    assert callable(second.upgrade) and callable(second.downgrade)


def test_the_dry_run_expects_the_head_of_the_migration_chain():
    """The dry run reports a stale schema by name, so its expectation must track the chain."""
    from app import dry_run

    revisions = {path.stem: load_migration(path.stem) for path in VERSIONS.glob("0*.py")}
    assert dry_run.EXPECTED_ALEMBIC_REVISION in revisions
    parents = {module.down_revision for module in revisions.values() if module.down_revision}
    heads = sorted(set(revisions) - parents)

    assert heads == [dry_run.EXPECTED_ALEMBIC_REVISION]


def unique_columns(inspector, table: str) -> set[str]:
    names = {column for index in inspector.get_indexes(table) if index.get("unique") for column in index["column_names"]}
    names |= {column for constraint in inspector.get_unique_constraints(table) for column in constraint["column_names"]}
    names |= set(inspector.get_pk_constraint(table)["constrained_columns"] or [])
    return names


def test_upgrade_and_downgrade_agree_with_the_models(tmp_path, monkeypatch):
    from alembic import command
    url = f"sqlite:///{(tmp_path / 'migration.db').as_posix()}"
    monkeypatch.setattr("app.core.config.get_settings", lambda: Settings(database_url=url))
    config = alembic_config()
    command.upgrade(config, "head")

    inspector = sa.inspect(sa.create_engine(url))
    tables = set(inspector.get_table_names())
    for model in NEW_TABLES:
        assert model.__tablename__ in tables
        assert set(model.__table__.columns.keys()) == {column["name"] for column in inspector.get_columns(model.__tablename__)}
        declared = {column.name for column in model.__table__.columns if column.unique or column.primary_key}
        assert declared == unique_columns(inspector, model.__tablename__)
    migrated = {column["name"] for column in inspector.get_columns("trades")}
    assert set(TradeRecord.__table__.columns.keys()) == migrated
    assert NEW_TRADE_COLUMNS <= migrated
    assert {"ix_signals_symbol", "ix_signals_strategy", "ix_signals_status", "ix_signals_created_at", "ix_trades_mt5_deal_ticket", "ix_risk_state_day"} <= {index["name"] for table in ("signals", "risk_state", "trades") for index in inspector.get_indexes(table)}

    command.downgrade(config, "0001_initial")
    inspector = sa.inspect(sa.create_engine(url))
    assert not ({model.__tablename__ for model in NEW_TABLES} & set(inspector.get_table_names()))
    remaining = {column["name"] for column in inspector.get_columns("trades")}
    assert not (NEW_TRADE_COLUMNS & remaining)
    assert {"trade_id", "symbol", "side", "volume", "status", "stop_loss", "strategy"} <= remaining
