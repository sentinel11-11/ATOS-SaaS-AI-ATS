"""Authenticated widget calls and private browser SIP devices."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operator_devices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(80), nullable=False),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id", "operator_id"], ["operators.tenant_id", "operators.id"]),
        sa.UniqueConstraint("operator_id"), sa.UniqueConstraint("endpoint"),
    )
    op.create_index("ix_operator_devices_tenant_id", "operator_devices", ["tenant_id"])
    op.create_table(
        "widget_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("client_request_id", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("operator_token", sa.String(64), nullable=False),
        sa.Column("crm_context", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("media_ready_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "operator_id", "client_request_id"),
        sa.UniqueConstraint("campaign_id"),
        sa.ForeignKeyConstraint(["tenant_id", "operator_id"], ["operators.tenant_id", "operators.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "campaign_id"], ["campaigns.tenant_id", "campaigns.id"]),
        sa.CheckConstraint("mode IN ('forward','browser')", name="ck_widget_call_mode"),
    )
    op.create_index("ix_widget_calls_tenant_id", "widget_calls", ["tenant_id"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM widget_calls) OR EXISTS(SELECT 1 FROM operator_devices)")).scalar():
        raise RuntimeError("Widget history/devices exist; retain schema 0013 during code recovery")
    op.drop_table("widget_calls")
    op.drop_table("operator_devices")
