"""Add mobile operator destinations and callback leg accounting."""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("operators", sa.Column("connection_method", sa.String(16), nullable=False, server_default="sip"))
    op.add_column("operators", sa.Column("mobile_phone", sa.String(16), nullable=False, server_default=""))
    op.create_check_constraint("ck_operator_connection_method", "operators", "connection_method IN ('sip','mobile')")
    op.add_column("call_attempts", sa.Column("call_flow", sa.String(24), nullable=False, server_default="standard"))
    op.add_column("call_attempts", sa.Column("channel_units", sa.Integer(), nullable=False, server_default="1"))
    for name in ("manager_confirmed_at", "bridged_at", "bridge_ended_at"):
        op.add_column("call_attempts", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.add_column("call_attempts", sa.Column("pending_terminal", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("call_attempts", sa.Column("route_snapshot", sa.JSON(), nullable=False, server_default="{}"))
    op.create_check_constraint("ck_attempt_channel_units", "call_attempts", "channel_units BETWEEN 1 AND 2")
    op.create_table(
        "call_legs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("phone", sa.String(16), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="prepared"),
        sa.Column("channel", sa.String(160), nullable=False, server_default=""),
        sa.Column("uniqueid", sa.String(80), nullable=False, server_default=""),
        sa.Column("linkedid", sa.String(80), nullable=False, server_default=""),
        sa.Column("bridge_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("entered_bridge_at", sa.DateTime(timezone=True)),
        sa.Column("left_bridge_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("dial_status", sa.String(32), nullable=False, server_default=""),
        sa.Column("hangup_cause", sa.Integer()),
        sa.ForeignKeyConstraint(["tenant_id", "attempt_id"], ["call_attempts.tenant_id", "call_attempts.id"]),
        sa.UniqueConstraint("attempt_id", "role"),
        sa.CheckConstraint("role IN ('operator','customer')", name="ck_call_leg_role"),
    )
    op.create_index("ix_call_legs_tenant_id", "call_legs", ["tenant_id"])
    op.create_index("ix_call_legs_tenant_channel", "call_legs", ["tenant_id", "channel"])


def downgrade():
    bind = op.get_bind()
    used = bind.execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM call_legs) OR "
        "EXISTS(SELECT 1 FROM call_attempts WHERE call_flow <> 'standard') OR "
        "EXISTS(SELECT 1 FROM operators WHERE connection_method <> 'sip' OR mobile_phone <> '')"
    )).scalar()
    if used:
        raise RuntimeError("Callback history/configuration exists; roll back code while retaining schema 0012")
    op.drop_table("call_legs")
    op.drop_constraint("ck_attempt_channel_units", "call_attempts", type_="check")
    for name in ("route_snapshot", "pending_terminal", "bridge_ended_at", "bridged_at", "manager_confirmed_at", "channel_units", "call_flow"):
        op.drop_column("call_attempts", name)
    op.drop_constraint("ck_operator_connection_method", "operators", type_="check")
    op.drop_column("operators", "mobile_phone")
    op.drop_column("operators", "connection_method")
