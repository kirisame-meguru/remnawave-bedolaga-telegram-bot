"""Реестр измерений трафика.

Обычный трафик — это измерение `base`: его состояние живёт в колонках
`subscriptions.traffic_limit_gb / traffic_used_gb / purchased_traffic_gb`, и
переносить его в новые таблицы никто не собирается — `traffic_limit_gb`
упоминается в коде сотни раз, и большой рефакторинг стоил бы дороже, чем даёт.
Поэтому реестр описывает, **где лежит состояние измерения**, а вызывающий код
ходит через `DimensionState`, не зная, читает он старые колонки или строку
`subscription_traffic_dimensions`.

Измерения задаёт администратор строками в БД, поэтому заголовки хранятся в
самой строке (словарь по языкам + запасной), а не ключами локализации: ключ
локализации нельзя завести на лету, а тест целостности локалей требует, чтобы
все ключи существовали статически во всех пяти языках.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    BASE_TRAFFIC_DIMENSION_KEY,
    Subscription,
    SubscriptionTrafficDimension,
    TariffTrafficDimension,
    TrafficAccountingMode,
    TrafficDimension,
    TrafficDimensionEnforcement,
    TrafficPurchase,
)


logger = structlog.get_logger(__name__)

BASE_KEY = BASE_TRAFFIC_DIMENSION_KEY
DEFAULT_DISCOUNT_CATEGORY = 'traffic'

_CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class TrafficDimensionSpec:
    """Неизменяемый снимок строки `traffic_dimensions`."""

    id: int
    key: str
    titles: dict[str, str]
    fallback_title: str
    icon: str
    inbound_uuids: frozenset[str]
    default_limit_gb: int
    accounting_mode: str
    enforcement: str
    discount_category: str
    is_enabled: bool
    is_builtin: bool
    position: int

    @property
    def is_base(self) -> bool:
        return self.key == BASE_KEY

    @property
    def shields_base_quota(self) -> bool:
        """Нужно ли поднимать лимит в панели на израсходованное этим измерением."""
        return not self.is_base and self.accounting_mode == TrafficAccountingMode.SHIELDED.value

    @property
    def strips_squads(self) -> bool:
        return self.enforcement == TrafficDimensionEnforcement.SQUAD_STRIP.value

    def title(self, language: str | None = None) -> str:
        titles = self.titles or {}
        if language and titles.get(language):
            return titles[language]
        default_language = getattr(settings, 'DEFAULT_LANGUAGE', 'ru')
        if titles.get(default_language):
            return titles[default_language]
        if titles:
            return next(iter(titles.values()))
        return self.fallback_title or self.key

    def label(self, language: str | None = None) -> str:
        title = self.title(language)
        return f'{self.icon} {title}'.strip() if self.icon else title


@dataclass(frozen=True)
class DimensionState:
    """Текущие цифры измерения у конкретной подписки.

    `limit_gb == 0` означает безлимит — так же, как у обычного трафика.
    `used_known=False` означает, что последнее измерение не удалось: нулю в
    `used_gb` в этом случае верить нельзя, и решения на нём строить запрещено.
    """

    spec: TrafficDimensionSpec
    base_limit_gb: int
    purchased_gb: int
    limit_gb: int
    used_gb: float
    used_known: bool
    blocked: bool
    block_reason: str | None
    stripped_squads: tuple[str, ...]
    window_start: date | None = None
    coverage_from: date | None = None

    @property
    def is_unlimited(self) -> bool:
        return self.limit_gb == 0

    @property
    def has_coverage_gap(self) -> bool:
        """Начало окна не покрыто журналом наблюдений — расход занижен.

        Панель хранит пер-инбаунд историю неделю, поэтому всё, что бот не успел
        снять (первый запуск измерения, длительный простой), потеряно навсегда.
        """
        if self.spec.is_base or self.window_start is None:
            return False
        return self.coverage_from is None or self.coverage_from > self.window_start

    @property
    def is_enforceable(self) -> bool:
        """Можно ли принимать решение о блокировке по этой цифре."""
        return self.used_known and not self.has_coverage_gap

    @property
    def used_percent(self) -> float:
        if not self.limit_gb:
            return 0.0
        return min((self.used_gb / self.limit_gb) * 100, 100.0)

    @property
    def is_exhausted(self) -> bool:
        """Квота исчерпана и это точно известно."""
        return self.used_known and self.limit_gb > 0 and self.used_gb >= self.limit_gb


def _resolve_accounting_mode(raw: str | None) -> str:
    value = (raw or getattr(settings, 'TRAFFIC_DIMENSION_ACCOUNTING_MODE', '') or '').strip().lower()
    if value not in {mode.value for mode in TrafficAccountingMode}:
        return TrafficAccountingMode.SUBQUOTA.value
    return value


def _spec_from_row(row: TrafficDimension) -> TrafficDimensionSpec:
    raw_uuids = row.inbound_uuids or []
    return TrafficDimensionSpec(
        id=row.id,
        key=row.key,
        titles=dict(row.title or {}),
        fallback_title=row.fallback_title or '',
        icon=row.icon or '',
        inbound_uuids=frozenset(str(uuid).strip().lower() for uuid in raw_uuids if str(uuid).strip()),
        default_limit_gb=int(row.default_limit_gb or 0),
        accounting_mode=_resolve_accounting_mode(row.accounting_mode),
        enforcement=row.enforcement or TrafficDimensionEnforcement.SQUAD_STRIP.value,
        discount_category=row.discount_category or DEFAULT_DISCOUNT_CATEGORY,
        is_enabled=bool(row.is_enabled),
        is_builtin=bool(row.is_builtin),
        position=int(row.position or 0),
    )


class TrafficDimensionRegistry:
    """Кэширующий доступ к строкам `traffic_dimensions`.

    Кэш живёт в процессе: измерения меняются только руками администратора, а
    читаются на каждой отрисовке подписки и в каждом цикле измерения.
    """

    def __init__(self) -> None:
        self._specs: tuple[TrafficDimensionSpec, ...] | None = None
        self._loaded_at: datetime | None = None

    def invalidate(self) -> None:
        self._specs = None
        self._loaded_at = None

    def _is_fresh(self) -> bool:
        if self._specs is None or self._loaded_at is None:
            return False
        return (datetime.now(UTC) - self._loaded_at).total_seconds() < _CACHE_TTL_SECONDS

    async def all(self, db: AsyncSession) -> tuple[TrafficDimensionSpec, ...]:
        if self._is_fresh():
            return self._specs  # type: ignore[return-value]
        result = await db.execute(select(TrafficDimension).order_by(TrafficDimension.position, TrafficDimension.id))
        specs = tuple(_spec_from_row(row) for row in result.scalars().all())
        self._specs = specs
        self._loaded_at = datetime.now(UTC)
        return specs

    async def enabled(self, db: AsyncSession) -> tuple[TrafficDimensionSpec, ...]:
        """Измерения, которые надо показывать и обслуживать.

        `base` остаётся всегда: выключить обычный трафик нельзя.
        """
        specs = await self.all(db)
        return tuple(spec for spec in specs if spec.is_base or spec.is_enabled)

    async def non_base(self, db: AsyncSession) -> tuple[TrafficDimensionSpec, ...]:
        """Включённые измерения кроме обычного трафика."""
        return tuple(spec for spec in await self.enabled(db) if not spec.is_base)

    async def measurable(self, db: AsyncSession) -> tuple[TrafficDimensionSpec, ...]:
        """Измерения, которые есть смысл считать: не базовые и с инбаундами."""
        return tuple(spec for spec in await self.non_base(db) if spec.inbound_uuids)

    async def by_key(self, db: AsyncSession, key: str) -> TrafficDimensionSpec | None:
        for spec in await self.all(db):
            if spec.key == key:
                return spec
        return None

    async def base(self, db: AsyncSession) -> TrafficDimensionSpec | None:
        return await self.by_key(db, BASE_KEY)


traffic_dimensions = TrafficDimensionRegistry()


def _base_state(spec: TrafficDimensionSpec, subscription: Subscription) -> DimensionState:
    """Состояние обычного трафика, прочитанное из старых колонок подписки."""
    total = int(subscription.traffic_limit_gb or 0)
    purchased = int(getattr(subscription, 'purchased_traffic_gb', 0) or 0)
    return DimensionState(
        spec=spec,
        base_limit_gb=max(total - purchased, 0) if total else 0,
        purchased_gb=purchased,
        limit_gb=total,
        used_gb=float(subscription.traffic_used_gb or 0.0),
        # Обычный трафик приходит из панели вместе с самим пользователем:
        # отдельного измерения, которое могло бы не удаться, здесь нет.
        used_known=True,
        blocked=False,
        block_reason=None,
        stripped_squads=(),
    )


def _row_state(spec: TrafficDimensionSpec, row: SubscriptionTrafficDimension | None) -> DimensionState:
    if row is None:
        # Тариф выдал измерение, но состояния ещё нет — значение по умолчанию.
        return DimensionState(
            spec=spec,
            base_limit_gb=spec.default_limit_gb,
            purchased_gb=0,
            limit_gb=spec.default_limit_gb,
            used_gb=0.0,
            used_known=False,
            blocked=False,
            block_reason=None,
            stripped_squads=(),
        )
    return DimensionState(
        spec=spec,
        base_limit_gb=int(row.base_limit_gb or 0),
        purchased_gb=int(row.purchased_gb or 0),
        limit_gb=row.limit_gb,
        used_gb=float(row.used_gb or 0.0),
        used_known=bool(row.measured_known),
        blocked=row.blocked_at is not None,
        block_reason=row.block_reason,
        stripped_squads=tuple(row.stripped_squads or []),
        window_start=row.window_start,
        coverage_from=row.coverage_from,
    )


async def load_dimension_rows(
    db: AsyncSession,
    subscription_id: int,
) -> dict[int, SubscriptionTrafficDimension]:
    """Строки состояния подписки, разложенные по id измерения."""
    result = await db.execute(
        select(SubscriptionTrafficDimension).where(SubscriptionTrafficDimension.subscription_id == subscription_id)
    )
    return {row.dimension_id: row for row in result.scalars().all()}


async def get_dimension_states(
    db: AsyncSession,
    subscription: Subscription,
    *,
    specs: Sequence[TrafficDimensionSpec] | None = None,
) -> list[DimensionState]:
    """Состояния всех включённых измерений подписки, включая обычный трафик."""
    specs = list(specs) if specs is not None else list(await traffic_dimensions.enabled(db))
    if not specs:
        return []
    rows = await load_dimension_rows(db, subscription.id)
    states: list[DimensionState] = []
    for spec in specs:
        if spec.is_base:
            states.append(_base_state(spec, subscription))
        else:
            states.append(_row_state(spec, rows.get(spec.id)))
    return states


async def get_dimension_state(
    db: AsyncSession,
    subscription: Subscription,
    spec: TrafficDimensionSpec,
) -> DimensionState:
    if spec.is_base:
        return _base_state(spec, subscription)
    rows = await load_dimension_rows(db, subscription.id)
    return _row_state(spec, rows.get(spec.id))


async def resolve_tariff_dimension_limit(
    db: AsyncSession,
    subscription: Subscription,
    spec: TrafficDimensionSpec,
) -> int:
    """Сколько ГБ измерения включает тариф подписки.

    Тариф — источник правды о включённом объёме: администратор решает,
    входит ли измерение в тариф и в каком размере. Умолчание самого измерения
    остаётся запасным вариантом для подписок без тарифа (классический режим).

    Строка тарифа с `included_gb = 0` означает «измерение включено безлимитно»,
    ровно как ноль у обычного трафика; отсутствие строки — «тариф это измерение
    не включает», и тогда берётся умолчание измерения.
    """
    config = await load_tariff_dimension_config(db, getattr(subscription, 'tariff_id', None))
    row = config.get(spec.id)
    if row is None:
        return spec.default_limit_gb
    return int(row.included_gb or 0)


async def ensure_dimension_row(
    db: AsyncSession,
    subscription: Subscription,
    spec: TrafficDimensionSpec,
    *,
    base_limit_gb: int | None = None,
) -> SubscriptionTrafficDimension:
    """Возвращает строку состояния, создавая её при первом обращении.

    При создании объём берётся из настройки тарифа, а не из умолчания
    измерения: тариф — то место, где администратор решает, что и в каком
    размере входит в подписку. Уже созданную строку не трогаем — в ней может
    жить индивидуальная правка администратора по конкретному пользователю.

    Без коммита: вызывающий сам решает границы транзакции.
    """
    result = await db.execute(
        select(SubscriptionTrafficDimension).where(
            SubscriptionTrafficDimension.subscription_id == subscription.id,
            SubscriptionTrafficDimension.dimension_id == spec.id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = SubscriptionTrafficDimension(
            subscription_id=subscription.id,
            dimension_id=spec.id,
            base_limit_gb=(
                await resolve_tariff_dimension_limit(db, subscription, spec) if base_limit_gb is None else base_limit_gb
            ),
            purchased_gb=0,
            used_gb=0.0,
            measured_known=False,
        )
        db.add(row)
        await db.flush()
    elif base_limit_gb is not None:
        row.base_limit_gb = base_limit_gb
    return row


async def load_tariff_dimension_config(
    db: AsyncSession,
    tariff_id: int | None,
) -> dict[int, TariffTrafficDimension]:
    """Настройки измерений тарифа, разложенные по id измерения."""
    if not tariff_id:
        return {}
    result = await db.execute(select(TariffTrafficDimension).where(TariffTrafficDimension.tariff_id == tariff_id))
    return {row.dimension_id: row for row in result.scalars().all()}


async def sync_tariff_dimensions(db: AsyncSession, subscription: Subscription) -> int:
    """Приводит включённые объёмы измерений к текущему тарифу подписки.

    Нужна там, где тариф меняется: при покупке, смене и продлении. Без неё
    подписка навсегда оставалась бы с объёмами того тарифа, на котором её
    завели, а изменение настроек тарифа не доходило бы до существующих
    подписок.

    Трогает только `base_limit_gb`. Докупки (`purchased_gb`) не сбрасываются:
    они оплачены отдельно, живут своим сроком и смену тарифа переживают —
    ровно как докупки обычного трафика.
    """
    specs = await traffic_dimensions.non_base(db)
    if not specs:
        return 0

    changed = 0
    for spec in specs:
        included = await resolve_tariff_dimension_limit(db, subscription, spec)
        row = await ensure_dimension_row(db, subscription, spec)
        if (row.base_limit_gb or 0) != included:
            row.base_limit_gb = included
            changed += 1
    return changed


def dimension_keys(specs: Iterable[TrafficDimensionSpec]) -> tuple[str, ...]:
    return tuple(spec.key for spec in specs)


def format_dimension_value(state: DimensionState, *, unlimited_mark: str = '∞') -> str:
    """Только цифры: «6.0 / 10 ГБ», с пометкой о блокировке или отсутствии данных."""
    limit = unlimited_mark if state.is_unlimited else str(state.limit_gb)
    value = f'{state.used_gb:.1f} / {limit} ГБ'
    if state.blocked:
        value += ' — исчерпан'
    elif not state.used_known:
        # Ноль здесь означал бы «панель промолчала», а не «трафика нет».
        value += ' (нет данных)'
    return value


def format_dimension_usage(state: DimensionState, language: str | None = None) -> str:
    """Строка вида «⚪ WL Трафик (БС): 6.0 / 10 ГБ»."""
    return f'{state.spec.label(language)}: {format_dimension_value(state)}'


async def format_extra_dimension_lines(
    db: AsyncSession,
    subscription: Subscription,
    language: str | None = None,
) -> list[str]:
    """Готовые строки по всем измерениям, кроме обычного трафика.

    Пустой список — когда измерений не заведено: интерфейс тогда выглядит
    ровно как до появления фичи.
    """
    specs = await traffic_dimensions.non_base(db)
    if not specs:
        return []
    states = await get_dimension_states(db, subscription, specs=specs)
    return [format_dimension_usage(state, language) for state in states]


# ============================== Докупки измерений ==============================

# Докупка живёт своим сроком, как и у обычного трафика: пакет переживает
# продление подписки и истекает по собственным часам.
DEFAULT_PURCHASE_DAYS = 30


async def grant_dimension_traffic(
    db: AsyncSession,
    subscription: Subscription,
    spec: TrafficDimensionSpec,
    gb: int,
    *,
    days: int = DEFAULT_PURCHASE_DAYS,
    now: datetime | None = None,
) -> TrafficPurchase:
    """Начисляет докупку измерения: запись в журнал покупок плюс пересчёт квоты.

    Не коммитит: вызывающий владеет транзакцией, потому что рядом с начислением
    почти всегда идёт списание с баланса, и разъезжаться они не должны.
    """
    now = now or datetime.now(UTC)
    purchase = TrafficPurchase(
        subscription_id=subscription.id,
        dimension=spec.key,
        traffic_gb=int(gb),
        expires_at=now + timedelta(days=days),
    )
    db.add(purchase)
    await db.flush()
    await sync_dimension_purchased_gb(db, subscription, spec, now=now)
    return purchase


async def sync_dimension_purchased_gb(
    db: AsyncSession,
    subscription: Subscription,
    spec: TrafficDimensionSpec,
    *,
    now: datetime | None = None,
) -> int:
    """Пересобирает `purchased_gb` измерения по живым докупкам.

    Пересчёт от источника, а не вычитание истёкшего: вычитание тянуло бы за
    собой любую накопленную ошибку, а пересчёт сходится к правде с первого
    прогона. Истёкшие записи удаляются здесь же.
    """
    now = now or datetime.now(UTC)

    await db.execute(
        delete(TrafficPurchase)
        .where(
            TrafficPurchase.subscription_id == subscription.id,
            TrafficPurchase.dimension == spec.key,
            TrafficPurchase.expires_at <= now,
        )
        .execution_options(synchronize_session='fetch')
    )
    result = await db.execute(
        select(func.coalesce(func.sum(TrafficPurchase.traffic_gb), 0)).where(
            TrafficPurchase.subscription_id == subscription.id,
            TrafficPurchase.dimension == spec.key,
            TrafficPurchase.expires_at > now,
        )
    )
    purchased = int(result.scalar() or 0)

    row = await ensure_dimension_row(db, subscription, spec)
    row.purchased_gb = purchased
    return purchased


async def expire_dimension_purchases(
    db: AsyncSession, subscription: Subscription, *, now: datetime | None = None
) -> int:
    """Приводит квоты всех измерений подписки к живым докупкам.

    Отдельно от обычного трафика: у измерений своя строка состояния, и общий
    housekeeping базового лимита их не касается — иначе истёкший WL-пакет ронял
    бы обычный лимит подписки.
    """
    specs = await traffic_dimensions.non_base(db)
    changed = 0
    for spec in specs:
        before = (await ensure_dimension_row(db, subscription, spec)).purchased_gb or 0
        after = await sync_dimension_purchased_gb(db, subscription, spec, now=now)
        if after != before:
            changed += 1
    return changed
