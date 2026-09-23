"""the single live-trade authorization"""
from alembic import op
import sqlalchemy as sa
revision = "0007_one_trade_authorization"; down_revision = "0006_news_calendar"; branch_labels = None; depends_on = None

# The singleton row the reservation updates. A fixed id makes "the authorization" one row, so the
# conditional UPDATE can only ever be won once, by one process, on any day.
AUTHORIZATION_ID = 1


def upgrade():
    op.create_table(
        "one_trade_authorization",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_trade_id", sa.String(64), nullable=True),
        sa.Column("consumed_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    # The row exists from the moment the schema is migrated and starts *unconsumed*: the owner has
    # authorized one live trade, so the engine comes up armed for exactly that one. Materialising it
    # here (rather than inserting it lazily on first order) keeps the reservation a plain UPDATE.
    op.execute(sa.text(f"INSERT INTO one_trade_authorization (id, consumed, updated_at) VALUES ({AUTHORIZATION_ID}, false, CURRENT_TIMESTAMP)"))


def downgrade():
    op.drop_table("one_trade_authorization")
