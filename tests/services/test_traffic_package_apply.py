"""Начисление пакетов на живой БД: два счёта в одной таблице не должны смешиваться.

Самое важное здесь — изоляция. Докупка измерения не имеет права влиять на
`traffic_limit_gb` обычного трафика, а истечение WL-пакета — ронять обычный
лимит подписки. Обе ошибки тихие: пользователь просто однажды обнаруживает,
что оплаченного трафика стало меньше.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import (
    BASE_TRAFFIC_DIMENSION_KEY,
    Subscription,
    SubscriptionTrafficDimension,
    Tariff,
    TariffTrafficDimension,
    TrafficDimension,
    TrafficPurchase,
    User,
)
from app.services.traffic_dimensions import (
    expire_dimension_purchases,
    grant_dimension_traffic,
    traffic_dimensions,
)
from app.services.traffic_package_service import apply_traffic_package
from app.services.traffic_packages import parse_packages
from tests.fixtures.sqlite_memory import memory_session
from tests.services.test_traffic_dimension_ledger import make_spec


TABLES = [
    User.__table__,
    Tariff.__table__,
    Subscription.__table__,
    TrafficDimension.__table__,
    SubscriptionTrafficDimension.__table__,
    TariffTrafficDimension.__table__,
    TrafficPurchase.__table__,
]

SPEC = make_spec(key='wl', inbounds=('aaa',))


async def seed(db, *, traffic_limit_gb=100) -> Subscription:
    db.add(
        TrafficDimension(
            id=SPEC.id,
            key=SPEC.key,
            title=SPEC.titles,
            fallback_title=SPEC.fallback_title,
            icon=SPEC.icon,
            inbound_uuids=sorted(SPEC.inbound_uuids),
            default_limit_gb=SPEC.default_limit_gb,
            enforcement=SPEC.enforcement,
            is_enabled=True,
            is_builtin=False,
            position=SPEC.position,
        )
    )
    user = User(telegram_id=1, username='u', first_name='U', language='ru')
    db.add(user)
    await db.flush()
    subscription = Subscription(
        user_id=user.id,
        status='active',
        start_date=datetime(2026, 3, 1, tzinfo=UTC),
        end_date=datetime(2026, 4, 1, tzinfo=UTC),
        remnawave_uuid='u-1',
        connected_squads=['sq-wl'],
        traffic_limit_gb=traffic_limit_gb,
        purchased_traffic_gb=0,
    )
    db.add(subscription)
    await db.flush()
    traffic_dimensions.invalidate()
    return subscription


async def dimension_row(db, subscription) -> SubscriptionTrafficDimension:
    from app.services.traffic_dimensions import load_dimension_rows

    rows = await load_dimension_rows(db, subscription.id)
    return rows.get(SPEC.id)


async def purchases(db, dimension=None) -> list:
    query = TrafficPurchase.__table__.select()
    rows = (await db.execute(query)).mappings().all()
    if dimension is None:
        return list(rows)
    return [row for row in rows if row['dimension'] == dimension]


# ------------------------------ виды пакетов ------------------------------


@pytest.mark.asyncio
async def test_dimension_only_package_leaves_base_traffic_alone(monkeypatch):
    """WL-пакет не имеет права трогать обычный лимит."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db, traffic_limit_gb=100)
        package = parse_packages([{'id': 'wl10', 'price_kopeks': 9900, 'grants': [{'dim': 'wl', 'gb': 10}]}])[0]

        result = await apply_traffic_package(db, subscription, package)
        await db.commit()

        assert result.applied_any
        assert subscription.traffic_limit_gb == 100, 'обычный лимит не изменился'
        assert subscription.purchased_traffic_gb == 0
        row = await dimension_row(db, subscription)
        assert row.purchased_gb == 10
        assert await purchases(db, BASE_TRAFFIC_DIMENSION_KEY) == []
        assert len(await purchases(db, 'wl')) == 1


@pytest.mark.asyncio
async def test_base_only_package_uses_the_legacy_path(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db, traffic_limit_gb=100)
        package = parse_packages({'50': 4900})[0]

        await apply_traffic_package(db, subscription, package)
        await db.commit()

        assert subscription.traffic_limit_gb == 150
        assert subscription.purchased_traffic_gb == 50
        assert len(await purchases(db, BASE_TRAFFIC_DIMENSION_KEY)) == 1
        assert await purchases(db, 'wl') == []


