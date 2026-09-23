"""news calendar cache and provider health"""
from alembic import op
import sqlalchemy as sa
revision = "0006_news_calendar"; down_revision = "0005_session_kill_switch"; branch_labels = None; depends_on = None


def upgrade():
    op.create_table(
        "news_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("country", sa.String(64), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("impact", sa.String(16), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual", sa.Text(), nullable=True),
        sa.Column("forecast", sa.Text(), nullable=True),
        sa.Column("previous", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_news_events_event_id", "news_events", ["event_id"], unique=True)
    op.create_index("ix_news_events_provider", "news_events", ["provider"], unique=False)
    op.create_index("ix_news_events_currency", "news_events", ["currency"], unique=False)
    op.create_index("ix_news_events_impact", "news_events", ["impact"], unique=False)
    op.create_index("ix_news_events_event_time", "news_events", ["event_time"], unique=False)

    op.create_table(
        "news_provider_state",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("healthy", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_news_provider_state_provider", "news_provider_state", ["provider"], unique=True)


def downgrade():
    op.drop_index("ix_news_provider_state_provider", table_name="news_provider_state")
    op.drop_table("news_provider_state")
    for name in ("ix_news_events_event_time", "ix_news_events_impact", "ix_news_events_currency", "ix_news_events_provider", "ix_news_events_event_id"):
        op.drop_index(name, table_name="news_events")
    op.drop_table("news_events")
