"""Indexes for tenant-scoped call analytics."""

from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_attempts_analytics_period",
        "call_attempts",
        ["tenant_id", "created_at", "route"],
    )
    op.create_index(
        "ix_inbound_analytics_period",
        "inbound_calls",
        ["tenant_id", "created_at", "route"],
    )
    op.create_index(
        "ix_history_analytics_period",
        "historical_calls",
        ["tenant_id", "created_at", "route", "direction"],
    )
    op.create_index(
        "ix_acd_analytics_operator",
        "acd_queue",
        ["tenant_id", "operator_id", "attempt_id"],
    )


def downgrade():
    op.drop_index("ix_acd_analytics_operator", table_name="acd_queue")
    op.drop_index("ix_history_analytics_period", table_name="historical_calls")
    op.drop_index("ix_inbound_analytics_period", table_name="inbound_calls")
    op.drop_index("ix_attempts_analytics_period", table_name="call_attempts")
