"""scheduler single-runner lock"""
from alembic import op
import sqlalchemy as sa
revision = "0003_scheduler_lock"; down_revision = "0002_signals_risk_execution"; branch_labels = None; depends_on = None


def upgrade():
    op.create_table(
        "scheduler_locks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("lock_key", sa.String(64), nullable=False),
        sa.Column("owner", sa.String(128), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_scheduler_locks_lock_key", "scheduler_locks", ["lock_key"], unique=True)


def downgrade():
    op.drop_index("ix_scheduler_locks_lock_key", table_name="scheduler_locks")
    op.drop_table("scheduler_locks")
