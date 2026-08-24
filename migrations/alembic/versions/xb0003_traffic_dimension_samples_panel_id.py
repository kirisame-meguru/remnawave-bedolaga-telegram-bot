"""traffic dimension samples: re-key from panel uuid to numeric panel user id

Revision ID: xb0003
Revises: xb0002
Create Date: 2026-08-24

Fork-local migration (branch ``xbedolaga``) — see ``xb0001`` for why fork
migrations live in the ``xbNNNN`` id space and why they must be applied with
``alembic upgrade heads`` (plural).

Remnawave 3.0.0 removed ``uuid`` from UsersSchema: a panel user record is
identified by its numeric ``id`` only, and upstream revision ``0104`` moved
every local identity column to ``remnawave_id``. The fork's own per-inbound
sample journal was still keyed by the panel uuid — a value the panel no longer
reports at all, so every new observation would have been unattributable and the
quota window unreadable.

Existing observations are re-keyed through the identity the upstream backfill
has already resolved (``subscriptions.remnawave_id``, then ``users.remnawave_id``
for rows that predate multi-tariff). Rows whose panel user cannot be resolved
are deleted: the journal is read strictly by numeric id, so such a row is dead
weight that would also block the NOT NULL constraint. Dropping it only shortens
the covered window, which the ledger already reports as a coverage gap (and a
coverage gap holds enforcement instead of handing out free quota).

Idempotent: safe to re-run against a partially migrated database.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'xb0003'
down_revision: Union[str, None] = 'xb0002'
branch_labels: Union[str, Sequence[str], None] = None
# Ordering edge, not a re-chaining: ``0104`` is what fills ``remnawave_id`` on
# subscriptions and users, and this revision reads exactly that to re-key its
# own rows. Without the edge alembic is free to run the fork branch first on a
# database that upgrades from before 3.0.0, and the re-key would find no source
# column at all. ``depends_on`` adds the edge without touching ``down_revision``,
# so the fork branch keeps its own head (see docs/fork-migrations.md).
depends_on: Union[str, Sequence[str], None] = ('0104',)


TABLE = 'traffic_dimension_samples'
UNIQUE = 'uq_traffic_dimension_sample'
UUID_INDEX = 'ix_traffic_dimension_samples_uuid_date'
PANEL_INDEX = 'ix_traffic_dimension_samples_panel_date'


def _inspector():
    return sa.inspect(op.get_bind())


def _tables() -> set:
    return set(_inspector().get_table_names())


def _columns(table: str) -> set:
    inspector = _inspector()
    if table not in set(inspector.get_table_names()):
        return set()
    return {c['name'] for c in inspector.get_columns(table)}


def _indexes(table: str) -> set:
    inspector = _inspector()
    if table not in set(inspector.get_table_names()):
        return set()
    return {i['name'] for i in inspector.get_indexes(table)}


def _identity_sources(*, into: str, using: str) -> list[str]:
    """Таблицы, из которых можно дотянуть идентичность, в порядке доверия."""
    return [table for table in ('subscriptions', 'users') if not {into, using} - _columns(table)]


def _copy_identity(source: str, *, into: str, using: str) -> None:
    """Дотягивает идентичность из таблицы, где она уже восстановлена.

    Коррелированный подзапрос с LIMIT 1 работает и в PostgreSQL, и в SQLite;
    дубликаты по ключу возможны (одна панельная запись на несколько подписок),
    и любой из них даёт один и тот же id.
    """
    op.execute(
        sa.text(
            f"""
            UPDATE {TABLE}
            SET {into} = (
                SELECT src.{into}
                FROM {source} AS src
                WHERE src.{using} = {TABLE}.{using}
                  AND src.{into} IS NOT NULL
                LIMIT 1
            )
            WHERE {into} IS NULL
            """
        )
    )


def upgrade() -> None:
    if TABLE not in _tables():
        return
    columns = _columns(TABLE)
    if 'remnawave_id' in columns or 'remnawave_uuid' not in columns:
        return

    sources = _identity_sources(into='remnawave_id', using='remnawave_uuid')
    if not sources:
        # Ни одной таблицы с обеими колонками: восстанавливать нечем, а стирать
        # наблюдения на этом основании нельзя — это молчаливая потеря данных.
        raise RuntimeError(
            'xb0003: не найдено ни subscriptions, ни users с колонками '
            'remnawave_id + remnawave_uuid — примените upstream-ревизию 0104 первой'
        )

    op.add_column(TABLE, sa.Column('remnawave_id', sa.BigInteger(), nullable=True))

    for source in sources:
        _copy_identity(source, into='remnawave_id', using='remnawave_uuid')

    op.execute(sa.text(f'DELETE FROM {TABLE} WHERE remnawave_id IS NULL'))

    if UUID_INDEX in _indexes(TABLE):
        op.drop_index(UUID_INDEX, table_name=TABLE)

    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(UNIQUE, type_='unique')
        batch.alter_column('remnawave_id', existing_type=sa.BigInteger(), nullable=False)
        batch.drop_column('remnawave_uuid')
        batch.create_unique_constraint(UNIQUE, ['remnawave_id', 'inbound_uuid', 'usage_date'])

    if PANEL_INDEX not in _indexes(TABLE):
        op.create_index(PANEL_INDEX, TABLE, ['remnawave_id', 'usage_date'])


def downgrade() -> None:
    if TABLE not in _tables():
        return
    columns = _columns(TABLE)
    if 'remnawave_uuid' in columns or 'remnawave_id' not in columns:
        return

    op.add_column(TABLE, sa.Column('remnawave_uuid', sa.String(length=36), nullable=True))

    for source in _identity_sources(into='remnawave_uuid', using='remnawave_id'):
        _copy_identity(source, into='remnawave_uuid', using='remnawave_id')

    # Обратный ключ восстанавливается только там, где uuid ещё хранится как
    # исторические данные; остальное на старой схеме нечитаемо.
    op.execute(sa.text(f'DELETE FROM {TABLE} WHERE remnawave_uuid IS NULL'))

    if PANEL_INDEX in _indexes(TABLE):
        op.drop_index(PANEL_INDEX, table_name=TABLE)

    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(UNIQUE, type_='unique')
        batch.alter_column('remnawave_uuid', existing_type=sa.String(length=36), nullable=False)
        batch.drop_column('remnawave_id')
        batch.create_unique_constraint(UNIQUE, ['remnawave_uuid', 'inbound_uuid', 'usage_date'])

    if UUID_INDEX not in _indexes(TABLE):
        op.create_index(UUID_INDEX, TABLE, ['remnawave_uuid', 'usage_date'])
