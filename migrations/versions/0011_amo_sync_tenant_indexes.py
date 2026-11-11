"""Add tenant indexes for amoCRM synchronization tables."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "ix_amo_sync_items_tenant_id" not in {index["name"] for index in inspector.get_indexes("amo_sync_items")}:
        op.create_index("ix_amo_sync_items_tenant_id", "amo_sync_items", ["tenant_id"])
    if "ix_amo_sync_jobs_tenant_id" not in {index["name"] for index in inspector.get_indexes("amo_sync_jobs")}:
        op.create_index("ix_amo_sync_jobs_tenant_id", "amo_sync_jobs", ["tenant_id"])


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "ix_amo_sync_jobs_tenant_id" in {index["name"] for index in inspector.get_indexes("amo_sync_jobs")}:
        op.drop_index("ix_amo_sync_jobs_tenant_id", table_name="amo_sync_jobs")
    if "ix_amo_sync_items_tenant_id" in {index["name"] for index in inspector.get_indexes("amo_sync_items")}:
        op.drop_index("ix_amo_sync_items_tenant_id", table_name="amo_sync_items")
