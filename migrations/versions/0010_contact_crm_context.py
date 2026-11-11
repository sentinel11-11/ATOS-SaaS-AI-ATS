"""Store amoCRM pipeline, stage and responsible person on contacts."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for table in ("contacts", "amo_sync_items"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "crm_context" not in columns:
            op.add_column(
                table,
                sa.Column("crm_context", sa.JSON(), server_default="{}", nullable=False),
            )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    for table in ("amo_sync_items", "contacts"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "crm_context" in columns:
            op.drop_column(table, "crm_context")
