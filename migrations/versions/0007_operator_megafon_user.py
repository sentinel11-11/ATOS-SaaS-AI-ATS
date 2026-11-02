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


def downgrade():
    op.drop_column("operators", "megafon_user")
