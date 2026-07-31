"""Приведение доступа в панели в соответствие с расходом измерений.

Реконсилятор — единственное место, которое ставит и снимает блокировку. Он
читает цифры, посчитанные журналом наблюдений, принимает по ним решение
(``traffic_dimension_enforcement.decide``) и, если режим позволяет, применяет
его к панели.

Три режима существуют ради одного: выкатывать такое сразу «боевым» нельзя.

* ``observe`` — считаем и логируем. Ничего не пишется и не отправляется.
* ``notify`` — фиксируем исчерпание в БД и уведомляем пользователя. Доступ не
  трогаем: видно, кого бы отрезало, но никто ещё ничего не теряет.
* ``enforce`` — то же плюс снятие сквадов в панели.

Предохранитель поверх всего: если за цикл заблокировать пришлось бы больше
подписок, чем разрешают пороги, не блокируется никто. Массовая блокировка почти
никогда не означает массовое исчерпание квоты — куда чаще это сменившиеся uuid
инбаундов или потерянная карта сквадов.

Здесь же живёт «щит» режима ``shielded``: панельный лимит держится поднятым на
израсходованное измерением, чтобы его трафик не съедал основную квоту. Это
делает именно цикл, а не разовые места, которые пушат лимит подписки: щит
растёт вместе с расходом, и постоянного правильного значения не существует.

Известное ограничение: пока по подписке открыт grace-оверлей, ограничение не
применяется, а сама подписка исключается из карты блокировок. Grace владеет
``active_internal_squads`` и сверяет ответ панели с запрошенным, поэтому чужой
фильтр он прочитал бы как отказ панели. После закрытия оверлея блокировка
возвращается на следующем цикле — то есть заблокированный пользователь с
истёкшей подпиской может получить доступ к измерению максимум на один интервал.
"""

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus, SubscriptionTrafficDimension
from app.services.traffic_dimension_enforcement import (
    BlastGuard,
    BlockReason,
    EnforcementAction,
    EnforcementMode,
    StripPlan,
    decide,
    dimension_squad_policy,
    effective_panel_traffic_limit_bytes,
    panel_squads_for,
    plan_squad_strip,
    resolve_mode,
)
from app.services.traffic_dimension_ledger import squad_inbound_index
from app.services.traffic_dimensions import (
    DimensionState,
    TrafficDimensionSpec,
    get_dimension_states,
    sync_tariff_dimensions,
    traffic_dimensions,
)


logger = structlog.get_logger(__name__)

_ENFORCED_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
)

# Пользовательский порог предупреждения ограничен снизу 50% (см.
# notification_prefs.get_traffic_warning_percent), поэтому ниже отбирать нечего.
_MIN_WARNING_PERCENT = 50


def _is_near_limit(state: DimensionState) -> bool:
    """Стоит ли вообще показывать это измерение уведомителю."""
    if state.is_unlimited or state.blocked or not state.is_enforceable:
        return False
    return state.used_percent >= _MIN_WARNING_PERCENT and state.used_gb < state.limit_gb


@dataclass
class DimensionTransition:
    """Смена состояния одного измерения одной подписки за цикл.

    Отдаётся наружу целиком: уведомления (шаг 5) и админские экраны читают её,
    а не лезут в БД повторно.
    """

    subscription_id: int
    user_id: int
    remnawave_uuid: str | None
    spec: TrafficDimensionSpec
    state: DimensionState
    action: EnforcementAction
    reason: BlockReason | None
    applied: bool  # дошло ли до панели
    stripped_squads: tuple[str, ...] = ()


@dataclass
class DimensionUsageSnapshot:
    """Не заблокированное измерение, подошедшее близко к лимиту.

    Собирается здесь, потому что состояния уже посчитаны, но порог берётся
    пользовательский (50..99%), поэтому отбор идёт по минимально возможному —
    точную отсечку делает уведомитель, который знает настройки пользователя.
    """

    subscription_id: int
    user_id: int
    spec: TrafficDimensionSpec
    state: DimensionState


