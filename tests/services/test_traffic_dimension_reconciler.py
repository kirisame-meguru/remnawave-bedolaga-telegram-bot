"""Реконсилятор на живой БД: три режима, откат неудачной записи, карта блокировок.

Ключевое, что здесь проверяется, — режимы действительно различаются. `observe`
не должен оставлять следов, `notify` не должен трогать панель, а неудачная
запись в панель не должна оставлять в БД отметку «доступ закрыт», которой на
самом деле нет.
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
from app.services.traffic_dimension_enforcement import (
    BlockReason,
    EnforcementAction,
    EnforcementMode,
    StripPlan,
    dimension_squad_policy,
    plan_squad_strip,
)
from app.services.traffic_dimension_reconciler import (
    ReconcileReport,
    TrafficDimensionReconciler,
    _PlannedChange,
)
from app.services.traffic_dimensions import traffic_dimensions
from tests.fixtures.sqlite_memory import memory_session
from tests.services.test_traffic_dimension_enforcement import make_state
from tests.services.test_traffic_dimension_ledger import make_spec


TABLES = [
    User.__table__,
    Tariff.__table__,
    Subscription.__table__,
    TrafficDimension.__table__,
    SubscriptionTrafficDimension.__table__,
    TrafficDimensionSample.__table__,
]

SPEC = make_spec(inbounds=('aaa',))
PLAN = plan_squad_strip(['sq-wl'], {'sq-wl': frozenset({'aaa'})}, frozenset({'aaa'}))


@pytest.fixture(autouse=True)
def clean_policy():
    """Карта блокировок живёт в процессе — тесты не должны течь друг в друга."""
    dimension_squad_policy.replace_all({})
    yield
    dimension_squad_policy.replace_all({})


class FakeApi:
    """Панель, которая запоминает вызовы и умеет падать по требованию."""

    def __init__(self, *, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    async def update_user(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError('panel is down')
        return object()


async def seed(db, *, connected_squads=('sq-wl', 'sq-eu')) -> Subscription:
    row = TrafficDimension(
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
    db.add(row)
    user = User(telegram_id=1, username='u', first_name='U', language='ru')
    db.add(user)
    await db.flush()
    subscription = Subscription(
        user_id=user.id,
        status='active',
        start_date=datetime(2026, 3, 1, tzinfo=UTC),
        end_date=datetime(2026, 4, 1, tzinfo=UTC),
        remnawave_uuid='u-1',
        connected_squads=list(connected_squads),
    )
    db.add(subscription)
    await db.flush()
    traffic_dimensions.invalidate()
    return subscription


def block_change(subscription, *, plan: StripPlan | None = PLAN) -> _PlannedChange:
    return _PlannedChange(
        subscription=subscription,
        spec=SPEC,
        state=make_state(spec=SPEC, used_gb=12.0),
        action=EnforcementAction.BLOCK,
        reason=BlockReason.QUOTA_EXHAUSTED,
        plan=plan,
    )


async def state_row(db) -> SubscriptionTrafficDimension:
    return (await db.execute(SubscriptionTrafficDimension.__table__.select())).mappings().one()


async def state_rows(db) -> list:
    return (await db.execute(SubscriptionTrafficDimension.__table__.select())).mappings().all()


# ------------------------------ режимы ------------------------------


@pytest.mark.asyncio
async def test_observe_leaves_no_trace(monkeypatch):
    """Режим наблюдения обязан быть полностью безопасным."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        api = FakeApi()
        report = ReconcileReport(mode=EnforcementMode.OBSERVE)

        await TrafficDimensionReconciler()._apply(
            db, block_change(subscription), api=api, report=report, block_allowed=True
        )
        await db.commit()

        assert api.calls == [], 'наблюдение не пишет в панель'
        assert await state_rows(db) == [], 'наблюдение не заводит даже строк состояния'
        assert dimension_squad_policy.blocked_uuids() == frozenset()
        assert report.blocked == 1, 'посчитать всё равно надо — ради отчёта'
        assert len(report.transitions) == 1, 'видно, кого бы отрезало'


