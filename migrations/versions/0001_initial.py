"""initial audit and trades tables"""
from alembic import op
import sqlalchemy as sa
revision = "0001_initial"; down_revision = None; branch_labels = None; depends_on = None
def upgrade():
    op.create_table("bot_events", sa.Column("id",sa.Integer,primary_key=True),sa.Column("event_type",sa.String(64),nullable=False),sa.Column("severity",sa.String(16),nullable=False),sa.Column("message",sa.Text,nullable=False),sa.Column("metadata_json",sa.Text),sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.text("CURRENT_TIMESTAMP")))
    op.create_table("trades", sa.Column("id",sa.Integer,primary_key=True),sa.Column("trade_id",sa.String(64),nullable=False,unique=True),sa.Column("symbol",sa.String(32),nullable=False),sa.Column("side",sa.String(8),nullable=False),sa.Column("volume",sa.Float,nullable=False),sa.Column("status",sa.String(32),nullable=False),sa.Column("mt5_order_ticket",sa.String(32)),sa.Column("mt5_position_ticket",sa.String(32)),sa.Column("requested_price",sa.Float),sa.Column("executed_price",sa.Float),sa.Column("stop_loss",sa.Float,nullable=False),sa.Column("take_profit",sa.Float),sa.Column("signal_score",sa.Integer,nullable=False),sa.Column("strategy",sa.String(64),nullable=False),sa.Column("created_at",sa.DateTime(timezone=True),server_default=sa.text("CURRENT_TIMESTAMP")))
def downgrade(): op.drop_table("trades"); op.drop_table("bot_events")
