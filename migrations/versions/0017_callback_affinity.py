"""Index recent outgoing calls by customer phone for inbound routing."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "callback_affinity",
        sa.Column("tenant_id", sa.Uuid, primary_key=True),
        sa.Column("phone", sa.String(16), primary_key=True),
        sa.Column("last_operator_id", sa.Uuid, nullable=False),
        sa.Column("call_flow", sa.String(24), nullable=False),
        sa.Column("dialed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "last_operator_id"], ["operators.tenant_id", "operators.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_callback_affinity_expires_at", "callback_affinity", ["expires_at"])
    op.create_unique_constraint("uq_inbound_tenant_id", "inbound_calls", ["tenant_id", "id"])
    op.create_table(
        "inbound_callbacks",
        sa.Column("inbound_id", sa.Uuid, primary_key=True),
        sa.Column("tenant_id", sa.Uuid, nullable=False),
        sa.Column("operator_id", sa.Uuid),
        sa.Column("browser_session_id", sa.Uuid),
        sa.Column("browser_token", sa.String(48), unique=True),
        sa.Column("caller_number", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "inbound_id"], ["inbound_calls.tenant_id", "inbound_calls.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operator_id"], ["operators.tenant_id", "operators.id"],
        ),
    )
    op.create_index(
        "ix_inbound_callbacks_active", "inbound_callbacks", ["tenant_id", "expires_at"],
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_inbound_callbacks_caller_usage", "inbound_callbacks",
        ["tenant_id", "caller_number", "created_at"],
    )
    op.execute(r"""
        WITH recent AS (
            SELECT a.id, a.tenant_id, a.phone, q.operator_id, a.call_flow,
                COALESCE((
                    SELECT min(e.created_at) FROM call_events AS e
                    WHERE e.tenant_id = a.tenant_id AND e.attempt_id = a.id
                        AND e.event = CASE WHEN a.call_flow = 'standard'
                            THEN 'dispatching' ELSE 'callback_customer_created' END
                ), leg.answered_at, a.created_at) AS dialed_at
            FROM call_attempts AS a
            JOIN acd_queue AS q ON q.tenant_id = a.tenant_id AND q.attempt_id = a.id
            LEFT JOIN call_legs AS leg ON leg.tenant_id = a.tenant_id AND leg.attempt_id = a.id
                AND leg.role = 'customer'
            WHERE a.created_at > now() - interval '7 days'
                AND a.phone ~ '^[+][1-9][0-9]{7,14}$'
                AND q.operator_id IS NOT NULL
                AND q.created_at <= COALESCE(a.ended_at, now())
                AND (
                    (a.call_flow IN ('mobile_callback', 'browser_callback') AND leg.channel <> '')
                    OR (a.call_flow = 'standard' AND a.external_id <> '' AND (
                        a.status IN ('accepted', 'answered', 'completed') OR a.answered_at IS NOT NULL
                        OR a.reason IN ('busy', 'no_answer')
                        OR EXISTS (
                            SELECT FROM inbox AS i WHERE i.tenant_id = a.tenant_id
                                AND i.route = a.route AND i.external_id = a.external_id
                                AND i.payload->>'dial_status' IN ('BUSY', 'NOANSWER')
                        )
                    ))
                )
        )
        INSERT INTO callback_affinity
            (tenant_id, phone, last_operator_id, call_flow, dialed_at, expires_at)
        SELECT DISTINCT ON (tenant_id, phone)
            tenant_id, phone, operator_id, call_flow, dialed_at, dialed_at + interval '7 days'
        FROM recent
        ORDER BY tenant_id, phone, dialed_at DESC, id DESC
    """)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT FROM inbound_callbacks WHERE released_at IS NULL)")):
        raise RuntimeError("Finish or release inbound callback offers before downgrading")
    op.drop_table("inbound_callbacks")
    op.drop_constraint("uq_inbound_tenant_id", "inbound_calls", type_="unique")
    op.drop_table("callback_affinity")
