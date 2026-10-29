"""Track idempotent purchase receipts for stock intake."""
from alembic import op
import sqlalchemy as sa

revision = '20261028_0004'
down_revision = '20261028_0003'
branch_labels = None
depends_on = None

def upgrade():
    if 'purchase_receipts' not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table('purchase_receipts',
            sa.Column('id',sa.Integer(),primary_key=True),
            sa.Column('request_key',sa.String(180),nullable=False,unique=True),
            sa.Column('order_id',sa.Integer(),sa.ForeignKey('orders.id'),nullable=False),
            sa.Column('order_item_id',sa.Integer(),sa.ForeignKey('order_items.id'),nullable=False),
            sa.Column('product_id',sa.Integer(),sa.ForeignKey('products.id'),nullable=False),
            sa.Column('quantity',sa.Integer(),nullable=False),
            sa.Column('unit_cost',sa.Float(),nullable=False,server_default='0'),
            sa.Column('created_at',sa.DateTime(),nullable=False,server_default=sa.func.current_timestamp()),
            sa.CheckConstraint('quantity > 0',name='ck_purchase_receipt_positive_quantity'))
    for name,table,column in [('ix_purchase_receipts_request_key','purchase_receipts','request_key'),('ix_purchase_receipts_order_id','purchase_receipts','order_id'),('ix_purchase_receipts_order_item_id','purchase_receipts','order_item_id'),('ix_purchase_receipts_product_id','purchase_receipts','product_id')]:
        indexes={i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table)}
        if name not in indexes: op.create_index(name,table,[column],unique=name.endswith('request_key'))

def downgrade():
    pass
