"""Baseline schema from schema.sql.

Revision ID: 0001
Revises:
Create Date: 2026-10-25

For a fresh database this executes the canonical `schema.sql` in one
transaction. For a database that was already initialized directly from
`schema.sql`, it detects the baseline and does nothing; later revisions apply
incremental changes.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "schema.sql",  # repo layout
    Path.cwd() / "schema.sql",  # container working dir
    Path("/app/schema.sql"),  # container fallback
)


def _schema_sql() -> str:
    for path in _CANDIDATES:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    searched = ", ".join(str(p) for p in _CANDIDATES)
    raise RuntimeError(f"schema.sql not found; looked in: {searched}")


def upgrade() -> None:
    bind = op.get_bind()
    existing = bind.execute(sa.text("SELECT to_regclass('public.email_accounts')")).scalar()
    if existing is not None:
        print("Baseline schema detected; 0001 is a no-op.")
        return
    bind.exec_driver_sql(_schema_sql())


def downgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(
        """
        DROP TABLE IF EXISTS
            oauth_flows,
            diagnostic_reports,
            audit_log,
            safety_events,
            test_run_results,
            test_runs,
            domain_auth_checks,
            suppressions,
            provider_events,
            quota_reservations,
            delivery_attempts,
            email_messages,
            allowed_test_recipients,
            send_policies,
            provider_credentials,
            app_settings,
            email_accounts
        CASCADE;
        """
    )
