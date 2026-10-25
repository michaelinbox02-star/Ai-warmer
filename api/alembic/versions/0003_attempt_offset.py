"""Track durable attempt numbering across operator requeues.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25

`attempt_count` is the retry budget for the current cycle. A manual requeue
resets that budget but must not reuse `(message_id, attempt_number)` values in
`delivery_attempts`, so the durable attempt number becomes
`attempt_offset + attempt_count`.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE email_messages "
        "ADD COLUMN IF NOT EXISTS attempt_offset integer NOT NULL DEFAULT 0"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'email_messages_attempt_offset_chk'
            ) THEN
                ALTER TABLE email_messages
                    ADD CONSTRAINT email_messages_attempt_offset_chk
                    CHECK (attempt_offset >= 0);
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE email_messages "
        "DROP CONSTRAINT IF EXISTS email_messages_attempt_offset_chk"
    )
    op.execute("ALTER TABLE email_messages DROP COLUMN IF EXISTS attempt_offset")
