"""persisted per-session trade counter for the hard risk limits"""
from alembic import op
import sqlalchemy as sa
revision = "0004_hard_risk_limits"; down_revision = "0003_scheduler_lock"; branch_labels = None; depends_on = None


def upgrade():
    op.add_column("risk_state", sa.Column("trades_opened", sa.Integer, nullable=False, server_default="0"))


def downgrade():
    op.drop_column("risk_state", "trades_opened")
