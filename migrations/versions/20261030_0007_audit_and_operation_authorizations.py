"""Add append-only audit log and single-use administrative operation authorizations."""
from alembic import op
import sqlalchemy as sa

revision = '20261030_0007'
down_revision = '20261029_0006'
branch_labels = None
depends_on = None

def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if 'audit_log' not in tables:
        op.create_table('audit_log',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('app_users.id'), nullable=True),
            sa.Column('username', sa.String(60), nullable=False, server_default='sistema'),
            sa.Column('action', sa.String(80), nullable=False),
            sa.Column('target', sa.String(120), nullable=True),
            sa.Column('outcome', sa.String(20), nullable=False),
            sa.Column('ip', sa.String(64), nullable=True),
            sa.Column('details', sa.String(500), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()))
    if 'operation_authorizations' not in tables:
        op.create_table('operation_authorizations',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('app_users.id'), nullable=False),
            sa.Column('operation', sa.String(80), nullable=False),
            sa.Column('target', sa.String(120), nullable=False),
            sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('used_at', sa.DateTime(), nullable=True),
            sa.Column('ip', sa.String(64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()))
    for name, table, column, unique in [
        ('ix_audit_log_user_id', 'audit_log', 'user_id', False), ('ix_audit_log_action', 'audit_log', 'action', False), ('ix_audit_log_outcome', 'audit_log', 'outcome', False),
        ('ix_operation_authorizations_user_id', 'operation_authorizations', 'user_id', False), ('ix_operation_authorizations_token_hash', 'operation_authorizations', 'token_hash', True)]:
        existing = {i['name'] for i in sa.inspect(op.get_bind()).get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, [column], unique=unique)

def downgrade():
    # Audit history must survive a schema rollback, like the earlier authentication tables.
    pass