@pytest.mark.asyncio
async def test_notify_records_without_touching_the_panel(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        api = FakeApi()
        report = ReconcileReport(mode=EnforcementMode.NOTIFY)

        await TrafficDimensionReconciler()._apply(
            db, block_change(subscription), api=api, report=report, block_allowed=True
        )
        await db.commit()

        assert api.calls == [], 'notify показывает, кого бы отрезало, но не режет'
        row = await state_row(db)
        assert row['blocked_at'] is not None
        assert row['block_reason'] == BlockReason.QUOTA_EXHAUSTED.value
        assert not row['stripped_squads'], 'ничего не снято — восстанавливать нечего'
        assert dimension_squad_policy.blocked_uuids() == frozenset()
        assert len(report.transitions) == 1


@pytest.mark.asyncio
async def test_enforce_strips_only_the_dimension_squad(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        api = FakeApi()
        report = ReconcileReport(mode=EnforcementMode.ENFORCE)

        await TrafficDimensionReconciler()._apply(
            db, block_change(subscription), api=api, report=report, block_allowed=True
        )
        await db.commit()

        assert len(api.calls) == 1
        assert api.calls[0]['active_internal_squads'] == ['sq-eu'], 'обычный доступ остаётся'
        row = await state_row(db)
        assert row['stripped_squads'] == ['sq-wl']
        assert dimension_squad_policy.stripped_for('u-1') == frozenset({'sq-wl'})
        assert report.panel_writes == 1


@pytest.mark.asyncio
async def test_entitlement_is_not_rewritten_by_a_block(monkeypatch):
    """`connected_squads` — право подписки, блокировка его не трогает."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        report = ReconcileReport(mode=EnforcementMode.ENFORCE)

        await TrafficDimensionReconciler()._apply(
            db, block_change(subscription), api=FakeApi(), report=report, block_allowed=True
        )
        await db.commit()

        assert subscription.connected_squads == ['sq-wl', 'sq-eu']


@pytest.mark.asyncio
async def test_failed_panel_write_does_not_pretend_access_is_closed(monkeypatch):
    """Иначе в БД стоит блокировка, а пользователь ходит — и никто не заметит."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        api = FakeApi(fail=True)
        report = ReconcileReport(mode=EnforcementMode.ENFORCE)

        await TrafficDimensionReconciler()._apply(
            db, block_change(subscription), api=api, report=report, block_allowed=True
        )
        await db.commit()

        row = await state_row(db)
        assert row['blocked_at'] is None
        assert row['block_reason'] is None
        assert dimension_squad_policy.blocked_uuids() == frozenset()
        assert report.panel_errors == 1


@pytest.mark.asyncio
async def test_blast_guard_suppresses_the_block(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        api = FakeApi()
        report = ReconcileReport(mode=EnforcementMode.ENFORCE)

        await TrafficDimensionReconciler()._apply(
            db, block_change(subscription), api=api, report=report, block_allowed=False
        )
        await db.commit()

        assert api.calls == []
        assert await state_rows(db) == [], 'подавленная блокировка не оставляет следов'
        assert report.transitions == []
        assert report.blocked == 0, 'в отчёте видно, что заблокировано не было ничего'


# ------------------------------ снятие блокировки ------------------------------


@pytest.mark.asyncio
async def test_unblock_restores_the_full_entitlement(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        reconciler = TrafficDimensionReconciler()
        api = FakeApi()

        # Блокируем, затем снимаем.
        await reconciler._apply(
            db,
            block_change(subscription),
            api=api,
            report=ReconcileReport(mode=EnforcementMode.ENFORCE),
            block_allowed=True,
        )
        await db.commit()
        assert dimension_squad_policy.stripped_for('u-1') == frozenset({'sq-wl'})

        unblock = _PlannedChange(
            subscription=subscription,
            spec=SPEC,
            state=make_state(spec=SPEC, used_gb=0.0, blocked=True),
            action=EnforcementAction.UNBLOCK,
            reason=None,
            plan=PLAN,
        )
        report = ReconcileReport(mode=EnforcementMode.ENFORCE)
        await reconciler._apply(db, unblock, api=api, report=report, block_allowed=True)
        await db.commit()

        assert api.calls[-1]['active_internal_squads'] == ['sq-wl', 'sq-eu']
        assert dimension_squad_policy.blocked_uuids() == frozenset()
        row = await state_row(db)
        assert row['blocked_at'] is None
        assert row['stripped_squads'] == []


@pytest.mark.asyncio
async def test_failed_unblock_keeps_the_filter(monkeypatch):
    """Панель не подтвердила возврат — фильтр обязан остаться, иначе доступ утечёт."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        reconciler = TrafficDimensionReconciler()

        await reconciler._apply(
            db,
            block_change(subscription),
            api=FakeApi(),
            report=ReconcileReport(mode=EnforcementMode.ENFORCE),
            block_allowed=True,
        )
        await db.commit()

        unblock = _PlannedChange(
            subscription=subscription,
            spec=SPEC,
            state=make_state(spec=SPEC, used_gb=0.0, blocked=True),
            action=EnforcementAction.UNBLOCK,
            reason=None,
            plan=PLAN,
        )
        report = ReconcileReport(mode=EnforcementMode.ENFORCE)
        await reconciler._apply(db, unblock, api=FakeApi(fail=True), report=report, block_allowed=True)
        await db.commit()

        assert dimension_squad_policy.stripped_for('u-1') == frozenset({'sq-wl'})
        row = await state_row(db)
        assert row['blocked_at'] is not None


# ------------------------------ повторяющиеся причины ------------------------------


@pytest.mark.asyncio
async def test_hold_is_reported_once_not_every_cycle(monkeypatch):
    """Иначе уведомление о «нет данных» уходило бы раз в три часа."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        reconciler = TrafficDimensionReconciler()
        hold = _PlannedChange(
            subscription=subscription,
            spec=SPEC,
            state=make_state(spec=SPEC, used_known=False, blocked=True),
            action=EnforcementAction.HOLD,
            reason=BlockReason.UNKNOWN_USAGE_HOLD,
            plan=None,
        )

        first = ReconcileReport(mode=EnforcementMode.ENFORCE)
        await reconciler._apply(db, hold, api=FakeApi(), report=first, block_allowed=True)
        await db.commit()
        assert len(first.transitions) == 1

        second = ReconcileReport(mode=EnforcementMode.ENFORCE)
        await reconciler._apply(db, hold, api=FakeApi(), report=second, block_allowed=True)
        await db.commit()
        assert second.transitions == [], 'причина не изменилась — событие уже было'
        assert second.held == 1, 'в счётчике отражается всё равно'


@pytest.mark.asyncio
async def test_mixed_squad_refusal_writes_no_panel_call(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db, connected_squads=('sq-mixed',))
        api = FakeApi()
        mixed_plan = plan_squad_strip(['sq-mixed'], {'sq-mixed': frozenset({'aaa', 'eu1'})}, frozenset({'aaa'}))
        change = _PlannedChange(
            subscription=subscription,
            spec=SPEC,
            state=make_state(spec=SPEC, used_gb=12.0),
            action=EnforcementAction.REFUSE,
            reason=BlockReason.MIXED_SQUAD,
            plan=mixed_plan,
        )
        report = ReconcileReport(mode=EnforcementMode.ENFORCE)

        await TrafficDimensionReconciler()._apply(db, change, api=api, report=report, block_allowed=True)
        await db.commit()

        assert api.calls == [], 'смешанный сквад не снимаем никогда'
        row = await state_row(db)
        assert row['block_reason'] == BlockReason.MIXED_SQUAD.value
        assert row['stripped_squads'] == []


# ------------------------------ карта блокировок ------------------------------


@pytest.mark.asyncio
async def test_publish_policy_rebuilds_the_map_from_the_database(monkeypatch):
    """После рестарта карта обязана подняться из БД до первой записи в панель."""
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        db.add(
            SubscriptionTrafficDimension(
                subscription_id=subscription.id,
                dimension_id=SPEC.id,
                base_limit_gb=10,
                used_gb=12.0,
                measured_known=True,
                window_start=date(2026, 3, 1),
                blocked_at=datetime(2026, 3, 5, tzinfo=UTC),
                block_reason=BlockReason.QUOTA_EXHAUSTED.value,
                stripped_squads=['SQ-WL'],
            )
        )
        await db.commit()

        count = await TrafficDimensionReconciler().publish_policy(db)

        assert count == 1
        assert dimension_squad_policy.stripped_for('u-1') == frozenset({'sq-wl'})


@pytest.mark.asyncio
async def test_publish_policy_ignores_unblocked_rows(monkeypatch):
    async with memory_session(monkeypatch, TABLES) as db:
        subscription = await seed(db)
        db.add(
            SubscriptionTrafficDimension(
                subscription_id=subscription.id,
                dimension_id=SPEC.id,
                base_limit_gb=10,
                used_gb=1.0,
                measured_known=True,
                blocked_at=None,
                stripped_squads=[],
            )
        )
        await db.commit()

        assert await TrafficDimensionReconciler().publish_policy(db) == 0
        assert dimension_squad_policy.blocked_uuids() == frozenset()