@pytest.mark.asyncio
async def test_mixed_package_grants_both_and_splits_the_price(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db, traffic_limit_gb=100)
        package = parse_packages(
            [
                {
                    'id': 'mix',
                    'price_kopeks': 15000,
                    'grants': [{'dim': 'base', 'gb': 10}, {'dim': 'wl', 'gb': 5}],
                }
            ]
        )[0]

        result = await apply_traffic_package(db, subscription, package)
        await db.commit()

        assert subscription.traffic_limit_gb == 110
        assert (await dimension_row(db, subscription)).purchased_gb == 5
        shares = [item.price_share_kopeks for item in result.grants]
        assert shares == [10000, 5000]
        assert sum(shares) == package.price_kopeks, 'сумма долей обязана сходиться с ценой'


@pytest.mark.asyncio
async def test_grant_for_unknown_dimension_is_skipped_not_fatal(monkeypatch):
    """Измерение могли удалить после настройки пакета — остальное честно."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db, traffic_limit_gb=100)
        package = parse_packages(
            [
                {
                    'id': 'mix',
                    'price_kopeks': 15000,
                    'grants': [{'dim': 'base', 'gb': 10}, {'dim': 'ghost', 'gb': 5}],
                }
            ]
        )[0]

        result = await apply_traffic_package(db, subscription, package)
        await db.commit()

        assert subscription.traffic_limit_gb == 110, 'обычная часть начислена'
        skipped = [item for item in result.grants if not item.applied]
        assert len(skipped) == 1
        assert skipped[0].skipped_reason == 'unknown_dimension'


@pytest.mark.asyncio
async def test_unlimited_base_grant_clears_base_purchases_only(monkeypatch):
    """Переход на безлимит не должен сжигать оплаченные пакеты измерений."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db, traffic_limit_gb=100)
        await grant_dimension_traffic(db, subscription, SPEC, 10)
        await apply_traffic_package(db, subscription, parse_packages({'50': 4900})[0])
        await db.commit()
        assert subscription.purchased_traffic_gb == 50

        unlimited = parse_packages([{'id': 'unl', 'price_kopeks': 50000, 'grants': [{'dim': 'base', 'gb': 0}]}])[0]
        await apply_traffic_package(db, subscription, unlimited)
        await db.commit()

        assert subscription.traffic_limit_gb == 0
        assert subscription.purchased_traffic_gb == 0
        assert await purchases(db, BASE_TRAFFIC_DIMENSION_KEY) == []
        assert len(await purchases(db, 'wl')) == 1, 'квота измерения оплачена и остаётся'
        assert (await dimension_row(db, subscription)).purchased_gb == 10


# ------------------------------ изоляция счетов ------------------------------


@pytest.mark.asyncio
async def test_dimension_purchase_does_not_inflate_base_limit(monkeypatch):
    """Ровно та ошибка, ради которой в таблице появился дискриминатор."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db, traffic_limit_gb=100)

        await grant_dimension_traffic(db, subscription, SPEC, 50)
        await db.commit()

        assert subscription.traffic_limit_gb == 100
        assert subscription.purchased_traffic_gb == 0

        # Обычная докупка после WL считает только свои пакеты.
        await apply_traffic_package(db, subscription, parse_packages({'10': 990})[0])
        await db.commit()

        assert subscription.traffic_limit_gb == 110
        assert subscription.purchased_traffic_gb == 10


@pytest.mark.asyncio
async def test_expired_dimension_purchase_does_not_drop_base_limit(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db, traffic_limit_gb=100)
        await apply_traffic_package(db, subscription, parse_packages({'20': 1990})[0])
        await grant_dimension_traffic(db, subscription, SPEC, 30)
        await db.commit()
        assert subscription.traffic_limit_gb == 120

        later = datetime.now(UTC) + timedelta(days=31)
        await expire_dimension_purchases(db, subscription, now=later)
        await db.commit()

        assert subscription.traffic_limit_gb == 120, 'истёкший WL не трогает обычный лимит'
        assert subscription.purchased_traffic_gb == 20
        assert (await dimension_row(db, subscription)).purchased_gb == 0


@pytest.mark.asyncio
async def test_dimension_quota_is_base_plus_purchased(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        from app.services.traffic_dimensions import ensure_dimension_row

        row = await ensure_dimension_row(db, subscription, SPEC, base_limit_gb=10)
        await grant_dimension_traffic(db, subscription, SPEC, 25)
        await db.commit()

        assert row.base_limit_gb == 10
        assert row.purchased_gb == 25
        assert row.limit_gb == 35


@pytest.mark.asyncio
async def test_purchases_accumulate(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        await grant_dimension_traffic(db, subscription, SPEC, 10)
        await grant_dimension_traffic(db, subscription, SPEC, 5)
        await db.commit()

        assert (await dimension_row(db, subscription)).purchased_gb == 15
        assert len(await purchases(db, 'wl')) == 2


@pytest.mark.asyncio
async def test_purchased_gb_is_recomputed_not_decremented(monkeypatch):
    """Пересчёт от источника сходится к правде даже из рассинхрона."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        await grant_dimension_traffic(db, subscription, SPEC, 10)
        row = await dimension_row(db, subscription)
        row.purchased_gb = 999  # искусственный рассинхрон
        await db.commit()

        from app.services.traffic_dimensions import sync_dimension_purchased_gb

        assert await sync_dimension_purchased_gb(db, subscription, SPEC) == 10


