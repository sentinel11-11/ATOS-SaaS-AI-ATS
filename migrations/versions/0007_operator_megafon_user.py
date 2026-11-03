"""Store the MegaFon VATS login for each ATS operator."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "operators",
        sa.Column("megafon_user", sa.String(length=120), server_default="", nullable=False),
    )
    op.add_column("contacts", sa.Column("source", sa.String(length=64), server_default="manual", nullable=False))
    op.create_table(
        "amo_sync_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("status", sa.String(20), server_default="preview", nullable=False),
        sa.Column("mode", sa.String(20), server_default="manual", nullable=False),
        sa.Column("cursor", sa.String(120), server_default="", nullable=False),
        sa.Column("counts", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("error", sa.String(120), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('preview','queued','running','completed','failed','cancelled')"),
        sa.CheckConstraint("mode IN ('manual','scheduled','webhook')"),
        sa.UniqueConstraint("tenant_id", "id"),
    )
    op.create_index("ix_amo_sync_jobs_tenant_created", "amo_sync_jobs", ["tenant_id", "created_at"])
    op.create_table(
        "amo_sync_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("crm_contact_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), server_default="", nullable=False),
        sa.Column("phone", sa.String(16), server_default="", nullable=False),
        sa.Column("email", sa.String(254), server_default="", nullable=False),
        sa.Column("group", sa.String(120), server_default="", nullable=False),
        sa.Column("source", sa.String(64), server_default="amoCRM", nullable=False),
        sa.Column("action", sa.String(24), server_default="create", nullable=False),
        sa.Column("selected", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("consent", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("applied", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("contact_id", sa.Uuid(), nullable=True),
        sa.Column("error", sa.String(120), server_default="", nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "job_id"], ["amo_sync_jobs.tenant_id", "amo_sync_jobs.id"]),
        sa.ForeignKeyConstraint(["tenant_id", "contact_id"], ["contacts.tenant_id", "contacts.id"]),
        sa.UniqueConstraint("tenant_id", "job_id", "crm_contact_id"),
    )
    op.create_index("ix_amo_sync_items_job", "amo_sync_items", ["tenant_id", "job_id"])


def downgrade():
    op.drop_index("ix_amo_sync_items_job", table_name="amo_sync_items")
    op.drop_table("amo_sync_items")
    op.drop_index("ix_amo_sync_jobs_tenant_created", table_name="amo_sync_jobs")
    op.drop_table("amo_sync_jobs")
    op.drop_column("contacts", "source")
    op.drop_column("operators", "megafon_user")
