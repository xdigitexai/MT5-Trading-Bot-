"""session start and the persisted session kill switch"""
from alembic import op
import sqlalchemy as sa
revision = "0005_session_kill_switch"; down_revision = "0004_hard_risk_limits"; branch_labels = None; depends_on = None


def upgrade():
    op.add_column("risk_state", sa.Column("session_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("risk_state", sa.Column("kill_switch_reason", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("risk_state", "kill_switch_reason")
    op.drop_column("risk_state", "session_started_at")
