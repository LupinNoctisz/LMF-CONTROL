"""Add relational columns required by the central-data modules."""
from alembic import op
import sqlalchemy as sa

revision = '20261028_0002'
down_revision = '20261028_0001'
branch_labels = None
depends_on = None

def _add(table, column):
    names={c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name not in names:
        if column.foreign_keys and op.get_bind().dialect.name=='sqlite':
            targets=[fk.target_fullname for fk in column.foreign_keys]
            target_table,target_column=targets[0].split('.',1)
            plain=sa.Column(column.name,column.type,nullable=column.nullable,server_default=column.server_default)
            with op.batch_alter_table(table) as batch:
                batch.add_column(plain)
                batch.create_foreign_key(f'fk_{table}_{column.name}_{target_table}',target_table,[column.name],[target_column])
        else: op.add_column(table,column)

def _index(table, name, columns, unique=False):
    names={i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in names: op.create_index(name,table,columns,unique=unique)

def upgrade():
    _add('customers',sa.Column('whatsapp',sa.String(40),nullable=True))
    _add('customers',sa.Column('active',sa.Boolean(),nullable=False,server_default=sa.true()))
    _add('suppliers',sa.Column('whatsapp',sa.String(40),nullable=True))
    _add('suppliers',sa.Column('website',sa.String(250),nullable=True))
    _add('suppliers',sa.Column('lead_time_days',sa.Integer(),nullable=True))
    _add('products',sa.Column('internal_code',sa.String(100),nullable=True))
    _add('products',sa.Column('supplier_id',sa.Integer(),sa.ForeignKey('suppliers.id'),nullable=True))
    _add('products',sa.Column('brand',sa.String(120),nullable=True))
    _add('products',sa.Column('model',sa.String(120),nullable=True))
    _add('movements',sa.Column('source_key',sa.String(180),nullable=True))
    _add('service_orders',sa.Column('accessories',sa.Text(),nullable=True))
    _add('service_orders',sa.Column('physical_condition',sa.Text(),nullable=True))
    _add('service_orders',sa.Column('service_executed',sa.Text(),nullable=True))
    _add('service_orders',sa.Column('cost',sa.Float(),nullable=False,server_default='0'))
    _add('service_orders',sa.Column('charged',sa.Float(),nullable=False,server_default='0'))
    _add('service_orders',sa.Column('completed_on',sa.Date(),nullable=True))
    _add('guarantees',sa.Column('guarantee_type',sa.String(40),nullable=False,server_default='serviço'))
    _add('guarantees',sa.Column('duration_days',sa.Integer(),nullable=True))
    _add('guarantees',sa.Column('status',sa.String(30),nullable=False,server_default='ativa'))
    _add('financial_entries',sa.Column('bank_account_id',sa.Integer(),sa.ForeignKey('bank_accounts.id'),nullable=True))
    _add('financial_entries',sa.Column('status',sa.String(30),nullable=False,server_default='pendente'))
    _add('financial_entries',sa.Column('competence_on',sa.Date(),nullable=True))
    _add('financial_entries',sa.Column('customer_id',sa.Integer(),sa.ForeignKey('customers.id'),nullable=True))
    _add('financial_entries',sa.Column('service_order_id',sa.Integer(),sa.ForeignKey('service_orders.id'),nullable=True))
    _add('financial_entries',sa.Column('order_id',sa.Integer(),sa.ForeignKey('orders.id'),nullable=True))
    _add('financial_entries',sa.Column('sale_id',sa.Integer(),sa.ForeignKey('sales.id'),nullable=True))
    _add('financial_entries',sa.Column('external_id',sa.String(180),nullable=True))
    for table,name,columns,unique in [
        ('products','ix_products_internal_code',['internal_code'],True),
        ('products','ix_products_supplier_id',['supplier_id'],False),
        ('movements','ix_movements_source_key',['source_key'],True),
        ('financial_entries','ix_financial_entries_bank_account_id',['bank_account_id'],False),
        ('financial_entries','ix_financial_entries_customer_id',['customer_id'],False),
        ('financial_entries','ix_financial_entries_service_order_id',['service_order_id'],False),
        ('financial_entries','ix_financial_entries_order_id',['order_id'],False),
        ('financial_entries','ix_financial_entries_sale_id',['sale_id'],False),
        ('financial_entries','ix_financial_entries_external_id',['external_id'],True),
    ]: _index(table,name,columns,unique)

def downgrade():
    # Columns are retained to protect user data on downgrade.
    pass
