"""traffic dimensions: registry, per-subscription state, tariff config, samples

Revision ID: xb0002
Revises: xb0001
Create Date: 2026-07-31

Fork-local migration (branch ``xbedolaga``) — see ``xb0001`` for why fork
migrations live in the ``xbNNNN`` id space and why they must be applied with
``alembic upgrade heads`` (plural).

Turns WL from a hardcoded pair of columns into an admin-defined traffic
dimension:

* ``traffic_dimensions`` — the registry. Row ``base`` describes ordinary
  traffic and is a pointer, not storage: its state stays in
  ``subscriptions.traffic_*``. Any further row is created by an admin.
* ``subscription_traffic_dimensions`` — per-subscription state for non-base
  dimensions. ``subscriptions.wl_traffic_limit_gb`` / ``wl_traffic_used_gb``
  are migrated into it and then dropped.
* ``tariff_traffic_dimensions`` — what a tariff includes and sells per
  dimension. No row means the tariff does not offer that dimension at all.
* ``traffic_dimension_samples`` — the bot's own copy of the panel's per-inbound
  daily counters. Required, not optional: the panel TRUNCATEs its per-inbound
  history every Monday 00:30 UTC, so any billing window longer than a week
  cannot be reconstructed from the panel.
* ``traffic_purchases.dimension`` — discriminator so one ledger serves every
  dimension. The composite index is re-cut to keep the housekeeping queries
  covered.

Idempotent throughout: safe to re-run against a partially migrated database.
"""

import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'xb0002'
down_revision: Union[str, None] = 'xb0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BASE_KEY = 'base'
WL_KEY = 'wl'


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


