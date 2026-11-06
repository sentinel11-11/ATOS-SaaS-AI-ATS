"""Add journal visibility for attempts and retire simulator defaults."""

import sqlalchemy as sa
from alembic import op


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    attempt_columns = {column["name"] for column in inspector.get_columns("call_attempts")}
    if "hidden_from_journal" not in attempt_columns:
        op.add_column(
            "call_attempts",
            sa.Column("hidden_from_journal", sa.Boolean(), server_default="false", nullable=False),
        )
    # Numbers imported before live providers were enabled used the simulator
    # placeholder. They are real caller-number records and now belong to the
    # MegaFon provider unless an administrator has already selected another one.
    op.execute(
        "UPDATE caller_numbers SET provider='megafon_vats' "
        "WHERE provider='sim'"
    )
    op.alter_column("caller_numbers", "provider", server_default="megafon_vats")
    # Retire old simulator campaigns without deleting their audit trail. Their
    # attempts are hidden from the journal, queued items cannot be dialled, and
    # the campaign is archived under a real provider route for schema safety.
    op.execute(
        "UPDATE call_attempts SET hidden_from_journal=true "
        "WHERE route='sim'"
    )
    op.execute(
        "UPDATE campaign_items SET status='cancelled' "
        "WHERE status='queued' AND campaign_id IN "
        "(SELECT id FROM campaigns WHERE route='sim')"
    )
    op.execute(
        "UPDATE campaigns SET route='megafon_vats', status='completed', archived=true "
        "WHERE route='sim'"
    )
    for constraint in inspector.get_check_constraints("campaigns"):
        sqltext = (constraint.get("sqltext") or "").lower()
        if constraint.get("name") and "route" in sqltext and "sim" in sqltext:
            op.drop_constraint(constraint["name"], "campaigns", type_="check")
    op.create_check_constraint(
        "ck_campaigns_live_route",
        "campaigns",
        "route IN ('multicom_ami','megafon_vats')",
    )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "hidden_from_journal" in {column["name"] for column in inspector.get_columns("call_attempts")}:
        op.drop_column("call_attempts", "hidden_from_journal")
    op.drop_constraint("ck_campaigns_live_route", "campaigns", type_="check")
    op.create_check_constraint(
        "ck_campaigns_route_legacy",
        "campaigns",
        "route IN ('sim','multicom_ami','megafon_vats')",
    )
