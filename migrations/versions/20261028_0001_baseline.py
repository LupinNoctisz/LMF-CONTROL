"""Create any missing LMF tables and establish the Alembic baseline."""
from alembic import op
from app.database import Base
from app import models  # noqa: F401

revision = '20261028_0001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    # create_all is intentionally additive here: existing user tables and rows are preserved.
    Base.metadata.create_all(bind=op.get_bind())

def downgrade():
    # Baseline tables contain user data; destructive downgrade is deliberately unsupported.
    pass