def upgrade() -> None:
    tables = _tables()

    if 'traffic_dimensions' not in tables:
        op.create_table(
            'traffic_dimensions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('key', sa.String(length=32), nullable=False, unique=True),
            sa.Column('title', sa.JSON(), nullable=True),
            sa.Column('fallback_title', sa.String(length=64), nullable=False, server_default=''),
            sa.Column('icon', sa.String(length=8), nullable=False, server_default=''),
            sa.Column('inbound_uuids', sa.JSON(), nullable=True),
            sa.Column('default_limit_gb', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('accounting_mode', sa.String(length=16), nullable=True),
            sa.Column('enforcement', sa.String(length=16), nullable=False, server_default='squad_strip'),
            sa.Column('discount_category', sa.String(length=32), nullable=True),
            sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('is_builtin', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            'ix_traffic_dimensions_enabled_position',
            'traffic_dimensions',
            ['is_enabled', 'position'],
        )

    if 'subscription_traffic_dimensions' not in tables:
        op.create_table(
            'subscription_traffic_dimensions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column(
                'subscription_id',
                sa.Integer(),
                sa.ForeignKey('subscriptions.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column(
                'dimension_id',
                sa.Integer(),
                sa.ForeignKey('traffic_dimensions.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('base_limit_gb', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('purchased_gb', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('used_gb', sa.Float(), nullable=False, server_default='0'),
            sa.Column('window_start', sa.Date(), nullable=True),
            sa.Column('coverage_from', sa.Date(), nullable=True),
            sa.Column('measured_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('measured_known', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('blocked_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('block_reason', sa.String(length=32), nullable=True),
            sa.Column('stripped_squads', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint('subscription_id', 'dimension_id', name='uq_subscription_traffic_dimension'),
        )
        op.create_index(
            'ix_subscription_traffic_dimensions_blocked',
            'subscription_traffic_dimensions',
            ['blocked_at'],
        )

    if 'tariff_traffic_dimensions' not in tables:
        op.create_table(
            'tariff_traffic_dimensions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('tariff_id', sa.Integer(), sa.ForeignKey('tariffs.id', ondelete='CASCADE'), nullable=False),
            sa.Column(
                'dimension_id',
                sa.Integer(),
                sa.ForeignKey('traffic_dimensions.id', ondelete='CASCADE'),
                nullable=False,
            ),
            sa.Column('included_gb', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('topup_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('custom_enabled', sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column('price_per_gb_kopeks', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('min_gb', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('max_gb', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('max_topup_gb', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint('tariff_id', 'dimension_id', name='uq_tariff_traffic_dimension'),
        )

    if 'traffic_dimension_samples' not in tables:
        op.create_table(
            'traffic_dimension_samples',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('remnawave_uuid', sa.String(length=36), nullable=False),
            sa.Column('inbound_uuid', sa.String(length=36), nullable=False),
            sa.Column('usage_date', sa.Date(), nullable=False),
            sa.Column('bytes', sa.BigInteger(), nullable=False, server_default='0'),
            sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                'remnawave_uuid',
                'inbound_uuid',
                'usage_date',
                name='uq_traffic_dimension_sample',
            ),
        )
        op.create_index(
            'ix_traffic_dimension_samples_uuid_date',
            'traffic_dimension_samples',
            ['remnawave_uuid', 'usage_date'],
        )

    if 'coverage_from' not in _columns('subscription_traffic_dimensions'):
        # Догоняющая ветка: таблица уже создана более ранним прогоном этой ревизии.
        op.add_column('subscription_traffic_dimensions', sa.Column('coverage_from', sa.Date(), nullable=True))

    # --- traffic_purchases: dimension discriminator ---------------------------
    if 'dimension' not in _columns('traffic_purchases'):
        op.add_column(
            'traffic_purchases',
            sa.Column('dimension', sa.String(length=32), nullable=False, server_default=BASE_KEY),
        )
    purchase_indexes = _indexes('traffic_purchases')
    if 'ix_traffic_purchases_sub_dim_expires' not in purchase_indexes:
        op.create_index(
            'ix_traffic_purchases_sub_dim_expires',
            'traffic_purchases',
            ['subscription_id', 'dimension', 'expires_at'],
        )
    if 'ix_traffic_purchases_sub_expires' in purchase_indexes:
        # Заменён композитным с dimension: старый стал избыточным префиксом.
        op.drop_index('ix_traffic_purchases_sub_expires', table_name='traffic_purchases')

    _seed_dimensions()
    _migrate_wl_state()

    # WL-колонки переехали в subscription_traffic_dimensions — снимаем дубль,
    # чтобы не было двух источников правды об одном и том же лимите.
    subscription_columns = _columns('subscriptions')
    if 'wl_traffic_used_gb' in subscription_columns:
        op.drop_column('subscriptions', 'wl_traffic_used_gb')
    if 'wl_traffic_limit_gb' in subscription_columns:
        op.drop_column('subscriptions', 'wl_traffic_limit_gb')


def _seed_dimensions() -> None:
    """Заводит `base` и переносит существующую WL-настройку в строку `wl`."""
    bind = op.get_bind()

    existing = {row[0] for row in bind.execute(sa.text('SELECT key FROM traffic_dimensions')).fetchall()}

    if BASE_KEY not in existing:
        bind.execute(
            sa.text(
                """
                INSERT INTO traffic_dimensions
                    (key, title, fallback_title, icon, inbound_uuids, default_limit_gb,
                     enforcement, is_enabled, is_builtin, position)
                VALUES
                    (:key, :title, :fallback, :icon, :inbounds, 0,
                     'panel_limit', true, true, 0)
                """
            ),
            {
                'key': BASE_KEY,
                'title': '{"ru": "Трафик", "en": "Traffic"}',
                'fallback': 'Трафик',
                'icon': '📊',
                'inbounds': '[]',
            },
        )

    if WL_KEY in existing:
        return

    # WL заводим только если он реально настроен: пустая строка со включённым
    # enforcement — это ловушка, а не «выключенная фича».
    wl_inbounds = _configured_wl_inbounds()
    if not wl_inbounds:
        return

    import json

    bind.execute(
        sa.text(
            """
            INSERT INTO traffic_dimensions
                (key, title, fallback_title, icon, inbound_uuids, default_limit_gb,
                 enforcement, is_enabled, is_builtin, position)
            VALUES
                (:key, :title, :fallback, :icon, :inbounds, :default_limit,
                 'squad_strip', :enabled, false, 1)
            """
        ),
        {
            'key': WL_KEY,
            'title': '{"ru": "WL Трафик (БС)", "en": "WL Traffic"}',
            'fallback': 'WL Трафик (БС)',
            'icon': '⚪',
            'inbounds': json.dumps(wl_inbounds),
            'default_limit': _configured_wl_default_limit(),
            'enabled': _configured_wl_enabled(),
        },
    )


def _legacy_setting(name: str) -> str | None:
    """Читает снятую с вооружения настройку из БД, затем из окружения.

    Намеренно не импортирует `app.config`: этих полей там больше нет — их
    заменили строки `traffic_dimensions`, — а миграция обязана уметь поднять
    старую конфигурацию у тех, кто обновляется. Настройка могла быть
    переопределена через админку (таблица `system_settings`), поэтому БД
    важнее окружения.
    """
    bind = op.get_bind()
    if 'system_settings' in _tables():
        try:
            row = bind.execute(
                sa.text('SELECT value FROM system_settings WHERE key = :key'),
                {'key': name},
            ).fetchone()
            if row is not None and row[0] is not None:
                return str(row[0])
        except Exception:  # noqa: BLE001 — схема system_settings у форков может отличаться
            pass
    return os.environ.get(name)


def _configured_wl_inbounds() -> list:
    raw = _legacy_setting('WL_INBOUND_UUIDS') or ''
    value = raw.split('#', 1)[0].strip()
    if not value:
        return []
    return [item.strip().lower() for item in value.split(',') if item.strip()]


def _configured_wl_default_limit() -> int:
    try:
        return int((_legacy_setting('WL_TRAFFIC_DEFAULT_LIMIT_GB') or '0').strip() or 0)
    except ValueError:
        return 0


def _configured_wl_enabled() -> bool:
    return (_legacy_setting('WL_TRAFFIC_ENABLED') or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _migrate_wl_state() -> None:
    """Переносит подписочные WL-колонки в строки состояния измерения."""
    bind = op.get_bind()

    if 'wl_traffic_limit_gb' not in _columns('subscriptions'):
        return

    wl_row = bind.execute(
        sa.text('SELECT id FROM traffic_dimensions WHERE key = :key'),
        {'key': WL_KEY},
    ).fetchone()
    if wl_row is None:
        return

    bind.execute(
        sa.text(
            """
            INSERT INTO subscription_traffic_dimensions
                (subscription_id, dimension_id, base_limit_gb, purchased_gb, used_gb, measured_known)
            SELECT s.id, :dimension_id, COALESCE(s.wl_traffic_limit_gb, 0), 0,
                   COALESCE(s.wl_traffic_used_gb, 0), false
            FROM subscriptions s
            WHERE COALESCE(s.wl_traffic_limit_gb, 0) <> 0
               OR COALESCE(s.wl_traffic_used_gb, 0) <> 0
            ON CONFLICT ON CONSTRAINT uq_subscription_traffic_dimension DO NOTHING
            """
        ),
        {'dimension_id': wl_row[0]},
    )


def downgrade() -> None:
    subscription_columns = _columns('subscriptions')
    if 'wl_traffic_limit_gb' not in subscription_columns:
        op.add_column(
            'subscriptions',
            sa.Column('wl_traffic_limit_gb', sa.Integer(), server_default='0', nullable=False),
        )
    if 'wl_traffic_used_gb' not in subscription_columns:
        op.add_column(
            'subscriptions',
            sa.Column('wl_traffic_used_gb', sa.Float(), server_default='0', nullable=False),
        )

    bind = op.get_bind()
    if 'subscription_traffic_dimensions' in _tables():
        bind.execute(
            sa.text(
                """
                UPDATE subscriptions s
                SET wl_traffic_limit_gb = std.base_limit_gb + std.purchased_gb,
                    wl_traffic_used_gb = std.used_gb
                FROM subscription_traffic_dimensions std
                JOIN traffic_dimensions td ON td.id = std.dimension_id
                WHERE std.subscription_id = s.id AND td.key = :key
                """
            ),
            {'key': WL_KEY},
        )

    purchase_indexes = _indexes('traffic_purchases')
    if 'ix_traffic_purchases_sub_expires' not in purchase_indexes:
        op.create_index(
            'ix_traffic_purchases_sub_expires',
            'traffic_purchases',
            ['subscription_id', 'expires_at'],
        )
    if 'ix_traffic_purchases_sub_dim_expires' in purchase_indexes:
        op.drop_index('ix_traffic_purchases_sub_dim_expires', table_name='traffic_purchases')
    if 'dimension' in _columns('traffic_purchases'):
        op.drop_column('traffic_purchases', 'dimension')

    for table in (
        'traffic_dimension_samples',
        'tariff_traffic_dimensions',
        'subscription_traffic_dimensions',
        'traffic_dimensions',
    ):
        if table in _tables():
            op.drop_table(table)