# ------------------------------ включённое тарифом ------------------------------


async def set_tariff_included(db, subscription, included_gb):
    """Заводит тариф и связывает его с подпиской."""
    tariff = Tariff(name='T', description='', traffic_limit_gb=100)
    db.add(tariff)
    await db.flush()
    subscription.tariff_id = tariff.id
    if included_gb is not None:
        db.add(TariffTrafficDimension(tariff_id=tariff.id, dimension_id=SPEC.id, included_gb=included_gb))
    await db.flush()
    return tariff


@pytest.mark.asyncio
async def test_new_row_takes_the_included_volume_from_the_tariff(monkeypatch):
    """Ровно то, ради чего заводился tariff_traffic_dimensions."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        await set_tariff_included(db, subscription, 50)

        from app.services.traffic_dimensions import ensure_dimension_row

        row = await ensure_dimension_row(db, subscription, SPEC)
        await db.commit()

        assert row.base_limit_gb == 50
        assert row.limit_gb == 50


@pytest.mark.asyncio
async def test_tariff_without_the_dimension_falls_back_to_its_default(monkeypatch):
    """Тариф измерение не включает — берём умолчание самого измерения."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        await set_tariff_included(db, subscription, None)

        from app.services.traffic_dimensions import ensure_dimension_row

        row = await ensure_dimension_row(db, subscription, SPEC)
        await db.commit()

        assert row.base_limit_gb == SPEC.default_limit_gb


@pytest.mark.asyncio
async def test_zero_included_means_unlimited(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        await set_tariff_included(db, subscription, 0)

        from app.services.traffic_dimensions import ensure_dimension_row

        row = await ensure_dimension_row(db, subscription, SPEC)
        await db.commit()

        assert row.base_limit_gb == 0
        assert row.is_unlimited


@pytest.mark.asyncio
async def test_sync_propagates_a_changed_tariff_setting(monkeypatch):
    """Правка тарифа обязана доходить до уже существующих подписок."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        tariff = await set_tariff_included(db, subscription, 20)

        from app.services.traffic_dimensions import ensure_dimension_row, sync_tariff_dimensions

        row = await ensure_dimension_row(db, subscription, SPEC)
        assert row.base_limit_gb == 20

        from sqlalchemy import select as sa_select

        from app.database.models import TariffTrafficDimension

        config = (
            await db.execute(sa_select(TariffTrafficDimension).where(TariffTrafficDimension.tariff_id == tariff.id))
        ).scalar_one()
        config.included_gb = 75
        await db.flush()

        assert await sync_tariff_dimensions(db, subscription) == 1
        await db.commit()
        assert row.base_limit_gb == 75


@pytest.mark.asyncio
async def test_sync_keeps_paid_topups(monkeypatch):
    """Смена тарифа не сжигает оплаченные докупки — у них свой срок."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        await set_tariff_included(db, subscription, 20)

        from app.services.traffic_dimensions import ensure_dimension_row, sync_tariff_dimensions

        row = await ensure_dimension_row(db, subscription, SPEC)
        await grant_dimension_traffic(db, subscription, SPEC, 30)
        await db.commit()
        assert row.limit_gb == 50

        await sync_tariff_dimensions(db, subscription)
        await db.commit()

        assert row.purchased_gb == 30, 'докупка пережила пересинхронизацию'
        assert row.base_limit_gb == 20


@pytest.mark.asyncio
async def test_sync_is_idempotent(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        await set_tariff_included(db, subscription, 20)

        from app.services.traffic_dimensions import sync_tariff_dimensions

        assert await sync_tariff_dimensions(db, subscription) >= 0
        await db.commit()
        assert await sync_tariff_dimensions(db, subscription) == 0, 'второй прогон ничего не меняет'
