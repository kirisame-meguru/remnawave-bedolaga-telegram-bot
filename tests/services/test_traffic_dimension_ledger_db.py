"""Журнал наблюдений на живой БД: апсерт, суммы за окно, чистка, запись состояния.

Главное, что здесь проверяется, — монотонность. Панельный бакет суток растёт,
пока сутки открыты, а после недельного TRUNCATE панель начнёт отдавать по тем
же суткам меньше или ничего. Апсерт обязан удержать максимум: иначе счётчик
расхода падал бы каждый понедельник и квота выдавалась бы заново.
"""

from datetime import UTC, date, datetime

import pytest

from app.database.models import (
    Subscription,
    SubscriptionTrafficDimension,
    Tariff,
    TrafficDimension,
    TrafficDimensionSample,
    User,
)
from app.services.traffic_dimension_ledger import (
    TrafficDimensionLedgerService,
    prune_samples,
    store_samples,
    window_usage,
)
from app.services.traffic_dimensions import traffic_dimensions
from tests.fixtures.sqlite_memory import memory_session
from tests.services.test_traffic_dimension_ledger import make_spec


TABLES = [
    User.__table__,
    Tariff.__table__,
    Subscription.__table__,
    TrafficDimension.__table__,
    SubscriptionTrafficDimension.__table__,
    TrafficDimensionSample.__table__,
]

FETCHED_AT = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)


def sample(usage_date: date, inbound: str, value: int, uuid: str = 'u-1') -> dict:
    return {
        'remnawave_uuid': uuid,
        'inbound_uuid': inbound,
        'usage_date': usage_date,
        'bytes': value,
        'fetched_at': FETCHED_AT,
    }


@pytest.mark.asyncio
async def test_upsert_keeps_the_larger_value(monkeypatch):
    """Панель отдала меньше, чем уже записано, — значит, был TRUNCATE, а не откат."""
    async with memory_session(monkeypatch, TABLES) as db:
        await store_samples(db, [sample(date(2026, 3, 10), 'aaa', 500)])
        await store_samples(db, [sample(date(2026, 3, 10), 'aaa', 900)])
        await store_samples(db, [sample(date(2026, 3, 10), 'aaa', 1)])
        await db.commit()

        usage = await window_usage(db, 'u-1', window_start=date(2026, 3, 1), window_end=date(2026, 3, 11))
        assert usage.by_inbound == {'aaa': 900}


