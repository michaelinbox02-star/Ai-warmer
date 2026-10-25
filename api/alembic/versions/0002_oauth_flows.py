"""Add oauth_flows for PKCE state (idempotent).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25

Fresh databases already contain this table via `schema.sql`; this revision
exists so a database initialized from schema v1.0 can be upgraded in place.
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_flows (
            id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            state               text NOT NULL UNIQUE,
            provider            text NOT NULL CHECK (provider IN ('google_gmail', 'google_workspace', 'microsoft_365', 'outlook_com')),
            code_verifier       text NOT NULL,
            redirect_uri        text NOT NULL,
            requested_scopes    text[] NOT NULL DEFAULT ARRAY[]::text[],
            account_id          uuid REFERENCES email_accounts(id) ON DELETE SET NULL,
            created_at          timestamptz NOT NULL DEFAULT now(),
            expires_at          timestamptz NOT NULL,
            consumed_at         timestamptz,
            CHECK (expires_at > created_at)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS oauth_flows_expires_idx ON oauth_flows (expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS oauth_flows")
