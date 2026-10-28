"""Create the initial tables

Revision ID: 0001
Revises: 
Create Date: 2026-10-28 11:50:49.480753

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# A copy, not an import: a later change to models.TRIGGERS needs its own revision.
TRIGGERS = (
    """CREATE TRIGGER agent_owner_is_fixed BEFORE UPDATE OF owner_id ON agent
WHEN NEW.owner_id IS NOT OLD.owner_id
BEGIN SELECT RAISE(ABORT, 'agent.owner_id is fixed'); END""",
    """CREATE TRIGGER agent_owner_is_human BEFORE INSERT ON agent
WHEN NOT EXISTS (SELECT 1 FROM human WHERE user_id = NEW.owner_id)
BEGIN SELECT RAISE(ABORT, 'an agent is owned by a human'); END""",
    """CREATE TRIGGER agent_user_is_not_human BEFORE INSERT ON agent
WHEN EXISTS (SELECT 1 FROM human WHERE user_id = NEW.user_id)
BEGIN SELECT RAISE(ABORT, 'a user has one profile'); END""",
    """CREATE TRIGGER human_user_is_not_agent BEFORE INSERT ON human
WHEN EXISTS (SELECT 1 FROM agent WHERE user_id = NEW.user_id)
BEGIN SELECT RAISE(ABORT, 'a user has one profile'); END""",
    """CREATE TRIGGER agent_name_is_free BEFORE INSERT ON agent
WHEN EXISTS (
  SELECT 1 FROM user AS mine, user AS other
  WHERE mine.id = NEW.user_id
    AND other.name = mine.name COLLATE NOCASE
    AND (other.id = NEW.owner_id
      OR other.id IN (SELECT user_id FROM agent WHERE owner_id = NEW.owner_id))
)
BEGIN SELECT RAISE(ABORT, 'name is taken in this account'); END""",
    """CREATE TRIGGER user_name_is_free BEFORE UPDATE OF name ON user
WHEN NEW.name IS NOT OLD.name AND EXISTS (
  SELECT 1 FROM user AS other
  WHERE other.id != NEW.id
    AND other.name = NEW.name COLLATE NOCASE
    AND (other.id = COALESCE((SELECT owner_id FROM agent WHERE user_id = NEW.id), NEW.id)
      OR other.id IN (
        SELECT user_id FROM agent
        WHERE owner_id = COALESCE((SELECT owner_id FROM agent WHERE user_id = NEW.id), NEW.id)
      ))
)
BEGIN SELECT RAISE(ABORT, 'name is taken in this account'); END""",
    """CREATE TRIGGER token_is_fixed BEFORE UPDATE OF user_id, secret_hash, expires_at ON token
WHEN NEW.user_id IS NOT OLD.user_id
  OR NEW.secret_hash IS NOT OLD.secret_hash
  OR NEW.expires_at IS NOT OLD.expires_at
BEGIN SELECT RAISE(ABORT, 'a session row is fixed'); END""",
    """CREATE TRIGGER task_owner_is_human BEFORE INSERT ON task
WHEN NOT EXISTS (SELECT 1 FROM human WHERE user_id = NEW.owned_by)
BEGIN SELECT RAISE(ABORT, 'a task is owned by a human'); END""",
    """CREATE TRIGGER task_is_fixed BEFORE UPDATE OF owned_by, created_by, created_at ON task
WHEN NEW.owned_by IS NOT OLD.owned_by
  OR NEW.created_at IS NOT OLD.created_at
  OR (NEW.created_by IS NOT OLD.created_by AND NOT (
    NEW.created_by = NEW.owned_by
    AND EXISTS (SELECT 1 FROM agent WHERE user_id = OLD.created_by AND owner_id = NEW.owned_by)
  ))
BEGIN SELECT RAISE(ABORT, 'a task keeps its owner and creation'); END""",
    """CREATE TRIGGER task_parent_in_account BEFORE INSERT ON task
WHEN NEW.parent_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM task WHERE id = NEW.parent_id AND owned_by = NEW.owned_by)
BEGIN SELECT RAISE(ABORT, 'a parent is in the same account'); END""",
    """CREATE TRIGGER task_parent_in_account_on_move BEFORE UPDATE OF parent_id ON task
WHEN NEW.parent_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM task WHERE id = NEW.parent_id AND owned_by = NEW.owned_by)
BEGIN SELECT RAISE(ABORT, 'a parent is in the same account'); END""",
    """CREATE TRIGGER task_status_is_held_by_a_block BEFORE UPDATE OF status ON task
WHEN NEW.status IS NOT OLD.status
  AND EXISTS (SELECT 1 FROM block WHERE task_id = NEW.id AND released_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'a blocked task keeps its status'); END""",
    """CREATE TRIGGER block_on_an_open_task BEFORE INSERT ON block
WHEN (SELECT status FROM task WHERE id = NEW.task_id) NOT IN (0, 1)
BEGIN SELECT RAISE(ABORT, 'a block is added only to an open task'); END""",
    """CREATE TRIGGER block_release_is_final BEFORE UPDATE OF released_at, released_by ON block
