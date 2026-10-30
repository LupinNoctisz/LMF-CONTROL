"""Add photo attachments (up to 6 per service order; files live in data/documentos)."""
from alembic import op
import sqlalchemy as sa

revision = '20261029_0006'
down_revision = '20261028_0005'
branch_labels = None
depends_on = None

def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if 'service_order_photos' not in tables:
        op.create_table('service_order_photos',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('service_order_id', sa.Integer(), sa.ForeignKey('service_orders.id'), nullable=False),
            sa.Column('original_name', sa.String(255), nullable=False),
            sa.Column('stored_name', sa.String(80), nullable=False, unique=True),
            sa.Column('content_type', sa.String(40), nullable=False),
            sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()))
    indexes = {i['name'] for i in sa.inspect(op.get_bind()).get_indexes('service_order_photos')}
    if 'ix_service_order_photos_service_order_id' not in indexes:
        op.create_index('ix_service_order_photos_service_order_id', 'service_order_photos', ['service_order_id'])

def downgrade():
    op.drop_index('ix_service_order_photos_service_order_id', table_name='service_order_photos')
    op.drop_table('service_order_photos')
