"""Собственный журнал посуточного пер-инбаунд трафика.

Зачем он вообще нужен: панель раз в неделю (понедельник 00:30 UTC) делает
TRUNCATE всей таблицы пер-инбаунд истории. Не «удаляет старое» — вычищает
целиком. Поэтому расчётное окно длиннее недели из панели не восстанавливается
в принципе, и единственный способ считать месячную квоту измерения — копить
собственные отсчёты.

Как это работает:

* Панель раскладывает трафик по UTC-суткам и накапливает дельты в бакет
  текущего дня: значение за день только растёт, пока день открыт, и замерзает
  после закрытия. Отсюда апсерт ``bytes = GREATEST(existing, incoming)`` —
  повторное чтение идемпотентно, а очистка на стороне панели ничего не ломает.
* Израсходованное за окно = сумма собственных наблюдений с ``window_start``,
  а не то, что панель отдала прямо сейчас.
* Граница окна берётся из режима сброса трафика тарифа — того же, что уходит в
  панель как ``trafficLimitStrategy``, — чтобы измерение сбрасывалось вместе с
  обычным трафиком, а не по своему календарю.

Чего журнал принципиально не умеет: восстановить сутки, которые он проспал.
Если бот стоял и панель успела сделать TRUNCATE, эти дни потеряны навсегда.
Такая дыра не прячется, а записывается в ``coverage_from``: расход за окно
заведомо занижен, и блокировать по нему нельзя.
"""

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import (
    Subscription,
    SubscriptionStatus,
    TrafficDimensionSample,
)
from app.services.traffic_dimension_meter import BYTES_IN_GB, InboundUsageMatrix
from app.services.traffic_dimensions import (
    TrafficDimensionSpec,
    ensure_dimension_row,
    traffic_dimensions,
)


logger = structlog.get_logger(__name__)

# Статусы, за которыми есть смысл следить: у остальных доступа к панели нет.
_SAMPLED_STATUSES = (
    SubscriptionStatus.ACTIVE.value,
    SubscriptionStatus.TRIAL.value,
)


# ============================ Границы расчётного окна ============================


