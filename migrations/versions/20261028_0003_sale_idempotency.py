"""Add an idempotency key to sales requests."""
from alembic import op
import sqlalchemy as sa

revision = '20261028_0003'
down_revision = '20261028_0002'
branch_labels = None
depends_on = None

def upgrade():
    columns={c['name'] for c in sa.inspect(op.get_bind()).get_columns('sales')}
    if 'request_key' not in columns:
        op.add_column('sales',sa.Column('request_key',sa.String(180),nullable=True))
    indexes={i['name'] for i in sa.inspect(op.get_bind()).get_indexes('sales')}
    if 'ix_sales_request_key' not in indexes:
        op.create_index('ix_sales_request_key','sales',['request_key'],unique=True)

def downgrade():
    # Keep idempotency data to avoid changing transaction history on downgrade.
    pass
