"""Customer public sequential number, persistent counters, service order equipment type.

Aditiva e idempotente: só adiciona colunas/tabela; não altera nem apaga dados existentes.
Clientes antigos recebem o número sequencial na ordem de cadastro (id).
"""
from alembic import op
import sqlalchemy as sa

revision = '20261031_0008'
down_revision = '20261030_0007'
branch_labels = None
depends_on = None


def _columns(table):
    return {c['name'] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'counters' not in tables:
        op.create_table('counters',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('name', sa.String(60), nullable=False, unique=True),
            sa.Column('value', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()))
    if 'public_seq' not in _columns('customers'):
        with op.batch_alter_table('customers') as batch:
            batch.add_column(sa.Column('public_seq', sa.Integer(), nullable=True))
            batch.create_index('ix_customers_public_seq', ['public_seq'], unique=True)
    if 'equipment_type' not in _columns('service_orders'):
        with op.batch_alter_table('service_orders') as batch:
            batch.add_column(sa.Column('equipment_type', sa.String(20), nullable=True))
            batch.create_index('ix_service_orders_equipment_type', ['equipment_type'])
    rows = bind.execute(sa.text('SELECT id FROM customers WHERE public_seq IS NULL ORDER BY id')).fetchall()
    current = bind.execute(sa.text('SELECT COALESCE(MAX(public_seq), 0) FROM customers')).scalar() or 0
    for (customer_id,) in rows:
        current += 1
        bind.execute(sa.text('UPDATE customers SET public_seq = :s WHERE id = :i'), {'s': current, 'i': customer_id})
    exists = bind.execute(sa.text("SELECT 1 FROM counters WHERE name = 'customer_public_seq'")).first()
    if exists:
        bind.execute(sa.text("UPDATE counters SET value = :v WHERE name = 'customer_public_seq' AND value < :v"), {'v': current})
    else:
        bind.execute(sa.text("INSERT INTO counters (name, value, created_at) VALUES ('customer_public_seq', :v, CURRENT_TIMESTAMP)"), {'v': current})


def downgrade():
    # Intencionalmente não destrutivo: não remove colunas com dados. Reverter exige decisão manual.
    pass
