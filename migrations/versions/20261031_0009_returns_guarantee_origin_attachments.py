"""Sale returns, guarantee origin/links, guarantee attachments, finance receipts, OS brand/model.

Aditiva e idempotente: nenhuma coluna/tabela existente é alterada ou apagada.
Garantias antigas ficam com origin NULL (tratadas como 'manual').
"""
from alembic import op
import sqlalchemy as sa

revision = '20261031_0009'
down_revision = '20261031_0008'
branch_labels = None
depends_on = None


def _cols(table):
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _add(table, columns, indexes=()):
    existing = _cols(table)
    missing = [c for c in columns if c.name not in existing]
    if not missing and not indexes:
        return
    with op.batch_alter_table(table) as batch:
        for column in missing:
            batch.add_column(column)
        for name, cols in indexes:
            if name not in {i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table)}:
                batch.create_index(name, cols)


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    now = sa.func.current_timestamp()
    if 'sale_returns' not in tables:
        op.create_table('sale_returns',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('sale_id', sa.Integer(), sa.ForeignKey('sales.id', ondelete='CASCADE'), nullable=False),
            sa.Column('sale_item_id', sa.Integer(), sa.ForeignKey('sale_items.id', ondelete='CASCADE'), nullable=False),
            sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=False),
            sa.Column('quantity', sa.Integer(), nullable=False),
            sa.Column('condition', sa.String(20), nullable=False),
            sa.Column('restocked', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('request_key', sa.String(180), nullable=True, unique=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=now),
            sa.CheckConstraint('quantity > 0', name='ck_sale_return_positive_quantity'))
        op.create_index('ix_sale_returns_sale_id', 'sale_returns', ['sale_id'])
        op.create_index('ix_sale_returns_sale_item_id', 'sale_returns', ['sale_item_id'])
        op.create_index('ix_sale_returns_product_id', 'sale_returns', ['product_id'])
    _add('guarantees', [
        sa.Column('origin', sa.String(20), nullable=True),
        sa.Column('sale_id', sa.Integer(), sa.ForeignKey('sales.id'), nullable=True),
        sa.Column('equipment_type', sa.String(20), nullable=True),
        sa.Column('brand', sa.String(120), nullable=True),
        sa.Column('model', sa.String(120), nullable=True),
        sa.Column('serial', sa.String(100), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.Column('purchased_on', sa.Date(), nullable=True),
        sa.Column('service_performed', sa.Text(), nullable=True),
    ], indexes=[('ix_guarantees_origin', ['origin']), ('ix_guarantees_sale_id', ['sale_id'])])
    _add('service_orders', [sa.Column('brand', sa.String(120), nullable=True), sa.Column('model', sa.String(120), nullable=True)])
    if 'guarantee_attachments' not in tables:
        op.create_table('guarantee_attachments',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('guarantee_id', sa.Integer(), sa.ForeignKey('guarantees.id'), nullable=False),
            sa.Column('kind', sa.String(20), nullable=False, server_default='documento'),
            sa.Column('original_name', sa.String(255), nullable=False),
            sa.Column('relative_path', sa.String(300), nullable=False, unique=True),
            sa.Column('content_type', sa.String(40), nullable=False),
            sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('sha256', sa.String(64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=now))
        op.create_index('ix_guarantee_attachments_guarantee_id', 'guarantee_attachments', ['guarantee_id'])
    if 'finance_receipts' not in tables:
        op.create_table('finance_receipts',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('financial_entry_id', sa.Integer(), sa.ForeignKey('financial_entries.id'), nullable=False),
            sa.Column('original_name', sa.String(255), nullable=False),
            sa.Column('relative_path', sa.String(300), nullable=False, unique=True),
            sa.Column('content_type', sa.String(40), nullable=False),
            sa.Column('size', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('sha256', sa.String(64), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=now))
        op.create_index('ix_finance_receipts_financial_entry_id', 'finance_receipts', ['financial_entry_id'])


def downgrade():
    # Intencionalmente não destrutivo: reverter apagaria anexos/devoluções. Exige decisão manual.
    pass
