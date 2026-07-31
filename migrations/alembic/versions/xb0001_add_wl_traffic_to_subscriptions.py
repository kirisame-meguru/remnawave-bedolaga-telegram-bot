"""add wl (whitelist / БС) traffic columns to subscriptions

Revision ID: xb0001
Revises: 0098
Create Date: 2026-07-21

Fork-local migration (branch ``xbedolaga``).

This was originally authored as revision '0099' and collided head-on with
upstream's '0099_add_platega_subscriptions': git merged cleanly because the
filenames differ, but alembic saw a duplicate revision id and refused to start
with MultipleHeads. Worse, the DB was stamped '0099', so once the duplicate was
removed alembic would have treated upstream's 0099 as already applied and
silently skipped it.

Fork migrations therefore live in the ``xbNNNN`` id space, which upstream's
sequential ``NNNN`` scheme can never generate, and hang off the fork point
(0098) as their own branch instead of being interleaved into upstream's chain.
That branch stays put no matter how far upstream's chain advances, so an
upstream sync can never collide with it and never needs it re-chained.

Because the graph has two heads, migrations must be applied with
``alembic upgrade heads`` (plural) — see ``app/database/migrations.py``.

Idempotent: databases that already applied the old '0099' already have these
columns.

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'xb0001'
down_revision: Union[str, None] = '0098'
branch_labels: Union[str, Sequence[str], None] = ('xbedolaga',)
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set:
    bind = op.get_bind()
    return {c['name'] for c in sa.inspect(bind).get_columns('subscriptions')}


def upgrade() -> None:
    existing = _columns()
    if 'wl_traffic_limit_gb' not in existing:
        op.add_column(
            'subscriptions',
            sa.Column('wl_traffic_limit_gb', sa.Integer(), server_default='0', nullable=False),
        )
    if 'wl_traffic_used_gb' not in existing:
        op.add_column(
            'subscriptions',
            sa.Column('wl_traffic_used_gb', sa.Float(), server_default='0', nullable=False),
        )


def downgrade() -> None:
    existing = _columns()
    if 'wl_traffic_used_gb' in existing:
        op.drop_column('subscriptions', 'wl_traffic_used_gb')
    if 'wl_traffic_limit_gb' in existing:
        op.drop_column('subscriptions', 'wl_traffic_limit_gb')