def _last_monthly_anniversary(anchor: date, today: date) -> date:
    """Последняя годовщина дня месяца `anchor`, не позже `today`.

    Для 31-го числа в коротком месяце берётся последний день месяца — так же,
    как это делает любой биллинг, у которого нет 31 февраля.
    """
    day = anchor.day
    year, month = today.year, today.month
    for _ in range(2):
        last_day_of_month = (date(year + (month // 12), (month % 12) + 1, 1) - timedelta(days=1)).day
        candidate = date(year, month, min(day, last_day_of_month))
        if candidate <= today:
            return candidate
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return today


def window_start_for(
    strategy: str | None,
    *,
    today: date,
    subscription_start: date,
    last_reset_at: date | None = None,
) -> date:
    """Первые UTC-сутки текущего расчётного окна измерения.

    Повторяет семантику панельного ``trafficLimitStrategy``, чтобы измерение
    обнулялось ровно тогда же, когда обычный трафик. Результат никогда не
    уходит раньше начала подписки: трафик до её появления — не её трафик.
    """
    name = (strategy or '').strip().upper()
    if name == 'DAY':
        start = today
    elif name == 'WEEK':
        start = today - timedelta(days=today.weekday())
    elif name == 'MONTH':
        start = today.replace(day=1)
    elif name == 'MONTH_ROLLING':
        # Панель катит окно от собственного lastTrafficResetAt; если его не
        # передали, годовщина начала подписки — тот же день месяца.
        start = last_reset_at if last_reset_at else _last_monthly_anniversary(subscription_start, today)
    else:
        # NO_RESET и всё незнакомое: считаем за всё время жизни подписки.
        start = subscription_start
    return max(min(start, today), subscription_start)


def resolve_window_start(subscription: Subscription, *, today: date, last_reset_at: date | None = None) -> date:
    """`window_start_for`, вытаскивающий режим сброса из тарифа подписки."""
    from app.services.subscription_service import get_traffic_reset_strategy

    strategy = get_traffic_reset_strategy(getattr(subscription, 'tariff', None))
    start_dt = getattr(subscription, 'start_date', None) or getattr(subscription, 'created_at', None)
    subscription_start = start_dt.date() if start_dt else today
    return window_start_for(
        getattr(strategy, 'value', strategy),
        today=today,
        subscription_start=subscription_start,
        last_reset_at=last_reset_at,
    )


# ============================== Топология сквадов ==============================


def squad_inbound_index(squads: Iterable[Any]) -> dict[str, frozenset[str]]:
    """`{squad_uuid: {inbound_uuid}}` из ответа `get_internal_squads()`.

    Один вызов на цикл: карта общая для всех подписок и меняется только когда
    администратор трогает сквады в панели.
    """
    index: dict[str, frozenset[str]] = {}
    for squad in squads or []:
        squad_uuid = str(getattr(squad, 'uuid', '') or '').lower()
        if not squad_uuid:
            continue
        inbounds = {
            str(getattr(inbound, 'uuid', '') or '').lower()
            for inbound in (getattr(squad, 'inbounds', None) or [])
            if getattr(inbound, 'uuid', None)
        }
        index[squad_uuid] = frozenset(inbounds)
    return index


def reachable_inbounds(
    connected_squads: Iterable[str] | None, squad_index: Mapping[str, frozenset[str]]
) -> frozenset[str]:
    """Инбаунды, до которых подписка вообще может дотянуться.

    Пустая карта сквадов означает «не знаем топологию», а не «доступа нет»:
    в этом случае возвращается пустое множество, и вызывающий обязан считать
    это неизвестностью, а не поводом пропустить подписку.
    """
    result: set[str] = set()
    for squad_uuid in connected_squads or []:
        result |= squad_index.get(str(squad_uuid).lower(), frozenset())
    return frozenset(result)


def applicable_specs(
    specs: Sequence[TrafficDimensionSpec],
    reachable: frozenset[str],
) -> tuple[TrafficDimensionSpec, ...]:
    """Измерения, трафик по которым подписка физически может нагенерить."""
    return tuple(spec for spec in specs if spec.inbound_uuids & reachable)


# ================================ Запись отсчётов ================================


def sample_rows_from_matrix(
    remnawave_uuid: str,
    matrix: InboundUsageMatrix,
    wanted_inbounds: frozenset[str],
    *,
    fetched_at: datetime,
) -> list[dict[str, Any]]:
    """Ячейки матрицы, пригодные для записи в журнал.

    Матрица без посуточной детализации (`has_daily_series=False`, старая панель)
    не пишется вовсе: она сворачивает всё окно в одни сутки, и апсерт
    ``GREATEST`` навсегда заморозил бы этот ком на случайном дне.
    """
    if not remnawave_uuid or not matrix.has_daily_series or not wanted_inbounds:
        return []
    return [
        {
            'remnawave_uuid': remnawave_uuid,
            'inbound_uuid': inbound_uuid,
            'usage_date': usage_date,
            'bytes': int(value),
            'fetched_at': fetched_at,
        }
        for (usage_date, inbound_uuid), value in matrix.cells.items()
        if inbound_uuid in wanted_inbounds and value > 0
    ]


async def store_samples(db: AsyncSession, rows: Sequence[Mapping[str, Any]]) -> int:
    """Апсертит наблюдения по `bytes = GREATEST(existing, incoming)`.

    Значение никогда не уменьшается: панельный бакет дня монотонно растёт, и
    любое меньшее число — это либо ещё не закрытые сутки, либо уже случившийся
    TRUNCATE. Ни то, ни другое не повод терять уже снятое.
    """
    if not rows:
        return 0

    table = TrafficDimensionSample.__table__
    dialect = db.bind.dialect.name if db.bind is not None else ''
    conflict_columns = ['remnawave_uuid', 'inbound_uuid', 'usage_date']

    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert as dialect_insert

        greatest = func.greatest
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

        # В SQLite скалярный max() от двух аргументов — тот же GREATEST.
        greatest = func.max
    else:
        return await _store_samples_portable(db, rows)

    stmt = dialect_insert(table).values(list(rows))
    stmt = stmt.on_conflict_do_update(
        index_elements=conflict_columns,
        set_={
            'bytes': greatest(table.c.bytes, stmt.excluded.bytes),
            'fetched_at': stmt.excluded.fetched_at,
        },
    )
    await db.execute(stmt)
    return len(rows)


async def _store_samples_portable(db: AsyncSession, rows: Sequence[Mapping[str, Any]]) -> int:
    """Тот же апсерт без ON CONFLICT — для диалектов, где его нет."""
    written = 0
    for row in rows:
        existing = await db.execute(
            select(TrafficDimensionSample).where(
                TrafficDimensionSample.remnawave_uuid == row['remnawave_uuid'],
                TrafficDimensionSample.inbound_uuid == row['inbound_uuid'],
                TrafficDimensionSample.usage_date == row['usage_date'],
            )
        )
        sample = existing.scalar_one_or_none()
        if sample is None:
            db.add(TrafficDimensionSample(**row))
        elif int(row['bytes']) > int(sample.bytes or 0):
            sample.bytes = int(row['bytes'])
            sample.fetched_at = row['fetched_at']
        else:
            continue
        written += 1
    return written


# ================================ Чтение окна ================================


@dataclass(frozen=True)
class WindowUsage:
    """Что журнал знает о расходе подписки за окно."""

    by_inbound: dict[str, int]
    covered_from: date | None

    def bytes_for(self, inbound_uuids: Iterable[str]) -> int:
        return sum(self.by_inbound.get(str(uuid).lower(), 0) for uuid in inbound_uuids)


async def window_usage(
    db: AsyncSession,
    remnawave_uuid: str,
    *,
    window_start: date,
    window_end: date,
) -> WindowUsage:
    """Суммы по инбаундам за окно плюс первые сутки, которые журнал реально видел."""
    if not remnawave_uuid:
        return WindowUsage(by_inbound={}, covered_from=None)

    result = await db.execute(
        select(
            TrafficDimensionSample.inbound_uuid,
            func.sum(TrafficDimensionSample.bytes),
            func.min(TrafficDimensionSample.usage_date),
        )
        .where(
            TrafficDimensionSample.remnawave_uuid == remnawave_uuid,
            TrafficDimensionSample.usage_date >= window_start,
            TrafficDimensionSample.usage_date <= window_end,
        )
        .group_by(TrafficDimensionSample.inbound_uuid)
    )

    by_inbound: dict[str, int] = {}
    covered_from: date | None = None
    for inbound_uuid, total, first_day in result.all():
        by_inbound[str(inbound_uuid).lower()] = int(total or 0)
        if first_day is not None and (covered_from is None or first_day < covered_from):
            covered_from = first_day
    return WindowUsage(by_inbound=by_inbound, covered_from=covered_from)


async def prune_samples(db: AsyncSession, *, keep_days: int, today: date) -> int:
    """Чистит наблюдения старше `keep_days`.

    Окно длиннее месяца не бывает даже у MONTH_ROLLING, так что запас нужен
    только на разбор жалоб и на `/traffic_why`.
    """
    if keep_days <= 0:
        return 0
    cutoff = today - timedelta(days=keep_days)
    result = await db.execute(delete(TrafficDimensionSample).where(TrafficDimensionSample.usage_date < cutoff))
    return int(result.rowcount or 0)


# ============================== Пересчёт состояния ==============================


@dataclass
class DimensionMeasurement:
    """Итог одного пересчёта измерения у одной подписки."""

    spec: TrafficDimensionSpec
    used_gb: float
    known: bool
    window_start: date
    coverage_from: date | None

    @property
    def has_coverage_gap(self) -> bool:
        """Начало окна не покрыто журналом — расход занижен."""
        return self.known and (self.coverage_from is None or self.coverage_from > self.window_start)


@dataclass
class LedgerCycleStats:
    """Счётчики одного прохода — уходят в лог и в админский статус."""

    subscriptions_scanned: int = 0
    subscriptions_sampled: int = 0
    samples_written: int = 0
    measurements: int = 0
    unknown: int = 0
    coverage_gaps: int = 0
    pruned: int = 0
    errors: int = 0
    # Карта сквадов этого цикла: реконсилятор берёт её готовой, чтобы не
    # спрашивать панель второй раз за те же полминуты.
    squad_index: Mapping[str, frozenset[str]] | None = None

    def as_dict(self) -> dict[str, int]:
        return {
            'subscriptions_scanned': self.subscriptions_scanned,
            'subscriptions_sampled': self.subscriptions_sampled,
            'samples_written': self.samples_written,
            'measurements': self.measurements,
            'unknown': self.unknown,
            'coverage_gaps': self.coverage_gaps,
            'pruned': self.pruned,
            'errors': self.errors,
        }


class TrafficDimensionLedgerService:
    """Снимает пер-инбаунд трафик с панели и держит расход измерений в актуальном виде.

    Пишет только цифры. Блокировками, уведомлениями и панельными лимитами
    занимается слой применения — здесь нет ни одного исходящего изменения в
    панели, чтобы сбор данных нельзя было превратить в аварию.
    """

    def __init__(self) -> None:
        from app.services.remnawave_service import RemnaWaveService

        self.remnawave_service = RemnaWaveService()
        self._last_prune_date: date | None = None

    # ------------------------------ настройки ------------------------------

    def get_interval_seconds(self) -> int:
        minutes = max(int(getattr(settings, 'TRAFFIC_DIMENSION_SAMPLE_INTERVAL_MINUTES', 180) or 180), 5)
        return minutes * 60

    def get_batch_size(self) -> int:
        return max(int(getattr(settings, 'TRAFFIC_CHECK_BATCH_SIZE', 1000) or 1000), 1)

    def get_concurrency(self) -> int:
        return max(int(getattr(settings, 'TRAFFIC_CHECK_CONCURRENCY', 10) or 10), 1)

    def get_retention_days(self) -> int:
        return max(int(getattr(settings, 'TRAFFIC_DIMENSION_SAMPLE_RETENTION_DAYS', 120) or 0), 0)

    # ------------------------------ одна подписка ------------------------------

    async def refresh_subscription(
        self,
        db: AsyncSession,
        subscription: Subscription,
        specs: Sequence[TrafficDimensionSpec],
        squad_index: Mapping[str, frozenset[str]],
        *,
        api: Any,
        today: date,
        stats: LedgerCycleStats | None = None,
    ) -> list[DimensionMeasurement]:
        """Снимает трафик подписки, пишет наблюдения и обновляет строки состояния.

        Ничего не коммитит: границы транзакции задаёт вызывающий.
        """
        stats = stats or LedgerCycleStats()
        remnawave_uuid = getattr(subscription, 'remnawave_uuid', None)
        if not remnawave_uuid:
            return []

        wanted = applicable_specs(specs, reachable_inbounds(subscription.connected_squads, squad_index))
        if not wanted:
            # Ни одного инбаунда измерения в сквадах подписки — считать нечего,
            # и это не «ноль расхода», а «расход невозможен».
            return []

        window_start = resolve_window_start(subscription, today=today)
        reading = await self.remnawave_service.read_inbound_usage(remnawave_uuid, window_start, today, api=api)
        stats.subscriptions_sampled += 1
        return await self._apply_reading(
            db, subscription, wanted, reading=reading, window_start=window_start, today=today, stats=stats
        )

    async def _write_state(
        self,
        db: AsyncSession,
        subscription: Subscription,
        spec: TrafficDimensionSpec,
        *,
        usage: WindowUsage,
        reading_known: bool,
        has_daily_series: bool,
        live_bytes: int,
        window_start: date,
    ) -> DimensionMeasurement:
        row = await ensure_dimension_row(db, subscription, spec)

        if not reading_known:
            # Панель не ответила. Предыдущее значение — лучшее, что у нас есть:
            # затирать его нулём значило бы подарить квоту всем разом.
            return DimensionMeasurement(
                spec=spec,
                used_gb=float(row.used_gb or 0.0),
                known=bool(row.measured_known),
                window_start=row.window_start or window_start,
                coverage_from=row.coverage_from,
            )

        window_rolled = row.window_start != window_start
        used_bytes = usage.bytes_for(spec.inbound_uuids)
        used_gb = used_bytes / BYTES_IN_GB
        coverage_from = usage.covered_from

        if not has_daily_series:
            # Старая панель без посуточных рядов: в журнал писать нечего, зато
            # живое число за окно есть. Держим счётчик монотонным внутри окна,
            # иначе недельная очистка панели роняла бы его каждый понедельник.
            used_gb = live_bytes / BYTES_IN_GB
            if not window_rolled:
                used_gb = max(used_gb, float(row.used_gb or 0.0))
                coverage_from = row.coverage_from
            else:
                coverage_from = None

        row.used_gb = used_gb
        row.window_start = window_start
        row.coverage_from = coverage_from
        row.measured_at = datetime.now(UTC)
        row.measured_known = True

        return DimensionMeasurement(
            spec=spec,
            used_gb=used_gb,
            known=True,
            window_start=window_start,
            coverage_from=coverage_from,
        )

    # -------------------------------- весь цикл --------------------------------

    async def run_cycle(self) -> LedgerCycleStats:
        """Проходит по активным подпискам и обновляет расход всех измерений.

        Стоимость — один запрос к панели на подписку, у которой есть доступ
        хотя бы к одному инбаунду измерения. Если администратор не завёл ни
        одного измерения, цикл не делает ни одного запроса.
        """
        stats = LedgerCycleStats()
        today = datetime.now(UTC).date()

        async with AsyncSessionLocal() as db:
            specs = await traffic_dimensions.measurable(db)
            if not specs:
                logger.debug('Измеримых измерений трафика нет — цикл журнала пропущен')
                return stats

            try:
                async with AsyncExitStack() as stack:
                    api = await stack.enter_async_context(self.remnawave_service.get_api_client())
                    squad_index = squad_inbound_index(await api.get_internal_squads())
                    if not squad_index:
                        logger.warning('Панель не вернула ни одного сквада — цикл журнала пропущен')
                        stats.errors += 1
                        return stats
                    stats.squad_index = squad_index
                    await self._sample_all(db, specs, squad_index, api=api, today=today, stats=stats)
            except Exception as e:
                stats.errors += 1
                logger.error('Ошибка цикла журнала измерений трафика', error=e)
                await db.rollback()
                return stats

            if self._last_prune_date != today:
                try:
                    stats.pruned = await prune_samples(db, keep_days=self.get_retention_days(), today=today)
                    await db.commit()
                    self._last_prune_date = today
                except Exception as e:
                    await db.rollback()
                    logger.warning('Не удалось почистить наблюдения журнала', error=e)

        logger.info('📐 Цикл журнала измерений трафика завершён', **stats.as_dict())
        return stats

    async def _sample_all(
        self,
        db: AsyncSession,
        specs: Sequence[TrafficDimensionSpec],
        squad_index: Mapping[str, frozenset[str]],
        *,
        api: Any,
        today: date,
        stats: LedgerCycleStats,
    ) -> None:
        semaphore = asyncio.Semaphore(self.get_concurrency())
        batch_size = self.get_batch_size()
        offset = 0

        while True:
            result = await db.execute(
                select(Subscription)
                .options(selectinload(Subscription.tariff))
                .where(
                    Subscription.status.in_(_SAMPLED_STATUSES),
                    Subscription.remnawave_uuid.isnot(None),
                )
                .order_by(Subscription.id)
                .offset(offset)
                .limit(batch_size)
            )
            batch = list(result.scalars().all())
            if not batch:
                break
            stats.subscriptions_scanned += len(batch)

            # Панельные чтения идут параллельно, запись в БД — строго по
            # очереди: AsyncSession не переживает конкурентное использование.
            async def read(subscription: Subscription, window_start: date):
                async with semaphore:
                    try:
                        return await self.remnawave_service.read_inbound_usage(
                            subscription.remnawave_uuid, window_start, today, api=api
                        )
                    except Exception as e:
                        logger.warning(
                            'Не удалось снять пер-инбаунд трафик',
                            subscription_id=subscription.id,
                            error=e,
                        )
                        return None

            planned: list[tuple[Subscription, tuple[TrafficDimensionSpec, ...], date]] = []
            for subscription in batch:
                wanted = applicable_specs(specs, reachable_inbounds(subscription.connected_squads, squad_index))
                if wanted:
                    planned.append((subscription, wanted, resolve_window_start(subscription, today=today)))

            readings = await asyncio.gather(
                *(read(subscription, window_start) for subscription, _, window_start in planned)
            )

            for (subscription, wanted, window_start), reading in zip(planned, readings, strict=True):
                if reading is None:
                    stats.errors += 1
                    continue
                stats.subscriptions_sampled += 1
                try:
                    await self._apply_reading(
                        db,
                        subscription,
                        wanted,
                        reading=reading,
                        window_start=window_start,
                        today=today,
                        stats=stats,
                    )
                except Exception as e:
                    stats.errors += 1
                    logger.warning('Не удалось записать состояние измерений', subscription_id=subscription.id, error=e)

            try:
                await db.commit()
            except Exception as e:
                await db.rollback()
                stats.errors += 1
                logger.error('Не удалось сохранить батч журнала измерений', error=e)

            offset += batch_size

    async def _apply_reading(
        self,
        db: AsyncSession,
        subscription: Subscription,
        wanted: Sequence[TrafficDimensionSpec],
        *,
        reading,
        window_start: date,
        today: date,
        stats: LedgerCycleStats,
    ) -> list[DimensionMeasurement]:
        """Пишет наблюдения чтения в журнал и пересчитывает по нему состояния."""
        remnawave_uuid = subscription.remnawave_uuid

        if reading.known:
            all_inbounds = frozenset().union(*(spec.inbound_uuids for spec in wanted))
            rows = sample_rows_from_matrix(
                remnawave_uuid,
                reading.matrix,
                all_inbounds,
                fetched_at=datetime.now(UTC),
            )
            stats.samples_written += await store_samples(db, rows)

        usage = await window_usage(db, remnawave_uuid, window_start=window_start, window_end=today)

        measurements: list[DimensionMeasurement] = []
        for spec in wanted:
            measurement = await self._write_state(
                db,
                subscription,
                spec,
                usage=usage,
                reading_known=reading.known,
                has_daily_series=reading.matrix.has_daily_series,
                live_bytes=reading.total_for(spec.inbound_uuids) if reading.known else 0,
                window_start=window_start,
            )
            measurements.append(measurement)
            stats.measurements += 1
            if not measurement.known:
                stats.unknown += 1
            elif measurement.has_coverage_gap:
                stats.coverage_gaps += 1
        return measurements


class TrafficDimensionLedgerScheduler:
    """Отдельный цикл, не завязанный на мониторинг злоупотреблений.

    Своя петля, а не хвост суточной проверки: та отключаемая, а журнал —
    источник цифр для блокировок, и молча переставать собирать данные ему
    нельзя. Интервал короче суток нужен по другой причине: панель делает
    TRUNCATE в понедельник 00:30 UTC, и воскресные сутки успевают закрыться
    всего за полчаса до этого.
    """

    def __init__(self, service: TrafficDimensionLedgerService) -> None:
        self.service = service
        self._task: asyncio.Task | None = None
        self._is_running = False

    async def start(self) -> None:
        if self._is_running:
            logger.warning('Планировщик журнала измерений трафика уже запущен')
            return
        self._is_running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            '🚀 Запуск журнала измерений трафика',
            interval_minutes=self.service.get_interval_seconds() // 60,
        )

    async def stop(self) -> None:
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info('ℹ️ Планировщик журнала измерений трафика остановлен')

    async def _loop(self) -> None:
        interval = self.service.get_interval_seconds()
        while self._is_running:
            try:
                stats = await self.service.run_cycle()
                # Пересчитали расход — сразу и решаем, кого ограничить, пока
                # карта сквадов свежая и панель не надо спрашивать второй раз.
                await self._reconcile(stats)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error('❌ Ошибка в цикле журнала измерений трафика', error=e)
            try:
                await asyncio.sleep(self.service.get_interval_seconds())
            except asyncio.CancelledError:
                break
            interval = self.service.get_interval_seconds()
        logger.debug('Цикл журнала измерений трафика завершён', interval=interval)

    async def _reconcile(self, stats: LedgerCycleStats) -> None:
        """Отдаёт свежие цифры реконсилятору.

        Импорт отложенный: реконсилятор читает журнал, и связывать модули на
        уровне импорта незачем.
        """
        from app.services.traffic_dimension_reconciler import reconcile_after_ledger

        try:
            await reconcile_after_ledger(stats.squad_index)
        except Exception as e:
            logger.error('❌ Ошибка цикла ограничений измерений трафика', error=e)

    async def run_now(self) -> LedgerCycleStats:
        stats = await self.service.run_cycle()
        await self._reconcile(stats)
        return stats


traffic_dimension_ledger_service = TrafficDimensionLedgerService()
traffic_dimension_ledger_scheduler = TrafficDimensionLedgerScheduler(traffic_dimension_ledger_service)
