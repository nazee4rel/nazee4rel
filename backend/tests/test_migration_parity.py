"""Guards the migration against drifting from the ORM models.

The realistic failure mode on a project like this is not a migration that
crashes — it is a migration that succeeds while quietly disagreeing with the
models, so a column exists in Python and not in Postgres. That surfaces much
later as a confusing runtime error.

Rather than requiring a live Postgres, these tests replay the migration's
`upgrade()` against a recorder that captures every `op.*` call, then compare the
resulting schema with `Base.metadata`. Fast, hermetic, and it fails the moment
someone adds a model column without a migration.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from app.models import Base

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"

# Tables introduced by later phases are not expected in the Phase 2 migration.
# Adding a model without adding it here *or* to a migration fails the parity
# test, which is the point.
EXPECTED_TABLES = {
    "users",
    "sessions",
    "x_accounts",
    "oauth_tokens",
    "account_capabilities",
    "audit_logs",
    "system_logs",
    "oauth_states",
    "api_usage_ledger",
    "posts",
    "post_metric_snapshots",
    "account_metric_snapshots",
    "collection_runs",
    "campaigns",
    "sponsorships",
    "revenue_entries",
    "revenue_attributions",
    "topics",
    "post_topics",
}


class OpRecorder:
    """Stands in for `alembic.op`, recording DDL instead of executing it."""

    def __init__(self) -> None:
        self.tables: dict[str, set[str]] = {}
        self.indexes: list[tuple[str, str, list[str]]] = []
        self.unique_indexes: set[str] = set()
        self.executed: list[str] = []

    def create_table(self, name: str, *args: Any, **_kw: Any) -> None:
        self.tables[name] = {c.name for c in args if isinstance(c, sa.Column)}

    def create_index(self, index_name: str, table_name: str, columns: list[str], **kw: Any) -> None:
        self.indexes.append((index_name, table_name, columns))
        if kw.get("unique"):
            self.unique_indexes.add(index_name)

    def drop_table(self, name: str, **_kw: Any) -> None:  # pragma: no cover
        self.tables.pop(name, None)

    def execute(self, statement: Any, **_kw: Any) -> None:
        self.executed.append(str(statement))

    def __getattr__(self, item: str) -> Any:  # pragma: no cover
        # Tolerate op.* calls this recorder does not model explicitly.
        def _noop(*_a: Any, **_k: Any) -> None:
            return None

        return _noop


def _load_migration(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def recorded() -> OpRecorder:
    recorder = OpRecorder()
    for path in sorted(MIGRATIONS_DIR.glob("[0-9]*.py")):
        module = _load_migration(path)
        module.op = recorder  # replace the alembic proxy
        module.upgrade()
    return recorder


class TestMigrationParity:
    def test_creates_every_expected_table(self, recorded: OpRecorder) -> None:
        assert set(recorded.tables) == EXPECTED_TABLES

    def test_every_model_table_is_migrated(self, recorded: OpRecorder) -> None:
        """No model may exist without a migration creating its table."""
        model_tables = set(Base.metadata.tables)
        missing = model_tables - set(recorded.tables)
        assert not missing, f"Models with no migration: {sorted(missing)}"

    def test_no_orphan_tables(self, recorded: OpRecorder) -> None:
        """No migration may create a table with no corresponding model."""
        orphans = set(recorded.tables) - set(Base.metadata.tables)
        assert not orphans, f"Migrated tables with no model: {sorted(orphans)}"

    @pytest.mark.parametrize("table_name", sorted(EXPECTED_TABLES))
    def test_columns_match_models(self, recorded: OpRecorder, table_name: str) -> None:
        model_columns = {c.name for c in Base.metadata.tables[table_name].columns}
        migrated_columns = recorded.tables[table_name]

        missing = model_columns - migrated_columns
        extra = migrated_columns - model_columns
        assert not missing, f"{table_name}: in models but not migrated: {sorted(missing)}"
        assert not extra, f"{table_name}: migrated but not in models: {sorted(extra)}"

    def test_downgrade_drops_enum_types(self) -> None:
        """Postgres keeps enum types after their tables are dropped.

        A downgrade that leaves them behind makes the subsequent upgrade fail
        with 'type already exists', so the rollback path is only actually
        reversible if these are cleaned up.
        """
        recorder = OpRecorder()
        module = _load_migration(MIGRATIONS_DIR / "0001_identity_and_access.py")
        module.op = recorder
        module.downgrade()

        dropped = " ".join(recorder.executed)
        for enum_name in ("user_role", "x_capability", "capability_status", "audit_action"):
            assert enum_name in dropped, f"downgrade leaves enum type {enum_name} behind"


class TestCriticalIndexes:
    """Indexes the query patterns in the architecture doc depend on."""

    def test_session_token_lookup_is_unique(self, recorded: OpRecorder) -> None:
        assert "ix_sessions_token_hash" in recorded.unique_indexes

    def test_email_is_unique(self, recorded: OpRecorder) -> None:
        assert "ix_users_email" in recorded.unique_indexes

    def test_one_token_row_per_account(self, recorded: OpRecorder) -> None:
        assert "ix_oauth_tokens_x_account_id" in recorded.unique_indexes