WHEN OLD.released_at IS NOT NULL AND (
  NEW.released_at IS NOT OLD.released_at
  OR (NEW.released_by IS NOT OLD.released_by AND NOT (
    NEW.released_by = (SELECT owned_by FROM task WHERE id = NEW.task_id)
    AND EXISTS (
      SELECT 1 FROM agent WHERE user_id = OLD.released_by
        AND owner_id = (SELECT owned_by FROM task WHERE id = NEW.task_id)
    )
  ))
)
BEGIN SELECT RAISE(ABORT, 'a released block stays released'); END""",
    """CREATE TRIGGER block_goes_with_its_task BEFORE DELETE ON block
WHEN EXISTS (SELECT 1 FROM task WHERE id = OLD.task_id)
BEGIN SELECT RAISE(ABORT, 'a block is deleted only with its task'); END""",
    """CREATE TRIGGER task_history_goes_with_its_task BEFORE DELETE ON task_history
WHEN EXISTS (SELECT 1 FROM task WHERE id = OLD.task_id)
BEGIN SELECT RAISE(ABORT, 'a history row is deleted only with its task'); END""",
    """CREATE TRIGGER block_is_fixed BEFORE UPDATE OF task_id, created_at ON block
WHEN NEW.task_id IS NOT OLD.task_id OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a block stays on its task'); END""",
    """CREATE TRIGGER task_history_is_fixed BEFORE UPDATE OF task_id, created_at, state
ON task_history
WHEN NEW.task_id IS NOT OLD.task_id
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.state IS NOT OLD.state
BEGIN SELECT RAISE(ABORT, 'a history row is fixed'); END""",
)


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('user',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_name'), ['name'], unique=False)

    op.create_table('agent',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=False),
    sa.Column('key_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('key_expires_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.Column('retired_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.CheckConstraint('(key_hash IS NULL) = (key_expires_at IS NULL)', name='ck_agent_key_pair'),
    sa.CheckConstraint('retired_at IS NULL OR key_hash IS NULL', name='ck_agent_retired_without_key'),
    sa.ForeignKeyConstraint(['owner_id'], ['user.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('user_id'),
    sa.UniqueConstraint('key_hash')
    )
    with op.batch_alter_table('agent', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_owner_id'), ['owner_id'], unique=False)

    op.create_table('human',
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('email', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('password_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('timezone', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.CheckConstraint('email = lower(email)', name='ck_human_email_lowercase'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('user_id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('task',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('title', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('title_key', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('notes', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('tags', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    sa.Column('status', sa.SmallInteger(), server_default='0', nullable=False),
    sa.Column('estimate_minutes', sa.SmallInteger(), nullable=True),
    sa.Column('due_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('updated_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('updated_by', sa.Integer(), nullable=False),
    sa.Column('owned_by', sa.Integer(), nullable=False),
    sa.CheckConstraint("estimate_minutes IS NULL OR (typeof(estimate_minutes) = 'integer' AND estimate_minutes BETWEEN 0 AND 32767)", name='ck_task_estimate_minutes'),
    sa.CheckConstraint("trim(title, ' ' || char(9) || char(10) || char(13)) != ''", name='ck_task_title'),
    sa.CheckConstraint("typeof(status) = 'integer' AND status IN (0, 1, 2, 3)", name='ck_task_status'),
    sa.ForeignKeyConstraint(['created_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['owned_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['parent_id'], ['task.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_task_owned_by'), ['owned_by'], unique=False)
        batch_op.create_index('ix_task_owned_by_title_key', ['owned_by', 'title_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_task_parent_id'), ['parent_id'], unique=False)

    op.create_table('token',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('secret_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('expires_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('secret_hash'),
    sa.UniqueConstraint('user_id')
    )
    op.create_table('block',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('released_by', sa.Integer(), nullable=True),
    sa.Column('released_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
    sa.CheckConstraint("trim(reason, ' ' || char(9) || char(10) || char(13)) != '' AND instr(reason, char(10)) = 0 AND instr(reason, char(13)) = 0", name='ck_block_reason'),
    sa.CheckConstraint('(released_at IS NULL) = (released_by IS NULL)', name='ck_block_release_pair'),
    sa.ForeignKeyConstraint(['created_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['released_by'], ['user.id'], ),
    sa.ForeignKeyConstraint(['task_id'], ['task.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('block', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_block_task_id'), ['task_id'], unique=False)

    op.create_table('task_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
    sa.Column('state', sa.JSON(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['task.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_history', schema=None) as batch_op:
        batch_op.create_index('ix_task_history_task_id_created_at', ['task_id', 'created_at'], unique=False)

    # ### end Alembic commands ###
    for trigger in TRIGGERS:
        op.execute(trigger)


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('task_history', schema=None) as batch_op:
        batch_op.drop_index('ix_task_history_task_id_created_at')

    op.drop_table('task_history')
    with op.batch_alter_table('block', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_block_task_id'))

    op.drop_table('block')
    op.drop_table('token')
    with op.batch_alter_table('task', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_task_parent_id'))
        batch_op.drop_index('ix_task_owned_by_title_key')
        batch_op.drop_index(batch_op.f('ix_task_owned_by'))

    op.drop_table('task')
    op.drop_table('human')
    with op.batch_alter_table('agent', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agent_owner_id'))

    op.drop_table('agent')
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_name'))

    op.drop_table('user')
    # ### end Alembic commands ###