@pytest.mark.asyncio
async def test_window_usage_respects_boundaries(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await store_samples(
            db,
            [
                sample(date(2026, 2, 28), 'aaa', 111),  # до окна
                sample(date(2026, 3, 1), 'aaa', 10),
                sample(date(2026, 3, 5), 'bbb', 20),
                sample(date(2026, 3, 12), 'aaa', 222),  # после окна
            ],
        )
        await db.commit()

        usage = await window_usage(db, 'u-1', window_start=date(2026, 3, 1), window_end=date(2026, 3, 11))
        assert usage.by_inbound == {'aaa': 10, 'bbb': 20}
        assert usage.covered_from == date(2026, 3, 1)
        assert usage.bytes_for(['aaa', 'bbb']) == 30


@pytest.mark.asyncio
async def test_window_usage_isolates_users(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await store_samples(db, [sample(date(2026, 3, 5), 'aaa', 10, uuid='u-1')])
        await store_samples(db, [sample(date(2026, 3, 5), 'aaa', 9999, uuid='u-2')])
        await db.commit()

        usage = await window_usage(db, 'u-1', window_start=date(2026, 3, 1), window_end=date(2026, 3, 11))
        assert usage.by_inbound == {'aaa': 10}


@pytest.mark.asyncio
async def test_window_usage_without_samples_is_uncovered(monkeypatch):
    """Пустой журнал — это дыра в покрытии, а не подтверждённый ноль."""
    async with memory_session(monkeypatch, TABLES) as db:
        usage = await window_usage(db, 'u-1', window_start=date(2026, 3, 1), window_end=date(2026, 3, 11))
        assert usage.by_inbound == {}
        assert usage.covered_from is None


@pytest.mark.asyncio
async def test_prune_drops_only_old_samples(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await store_samples(
            db,
            [
                sample(date(2025, 11, 1), 'aaa', 1),
                sample(date(2026, 3, 1), 'aaa', 2),
            ],
        )
        await db.commit()

        removed = await prune_samples(db, keep_days=30, today=date(2026, 3, 11))
        await db.commit()
        assert removed == 1

        usage = await window_usage(db, 'u-1', window_start=date(2020, 1, 1), window_end=date(2026, 3, 11))
        assert usage.by_inbound == {'aaa': 2}


@pytest.mark.asyncio
async def test_prune_disabled_by_zero(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        await store_samples(db, [sample(date(2020, 1, 1), 'aaa', 1)])
        await db.commit()
        assert await prune_samples(db, keep_days=0, today=date(2026, 3, 11)) == 0


# --------------------------- запись состояния подписки ---------------------------


async def make_subscription(db) -> Subscription:
    user = User(telegram_id=1, username='u', first_name='U', language='ru')
    db.add(user)
    await db.flush()
    subscription = Subscription(
        user_id=user.id,
        status='active',
        start_date=datetime(2026, 3, 1, tzinfo=UTC),
        end_date=datetime(2026, 4, 1, tzinfo=UTC),
        remnawave_uuid='u-1',
        connected_squads=['sq-1'],
    )
    db.add(subscription)
    await db.flush()
    return subscription


async def make_dimension(db, spec) -> TrafficDimension:
    row = TrafficDimension(
        id=spec.id,
        key=spec.key,
        title=spec.titles,
        fallback_title=spec.fallback_title,
        icon=spec.icon,
        inbound_uuids=sorted(spec.inbound_uuids),
        default_limit_gb=spec.default_limit_gb,
        enforcement=spec.enforcement,
        is_enabled=True,
        is_builtin=False,
        position=spec.position,
    )
    db.add(row)
    await db.flush()
    traffic_dimensions.invalidate()
    return row


class FakeReading:
    """Ровно то, что `_write_state` читает у результата чтения панели."""

    def __init__(self, known: bool, *, has_daily_series: bool = True, total: int = 0):
        self.known = known
        self.matrix = type('M', (), {'has_daily_series': has_daily_series})()
        self._total = total

    def total_for(self, _inbounds):
        return self._total


GB = 1024**3


@pytest.mark.asyncio
async def test_state_is_written_from_the_ledger(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        spec = make_spec(inbounds=('aaa',))
        await make_dimension(db, spec)
        subscription = await make_subscription(db)

        await store_samples(db, [sample(date(2026, 3, 2), 'aaa', 3 * GB, uuid='u-1')])
        usage = await window_usage(db, 'u-1', window_start=date(2026, 3, 1), window_end=date(2026, 3, 11))

        service = TrafficDimensionLedgerService()
        measurement = await service._write_state(
            db,
            subscription,
            spec,
            usage=usage,
            reading_known=True,
            has_daily_series=True,
            live_bytes=3 * GB,
            window_start=date(2026, 3, 1),
        )
        await db.commit()

        assert measurement.used_gb == pytest.approx(3.0)
        assert measurement.known
        # Первые наблюдения — 2 марта, окно открылось 1-го: начало не покрыто.
        assert measurement.coverage_from == date(2026, 3, 2)
        assert measurement.has_coverage_gap

        row = (await db.execute(SubscriptionTrafficDimension.__table__.select())).mappings().one()
        assert row['used_gb'] == pytest.approx(3.0)
        assert row['window_start'] == date(2026, 3, 1)
        assert row['coverage_from'] == date(2026, 3, 2)
        assert row['measured_known'] is True or row['measured_known'] == 1


@pytest.mark.asyncio
async def test_failed_reading_keeps_previous_value(monkeypatch):
    """Панель молчит — прежняя цифра лучше нуля: ноль раздал бы квоту всем разом."""
    async with memory_session(monkeypatch, TABLES) as db:
        spec = make_spec(inbounds=('aaa',))
        await make_dimension(db, spec)
        subscription = await make_subscription(db)
        service = TrafficDimensionLedgerService()

        await store_samples(db, [sample(date(2026, 3, 1), 'aaa', 7 * GB, uuid='u-1')])
        usage = await window_usage(db, 'u-1', window_start=date(2026, 3, 1), window_end=date(2026, 3, 11))
        await service._write_state(
            db,
            subscription,
            spec,
            usage=usage,
            reading_known=True,
            has_daily_series=True,
            live_bytes=7 * GB,
            window_start=date(2026, 3, 1),
        )
        await db.commit()

        measurement = await service._write_state(
            db,
            subscription,
            spec,
            usage=await window_usage(db, 'u-1', window_start=date(2026, 3, 1), window_end=date(2026, 3, 11)),
            reading_known=False,
            has_daily_series=True,
            live_bytes=0,
            window_start=date(2026, 3, 1),
        )
        await db.commit()

        assert measurement.used_gb == pytest.approx(7.0)
        assert measurement.known
        row = (await db.execute(SubscriptionTrafficDimension.__table__.select())).mappings().one()
        assert row['used_gb'] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_window_rollover_resets_the_counter(monkeypatch):
    """Новое окно — новый счёт: наблюдения прошлого окна в сумму не попадают."""
    async with memory_session(monkeypatch, TABLES) as db:
        spec = make_spec(inbounds=('aaa',))
        await make_dimension(db, spec)
        subscription = await make_subscription(db)
        service = TrafficDimensionLedgerService()

        await store_samples(db, [sample(date(2026, 3, 1), 'aaa', 9 * GB, uuid='u-1')])
        await service._write_state(
            db,
            subscription,
            spec,
            usage=await window_usage(db, 'u-1', window_start=date(2026, 3, 1), window_end=date(2026, 3, 31)),
            reading_known=True,
            has_daily_series=True,
            live_bytes=9 * GB,
            window_start=date(2026, 3, 1),
        )
        await db.commit()

        measurement = await service._write_state(
            db,
            subscription,
            spec,
            usage=await window_usage(db, 'u-1', window_start=date(2026, 4, 1), window_end=date(2026, 4, 2)),
            reading_known=True,
            has_daily_series=True,
            live_bytes=0,
            window_start=date(2026, 4, 1),
        )
        await db.commit()

        assert measurement.used_gb == pytest.approx(0.0)
        assert measurement.window_start == date(2026, 4, 1)


@pytest.mark.asyncio
async def test_legacy_panel_counter_never_goes_backwards(monkeypatch):
    """Панель без посуточных рядов отдаёт только текущую неделю — счётчик держим."""
    async with memory_session(monkeypatch, TABLES) as db:
        spec = make_spec(inbounds=('aaa',))
        await make_dimension(db, spec)
        subscription = await make_subscription(db)
        service = TrafficDimensionLedgerService()
        empty = await window_usage(db, 'u-1', window_start=date(2026, 3, 1), window_end=date(2026, 3, 11))

        await service._write_state(
            db,
            subscription,
            spec,
            usage=empty,
            reading_known=True,
            has_daily_series=False,
            live_bytes=12 * GB,
            window_start=date(2026, 3, 1),
        )
        await db.commit()

        # Понедельник: панель вычистила историю и отдаёт почти ноль.
        measurement = await service._write_state(
            db,
            subscription,
            spec,
            usage=empty,
            reading_known=True,
            has_daily_series=False,
            live_bytes=GB // 2,
            window_start=date(2026, 3, 1),
        )
        await db.commit()

        assert measurement.used_gb == pytest.approx(12.0)

        # Смена окна снимает удержание — иначе счётчик залип бы навсегда.
        rolled = await service._write_state(
            db,
            subscription,
            spec,
            usage=empty,
            reading_known=True,
            has_daily_series=False,
            live_bytes=GB // 2,
            window_start=date(2026, 4, 1),
        )
        await db.commit()
        assert rolled.used_gb == pytest.approx(0.5)


# ------------------------------ полный проход по подписке ------------------------------


class FakePanelReading:
    def __init__(self, matrix):
        self.matrix = matrix
        self.known = True

    def total_for(self, inbounds):
        return self.matrix.total_for(inbounds)


@pytest.mark.asyncio
async def test_refresh_subscription_end_to_end(monkeypatch):
    """Снять с панели → записать в журнал → пересчитать состояние."""
    from app.services.traffic_dimension_meter import InboundUsageMatrix

    async with memory_session(monkeypatch, TABLES) as db:
        spec = make_spec(inbounds=('aaa',))
        await make_dimension(db, spec)
        subscription = await make_subscription(db)

        matrix = InboundUsageMatrix(
            cells={
                (date(2026, 3, 1), 'aaa'): 2 * GB,
                (date(2026, 3, 2), 'aaa'): 3 * GB,
                (date(2026, 3, 2), 'other'): 50 * GB,  # чужой инбаунд в журнал не попадает
            },
            dates=(date(2026, 3, 1), date(2026, 3, 2)),
        )

        service = TrafficDimensionLedgerService()

        async def fake_read(remnawave_uuid, start, end, *, api=None):
            assert remnawave_uuid == 'u-1'
            assert start == date(2026, 3, 1), 'окно открывается вместе с подпиской'
            return FakePanelReading(matrix)

        monkeypatch.setattr(service.remnawave_service, 'read_inbound_usage', fake_read)

        squad_index = {'sq-1': frozenset({'aaa'})}
        measurements = await service.refresh_subscription(
            db, subscription, [spec], squad_index, api=None, today=date(2026, 3, 2)
        )
        await db.commit()

        assert len(measurements) == 1
        assert measurements[0].used_gb == pytest.approx(5.0)
        assert measurements[0].coverage_from == date(2026, 3, 1)
        assert not measurements[0].has_coverage_gap, 'журнал покрывает окно с первого дня'

        stored = (await db.execute(TrafficDimensionSample.__table__.select())).mappings().all()
        assert {row['inbound_uuid'] for row in stored} == {'aaa'}, 'чужие инбаунды не пишем'


@pytest.mark.asyncio
async def test_refresh_skips_subscription_without_dimension_squads(monkeypatch):
    """Нет инбаунда измерения в сквадах — панель не дёргаем вовсе."""
    async with memory_session(monkeypatch, TABLES) as db:
        spec = make_spec(inbounds=('aaa',))
        await make_dimension(db, spec)
        subscription = await make_subscription(db)

        service = TrafficDimensionLedgerService()
        calls = []

        async def fake_read(*args, **kwargs):
            calls.append(args)
            raise AssertionError('панель не должна опрашиваться')

        monkeypatch.setattr(service.remnawave_service, 'read_inbound_usage', fake_read)

        measurements = await service.refresh_subscription(
            db, subscription, [spec], {'sq-1': frozenset({'zzz'})}, api=None, today=date(2026, 3, 2)
        )

        assert measurements == []
        assert calls == []