@dataclass
class ReconcileReport:
    mode: EnforcementMode = EnforcementMode.OBSERVE
    scanned: int = 0
    blocked: int = 0
    unblocked: int = 0
    held: int = 0
    refused: int = 0
    panel_writes: int = 0
    shield_writes: int = 0
    panel_errors: int = 0
    skipped_grace: int = 0
    blast_guard_tripped: str | None = None
    transitions: list[DimensionTransition] = field(default_factory=list)
    near_limit: list[DimensionUsageSnapshot] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            'mode': self.mode.value,
            'scanned': self.scanned,
            'blocked': self.blocked,
            'unblocked': self.unblocked,
            'held': self.held,
            'refused': self.refused,
            'panel_writes': self.panel_writes,
            'shield_writes': self.shield_writes,
            'panel_errors': self.panel_errors,
            'skipped_grace': self.skipped_grace,
            'blast_guard_tripped': self.blast_guard_tripped,
        }


@dataclass
class _PlannedChange:
    """Решение по подписке, ещё не применённое: сначала считаем весь цикл."""

    subscription: Subscription
    spec: TrafficDimensionSpec
    state: DimensionState
    action: EnforcementAction
    reason: BlockReason | None
    plan: StripPlan | None


class TrafficDimensionReconciler:
    """Ставит и снимает блокировки измерений.

    Панельных записей делает ровно столько, сколько подписок реально меняют
    состояние: решение принимается по кэшу расхода, который уже посчитал
    журнал, поэтому цикл без изменений не стоит ни одного запроса.
    """

    def __init__(self) -> None:
        self._notifier = None
        # Последний отправленный в панель «щит» по каждому uuid. Только в
        # памяти: после рестарта щит переставляется заново, что заодно чинит
        # лимит, если его успел сбить кто-то ещё.
        self._pushed_shield: dict[str, int] = {}

    def set_notifier(self, notifier) -> None:
        """Подключает доставку уведомлений (шаг 5).

        Реконсилятор про Telegram ничего не знает: он отдаёт переходы, а кто и
        как их показывает — не его забота.
        """
        self._notifier = notifier

    # ------------------------------ настройки ------------------------------

    def get_mode(self) -> EnforcementMode:
        return resolve_mode(getattr(settings, 'TRAFFIC_DIMENSION_ENFORCEMENT_MODE', None))

    def _blast_guard(self, scanned: int) -> BlastGuard:
        return BlastGuard(
            max_blocks=max(int(getattr(settings, 'TRAFFIC_DIMENSION_ENFORCEMENT_MAX_BLOCKS_PER_CYCLE', 0) or 0), 0),
            max_percent=max(int(getattr(settings, 'TRAFFIC_DIMENSION_ENFORCEMENT_MAX_BLOCKS_PERCENT', 0) or 0), 0),
            scanned=scanned,
        )

    # ------------------------------ основной цикл ------------------------------

    async def reconcile(self, *, squad_index: Mapping[str, frozenset[str]] | None = None) -> ReconcileReport:
        """Один полный проход. Возвращает отчёт, ничего не бросает наружу."""
        report = ReconcileReport(mode=self.get_mode())

        async with AsyncSessionLocal() as db:
            specs = await traffic_dimensions.measurable(db)
            if not specs:
                await self.publish_policy(db)
                return report

            try:
                async with AsyncExitStack() as stack:
                    api = None
                    if squad_index is None or report.mode is EnforcementMode.ENFORCE:
                        from app.services.remnawave_service import RemnaWaveService

                        api = await stack.enter_async_context(RemnaWaveService().get_api_client())
                    if squad_index is None:
                        squad_index = squad_inbound_index(await api.get_internal_squads())
                    if not squad_index:
                        logger.warning('Панель не вернула сквады — цикл ограничений пропущен')
                        return report
                    await self._run(db, specs, squad_index, api=api, report=report)
            except Exception as e:
                logger.error('Ошибка цикла ограничений измерений трафика', error=e)
                await db.rollback()
                return report

            await self.publish_policy(db)

        # near_limit тоже повод позвать уведомителя: предупреждение о подходе к
        # лимиту приходит задолго до того, как появится хоть один переход.
        if (report.transitions or report.near_limit) and self._notifier is not None:
            try:
                await self._notifier(report)
            except Exception as e:
                logger.error('Ошибка доставки уведомлений об измерениях', error=e)

        logger.info('🚦 Цикл ограничений измерений трафика завершён', **report.as_dict())
        return report

    async def _run(
        self,
        db: AsyncSession,
        specs: Sequence[TrafficDimensionSpec],
        squad_index: Mapping[str, frozenset[str]],
        *,
        api: Any,
        report: ReconcileReport,
    ) -> None:
        from app.services.grace_access_runtime import get_open_grace_subscription_ids

        grace_ids = await get_open_grace_subscription_ids(db)
        subscriptions = await self._load_candidates(db)
        planned: list[_PlannedChange] = []
        shielded: list[tuple[Subscription, list[DimensionState]]] = []

        for subscription in subscriptions:
            if subscription.id in grace_ids:
                # Grace сам владеет active_internal_squads и сверяет ответ
                # панели с запрошенным: чужой фильтр он прочтёт как отказ.
                report.skipped_grace += 1
                continue

            report.scanned += 1
            # Тариф мог поменяться (продление, переход, правка настроек) —
            # включённые объёмы подтягиваем до того, как считать исчерпание,
            # иначе решение принималось бы по устаревшей квоте.
            await sync_tariff_dimensions(db, subscription)
            states = await get_dimension_states(db, subscription, specs=specs)
            shielded.append((subscription, states))
            for state in states:
                plan = plan_squad_strip(subscription.connected_squads, squad_index, state.spec.inbound_uuids)
                decision = decide(state, plan if state.spec.strips_squads else None)
                if _is_near_limit(state):
                    report.near_limit.append(
                        DimensionUsageSnapshot(
                            subscription_id=subscription.id,
                            user_id=subscription.user_id,
                            spec=state.spec,
                            state=state,
                        )
                    )
                if decision.action is EnforcementAction.NONE:
                    continue
                planned.append(
                    _PlannedChange(
                        subscription=subscription,
                        spec=state.spec,
                        state=state,
                        action=decision.action,
                        reason=decision.reason,
                        plan=decision.plan,
                    )
                )

        guard = self._blast_guard(report.scanned)
        guard.planned = sum(1 for change in planned if change.action is EnforcementAction.BLOCK)
        block_allowed = True
        if guard.would_trip():
            block_allowed = False
            report.blast_guard_tripped = guard.tripped_by
            logger.error(
                '🛑 Предохранитель ограничений сработал: блокировки этого цикла отменены',
                reason=guard.tripped_by,
                planned=guard.planned,
                scanned=report.scanned,
            )

        for change in planned:
            await self._apply(db, change, api=api, report=report, block_allowed=block_allowed)

        await self._apply_shield(shielded, api=api, report=report)

        await db.commit()

    async def _apply_shield(
        self,
        shielded: Sequence[tuple[Subscription, Sequence[DimensionState]]],
        *,
        api: Any,
        report: ReconcileReport,
    ) -> None:
        """Держит панельный лимит поднятым на израсходованное «щитующими» измерениями.

        Постоянно правильного значения тут не бывает: щит растёт вместе с
        расходом, поэтому его переставляет этот цикл, а не места, которые
        разово пушат лимит подписки. Округление до целых ГБ (в самой функции)
        даёт естественный гистерезис — запись не чаще раза на гигабайт.
        """
        if report.mode is not EnforcementMode.ENFORCE or api is None:
            return

        for subscription, states in shielded:
            if not any(state.spec.shields_base_quota for state in states):
                continue
            desired = effective_panel_traffic_limit_bytes(subscription.traffic_limit_gb, states)
            if not desired:
                # Безлимит: поднимать нечего.
                continue
            uuid = str(subscription.remnawave_uuid)
            if self._pushed_shield.get(uuid) == desired:
                continue
            from app.services.grace_access_runtime import update_panel_user_grace_safe

            try:
                await update_panel_user_grace_safe(
                    api,
                    subscription.id,
                    uuid=subscription.remnawave_uuid,
                    traffic_limit_bytes=desired,
                )
            except Exception as e:
                report.panel_errors += 1
                logger.warning(
                    'Не удалось поднять панельный лимит под щит измерения',
                    subscription_id=subscription.id,
                    error=e,
                )
                continue
            self._pushed_shield[uuid] = desired
            report.shield_writes += 1

    async def _load_candidates(self, db: AsyncSession) -> list[Subscription]:
        """Подписки, у которых уже есть состояние хотя бы одного измерения.

        Строка появляется при первом измерении, поэтому список ровно тот, по
        которому вообще есть что решать.
        """
        result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.tariff))
            .where(
                Subscription.status.in_(_ENFORCED_STATUSES),
                Subscription.remnawave_uuid.isnot(None),
                Subscription.id.in_(select(SubscriptionTrafficDimension.subscription_id).distinct()),
            )
            .order_by(Subscription.id)
        )
        return list(result.scalars().all())

    async def _apply(
        self,
        db: AsyncSession,
        change: _PlannedChange,
        *,
        api: Any,
        report: ReconcileReport,
        block_allowed: bool,
    ) -> None:
        from app.services.traffic_dimensions import ensure_dimension_row

        subscription = change.subscription
        if change.action is EnforcementAction.BLOCK and not block_allowed:
            return

        self._count(change, report)

        if report.mode is EnforcementMode.OBSERVE:
            # Наблюдение обязано быть полностью безопасным: даже строки
            # состояния не заводим, чтобы «посмотреть, как оно будет» нельзя
            # было случайно превратить в изменение данных.
            report.transitions.append(self._transition(change, applied=False, stripped=()))
            return

        row = await ensure_dimension_row(db, subscription, change.spec)
        now = datetime.now(UTC)
        applied = False
        stripped: tuple[str, ...] = ()
        # HOLD и REFUSE повторяются каждый цикл, пока причина не ушла. Переход
        # записывается только при смене причины, иначе уведомления (шаг 5) и
        # админские экраны получали бы одно и то же событие раз в три часа.
        previous_reason = row.block_reason
        report_transition = True

        if change.action is EnforcementAction.HOLD:
            # Расход неизвестен: блокировка остаётся, но причина честно меняется.
            report_transition = previous_reason != BlockReason.UNKNOWN_USAGE_HOLD.value
            row.block_reason = BlockReason.UNKNOWN_USAGE_HOLD.value

        elif change.action is EnforcementAction.REFUSE:
            report_transition = previous_reason != BlockReason.MIXED_SQUAD.value
            if report_transition:
                logger.warning(
                    '⚠️ Измерение нельзя ограничить: смешанные сквады',
                    subscription_id=subscription.id,
                    dimension=change.spec.key,
                    mixed_squads=sorted(change.plan.mixed) if change.plan else [],
                )
            row.blocked_at = row.blocked_at or now
            row.block_reason = BlockReason.MIXED_SQUAD.value
            row.stripped_squads = []

        elif change.action is EnforcementAction.BLOCK:
            row.blocked_at = now
            row.block_reason = (change.reason or BlockReason.QUOTA_EXHAUSTED).value
            if report.mode is EnforcementMode.ENFORCE and change.plan and change.plan.strip:
                stripped = tuple(sorted(change.plan.strip))
                applied = await self._push_squads(
                    api,
                    subscription,
                    panel_squads_for(subscription.connected_squads, stripped),
                    report=report,
                )
                if applied:
                    row.stripped_squads = list(stripped)
                    dimension_squad_policy.set_for(subscription.remnawave_uuid, stripped)
                else:
                    # В панель не доехало — не притворяемся, что доступ закрыт.
                    row.blocked_at = None
                    row.block_reason = None
                    stripped = ()

        elif change.action is EnforcementAction.UNBLOCK:
            previously = tuple(row.stripped_squads or [])
            if report.mode is EnforcementMode.ENFORCE and previously:
                # Снимаем фильтр до записи, иначе граница API вырежет ровно то,
                # что мы возвращаем.
                dimension_squad_policy.clear_for(subscription.remnawave_uuid)
                applied = await self._push_squads(
                    api,
                    subscription,
                    [str(uuid) for uuid in (subscription.connected_squads or [])],
                    report=report,
                )
                if not applied:
                    dimension_squad_policy.set_for(subscription.remnawave_uuid, previously)
                    return
            row.blocked_at = None
            row.block_reason = None
            row.stripped_squads = []

        if not report_transition:
            return

        report.transitions.append(self._transition(change, applied=applied, stripped=stripped))

    @staticmethod
    def _count(change: _PlannedChange, report: ReconcileReport) -> None:
        """Счётчики отчёта — одни и те же во всех режимах."""
        if change.action is EnforcementAction.BLOCK:
            report.blocked += 1
        elif change.action is EnforcementAction.UNBLOCK:
            report.unblocked += 1
        elif change.action is EnforcementAction.HOLD:
            report.held += 1
        elif change.action is EnforcementAction.REFUSE:
            report.refused += 1

    @staticmethod
    def _transition(
        change: _PlannedChange,
        *,
        applied: bool,
        stripped: tuple[str, ...],
    ) -> DimensionTransition:
        return DimensionTransition(
            subscription_id=change.subscription.id,
            user_id=change.subscription.user_id,
            remnawave_uuid=change.subscription.remnawave_uuid,
            spec=change.spec,
            state=change.state,
            action=change.action,
            reason=change.reason,
            applied=applied,
            stripped_squads=stripped,
        )

    async def _push_squads(
        self,
        api: Any,
        subscription: Subscription,
        squads: list[str],
        *,
        report: ReconcileReport,
    ) -> bool:
        """Единственная исходящая запись в панель во всём модуле."""
        if api is None:
            return False
        from app.services.grace_access_runtime import update_panel_user_grace_safe

        try:
            await update_panel_user_grace_safe(
                api,
                subscription.id,
                uuid=subscription.remnawave_uuid,
                active_internal_squads=squads,
            )
        except Exception as e:
            report.panel_errors += 1
            logger.warning(
                'Не удалось применить ограничение измерения в панели',
                subscription_id=subscription.id,
                error=e,
            )
            return False
        report.panel_writes += 1
        return True

    # ------------------------------ карта блокировок ------------------------------

    async def publish_policy(self, db: AsyncSession) -> int:
        """Перекладывает актуальные блокировки в карту, которую читает граница API.

        Вызывается в конце каждого цикла и при старте процесса: без этого после
        рестарта первая же посторонняя запись в панель вернула бы снятые сквады.
        """
        result = await db.execute(
            select(Subscription.remnawave_uuid, SubscriptionTrafficDimension.stripped_squads)
            .join(
                SubscriptionTrafficDimension,
                SubscriptionTrafficDimension.subscription_id == Subscription.id,
            )
            .where(
                SubscriptionTrafficDimension.blocked_at.isnot(None),
                Subscription.remnawave_uuid.isnot(None),
            )
        )
        mapping: dict[str, set[str]] = {}
        for remnawave_uuid, stripped in result.all():
            if not remnawave_uuid or not stripped:
                continue
            mapping.setdefault(str(remnawave_uuid), set()).update(str(uuid).lower() for uuid in stripped)

        from app.services.grace_access_runtime import get_open_grace_subscription_ids

        grace_ids = await get_open_grace_subscription_ids(db)
        if grace_ids:
            open_uuids = await db.execute(select(Subscription.remnawave_uuid).where(Subscription.id.in_(grace_ids)))
            for remnawave_uuid in open_uuids.scalars().all():
                mapping.pop(str(remnawave_uuid), None)

        dimension_squad_policy.replace_all(mapping)
        return len(mapping)


traffic_dimension_reconciler = TrafficDimensionReconciler()


async def restore_policy_on_startup() -> int:
    """Поднимает карту блокировок из БД до того, как пойдут записи в панель."""
    try:
        async with AsyncSessionLocal() as db:
            count = await traffic_dimension_reconciler.publish_policy(db)
        if count:
            logger.info('Восстановлены блокировки измерений трафика', subscriptions=count)
        return count
    except Exception as e:
        logger.error('Не удалось восстановить блокировки измерений трафика', error=e)
        return 0


async def reconcile_after_ledger(squad_index: Mapping[str, frozenset[str]] | None = None) -> ReconcileReport:
    """Хук для планировщика журнала: пересчитали расход — сразу и решили."""
    await asyncio.sleep(0)
    return await traffic_dimension_reconciler.reconcile(squad_index=squad_index)
