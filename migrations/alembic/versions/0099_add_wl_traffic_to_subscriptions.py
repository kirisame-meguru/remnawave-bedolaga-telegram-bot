"""add wl (whitelist / БС) traffic columns to subscriptions

Revision ID: 0099
Revises: 0098
Create Date: 2026-07-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0099'
down_revision: Union[str, None] = '0098'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'subscriptions',
        sa.Column('wl_traffic_limit_gb', sa.Integer(), server_default='0', nullable=False),
    )
    op.add_column(
        'subscriptions',
        sa.Column('wl_traffic_used_gb', sa.Float(), server_default='0', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('subscriptions', 'wl_traffic_used_gb')
    op.drop_column('subscriptions', 'wl_traffic_limit_gb')
