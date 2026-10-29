"""Add local administrator accounts and audit opening-balance corrections."""
from alembic import op
import sqlalchemy as sa

revision = '20261028_0005'
down_revision = '20261028_0004'
branch_labels = None
depends_on = None

def upgrade():
    account_columns={c['name'] for c in sa.inspect(op.get_bind()).get_columns('bank_accounts')}
    if 'opening_balance_date' not in account_columns:
        op.add_column('bank_accounts',sa.Column('opening_balance_date',sa.Date(),nullable=True))
    tables=set(sa.inspect(op.get_bind()).get_table_names())
    if 'app_users' not in tables:
        op.create_table('app_users',
            sa.Column('id',sa.Integer(),primary_key=True),
            sa.Column('username',sa.String(60),nullable=False,unique=True),
            sa.Column('password_hash',sa.String(300),nullable=False),
            sa.Column('is_admin',sa.Boolean(),nullable=False,server_default=sa.true()),
            sa.Column('active',sa.Boolean(),nullable=False,server_default=sa.true()),
            sa.Column('created_at',sa.DateTime(),nullable=False,server_default=sa.func.current_timestamp()))
    tables=set(sa.inspect(op.get_bind()).get_table_names())
    if 'account_balance_adjustments' not in tables:
        op.create_table('account_balance_adjustments',
            sa.Column('id',sa.Integer(),primary_key=True),
            sa.Column('bank_account_id',sa.Integer(),sa.ForeignKey('bank_accounts.id'),nullable=False),
            sa.Column('user_id',sa.Integer(),sa.ForeignKey('app_users.id'),nullable=False),
            sa.Column('old_balance',sa.Float(),nullable=False),
            sa.Column('new_balance',sa.Float(),nullable=False),
            sa.Column('old_reference_date',sa.Date(),nullable=True),
            sa.Column('new_reference_date',sa.Date(),nullable=True),
            sa.Column('action',sa.String(40),nullable=False),
            sa.Column('reason',sa.String(250),nullable=False),
            sa.Column('created_at',sa.DateTime(),nullable=False,server_default=sa.func.current_timestamp()))
    for name,table,column in [('ix_app_users_username','app_users','username'),('ix_account_balance_adjustments_bank_account_id','account_balance_adjustments','bank_account_id'),('ix_account_balance_adjustments_user_id','account_balance_adjustments','user_id')]:
        indexes={i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table)}
        if name not in indexes: op.create_index(name,table,[column],unique=name=='ix_app_users_username')

def downgrade():
    # Authentication and administrative audit records must survive schema rollback.
    pass
