"""Detach deleted identities and add adjustable caller-number usage cycles."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("campaign_items", "contact_id", nullable=True)
    op.alter_column("widget_calls", "operator_id", nullable=True)
    op.alter_column("widget_calls", "initiator_user_id", nullable=True)
    op.add_column("inbound_calls", sa.Column("contact_snapshot", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("caller_numbers", sa.Column("quota_started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column("caller_numbers", sa.Column("quota_cooldown_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("caller_numbers", sa.Column("quota_cooldown_seconds", sa.Integer(), nullable=False, server_default="3600"))
    op.execute(sa.text("""UPDATE campaigns AS c SET window_start=0, window_end=24,
        weekdays='[0,1,2,3,4,5,6]'::json,
        schedule=(c.schedule::jsonb || jsonb_build_object('source','amo_widget',
            'widget_call_id',w.id::text, 'operator_id',w.operator_id::text,
            'connection_method',CASE WHEN w.mode='forward' THEN 'mobile' ELSE 'browser' END))::json
        FROM widget_calls AS w WHERE w.campaign_id=c.id AND w.tenant_id=c.tenant_id"""))


def downgrade():
    if op.get_bind().scalar(sa.text("""SELECT
        EXISTS (SELECT 1 FROM campaign_items WHERE contact_id IS NULL)
        OR EXISTS (SELECT 1 FROM widget_calls WHERE operator_id IS NULL OR initiator_user_id IS NULL)
        OR EXISTS (SELECT 1 FROM inbound_calls WHERE contact_snapshot::jsonb <> '{}'::jsonb)
        OR EXISTS (SELECT 1 FROM caller_numbers WHERE quota_cooldown_until > now())
    """)):
        raise RuntimeError("Detached call history or active caller cooldown exists; preserve it before downgrading")
    op.drop_column("inbound_calls", "contact_snapshot")
    op.drop_column("caller_numbers", "quota_cooldown_until")
    op.drop_column("caller_numbers", "quota_cooldown_seconds")
    op.drop_column("caller_numbers", "quota_started_at")
    op.alter_column("widget_calls", "initiator_user_id", nullable=False)
    op.alter_column("widget_calls", "operator_id", nullable=False)
    op.alter_column("campaign_items", "contact_id", nullable=False)
