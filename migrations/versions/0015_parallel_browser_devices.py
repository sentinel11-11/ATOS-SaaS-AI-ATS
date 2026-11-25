"""Give simultaneous browser sessions independent private SIP endpoints."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("operator_devices", sa.Column("slot", sa.Integer(), nullable=False, server_default="0"))
    op.drop_constraint("operator_devices_operator_id_key", "operator_devices", type_="unique")
    op.create_unique_constraint("uq_operator_device_slot", "operator_devices", ["operator_id", "slot"])
    op.create_check_constraint("ck_operator_device_slot", "operator_devices", "slot >= 0 AND slot < 8")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM operator_devices WHERE slot > 0)")):
        raise RuntimeError("Parallel browser devices exist; preserve their credentials before downgrading")
    op.drop_constraint("ck_operator_device_slot", "operator_devices", type_="check")
    op.drop_constraint("uq_operator_device_slot", "operator_devices", type_="unique")
    op.create_unique_constraint("operator_devices_operator_id_key", "operator_devices", ["operator_id"])
    op.drop_column("operator_devices", "slot")
