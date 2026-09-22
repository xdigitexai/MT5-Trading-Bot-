"""signals, durable risk state, execution guards and reconciliation columns"""
from alembic import op
import sqlalchemy as sa
revision = "0002_signals_risk_execution"; down_revision = "0001_initial"; branch_labels = None; depends_on = None

TRADE_RECONCILIATION_COLUMNS = (
    sa.Column("exit_price", sa.Float, nullable=True),
    sa.Column("commission", sa.Float, nullable=True),
    sa.Column("swap", sa.Float, nullable=True),
    sa.Column("profit", sa.Float, nullable=True),
    sa.Column("open_time", sa.DateTime(timezone=True), nullable=True),
    sa.Column("close_time", sa.DateTime(timezone=True), nullable=True),
    sa.Column("mt5_deal_ticket", sa.String(32), nullable=True),
    sa.Column("volume_closed", sa.Float, nullable=True),
    sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("close_reason", sa.String(64), nullable=True),
)


def upgrade():
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("signal_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("entry_price", sa.Float),
        sa.Column("stop_loss", sa.Float),
        sa.Column("take_profit", sa.Float),
        sa.Column("reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("executed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("order_ticket", sa.String(32)),
        sa.Column("status", sa.String(32), nullable=False),
    )
    op.create_index("ix_signals_signal_id", "signals", ["signal_id"], unique=True)
    op.create_index("ix_signals_symbol", "signals", ["symbol"])
    op.create_index("ix_signals_strategy", "signals", ["strategy"])
    op.create_index("ix_signals_created_at", "signals", ["created_at"])
    op.create_index("ix_signals_executed", "signals", ["executed"])
    op.create_index("ix_signals_status", "signals", ["status"])

    op.create_table(
        "risk_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("realized_pnl", sa.Float, nullable=False, server_default=sa.text("0")),
        sa.Column("starting_equity", sa.Float),
        sa.Column("peak_equity", sa.Float),
        sa.Column("emergency_locked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_risk_state_day", "risk_state", ["day"], unique=True)

    op.create_table(
        "execution_guards",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("signal_id", sa.String(64)),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("volume", sa.Float, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("order_ticket", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_execution_guards_idempotency_key", "execution_guards", ["idempotency_key"], unique=True)
    op.create_index("ix_execution_guards_signal_id", "execution_guards", ["signal_id"])
    op.create_index("ix_execution_guards_status", "execution_guards", ["status"])

    for column in TRADE_RECONCILIATION_COLUMNS:
        op.add_column("trades", column)
    op.create_index("ix_trades_mt5_deal_ticket", "trades", ["mt5_deal_ticket"])


def downgrade():
    op.drop_index("ix_trades_mt5_deal_ticket", table_name="trades")
    with op.batch_alter_table("trades") as batch:
        for column in reversed(TRADE_RECONCILIATION_COLUMNS):
            batch.drop_column(column.name)
    op.drop_index("ix_execution_guards_status", table_name="execution_guards")
    op.drop_index("ix_execution_guards_signal_id", table_name="execution_guards")
    op.drop_index("ix_execution_guards_idempotency_key", table_name="execution_guards")
    op.drop_table("execution_guards")
    op.drop_index("ix_risk_state_day", table_name="risk_state")
    op.drop_table("risk_state")
    for index in ("ix_signals_status", "ix_signals_executed", "ix_signals_created_at", "ix_signals_strategy", "ix_signals_symbol", "ix_signals_signal_id"):
        op.drop_index(index, table_name="signals")
    op.drop_table("signals")
