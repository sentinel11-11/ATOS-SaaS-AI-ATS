"""Separate widget initiators and participants and lease one browser agent per device."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("widget_calls", sa.Column("initiator_user_id", sa.Uuid(), nullable=True))
    op.add_column("widget_calls", sa.Column("browser_session_id", sa.Uuid(), nullable=True))
    op.execute(sa.text("""
        UPDATE widget_calls AS call
        SET initiator_user_id = COALESCE(
            NULLIF(call.crm_context->>'initiator_ats_user_id', '')::uuid, operator.user_id
        ), browser_session_id = NULLIF(call.crm_context->>'browser_session_id', '')::uuid
        FROM operators AS operator
        WHERE operator.id = call.operator_id AND operator.tenant_id = call.tenant_id
    """))
    op.alter_column("widget_calls", "initiator_user_id", nullable=False)
    op.create_foreign_key("fk_widget_initiator", "widget_calls", "users",
                          ["tenant_id", "initiator_user_id"], ["tenant_id", "id"])
    op.create_unique_constraint("uq_widget_initiator_request", "widget_calls",
                                ["tenant_id", "initiator_user_id", "client_request_id"])
    op.create_index("ix_widget_call_token", "widget_calls", ["operator_token"])
    op.add_column("operator_devices", sa.Column("agent_session_id", sa.Uuid(), nullable=True))
    op.add_column("operator_devices", sa.Column("agent_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("operator_devices", sa.Column("agent_registered", sa.Boolean(), nullable=False, server_default="false"))


def downgrade():
    op.drop_column("operator_devices", "agent_registered")
    op.drop_column("operator_devices", "agent_expires_at")
    op.drop_column("operator_devices", "agent_session_id")
    op.drop_index("ix_widget_call_token", table_name="widget_calls")
    op.drop_constraint("uq_widget_initiator_request", "widget_calls", type_="unique")
    op.drop_constraint("fk_widget_initiator", "widget_calls", type_="foreignkey")
    op.drop_column("widget_calls", "browser_session_id")
    op.drop_column("widget_calls", "initiator_user_id")
