"""Ограничение доступа, когда квота измерения исчерпана.

В панели RemnaWave у пользователя ровно один ``trafficLimitBytes`` и ровно одна
``trafficLimitStrategy``. Пер-инбаунд лимита не существует. Поэтому второе
измерение можно ограничить только одним способом: убрать из
``activeInternalSquads`` те сквады, которые дают доступ к его инбаундам.

Отсюда два правила, которые здесь и живут.

**Смешанный сквад не трогаем.** Сквад — это набор инбаундов. Если в скваде есть
и инбаунд измерения, и обычный, снятие сквада отберёт у пользователя оплаченный
обычный доступ. Такое ограничение — не «строгость», а порча оплаченной услуги,
поэтому оно не применяется вовсе: подписка помечается ``mixed_squad``, а
администратор получает сигнал, что топология не даёт ограничить измерение.

**Право на сквады не переписывается.** ``connected_squads`` — это то, что
подписке положено, и блокировка его не меняет. Снятые сквады запоминаются
отдельно (``stripped_squads``), а снятие происходит фильтром на границе API:
исходящих обновлений панели два десятка, и каждое из них обязано соблюдать
блокировку, даже если про измерения ничего не знает.

Отдельно: пока по подписке открыт grace-оверлей, ограничение не применяется.
Grace сам владеет ``active_internal_squads`` и сверяет ответ панели с тем, что
просил; чужой фильтр в этот момент выглядел бы для него как отказ панели.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from math import ceil

import structlog


logger = structlog.get_logger(__name__)


class SquadTopology(str, Enum):
    """Как сквад соотносится с инбаундами измерения."""

    FREE = 'free'  # ни одного инбаунда измерения — снимать незачем
    PURE = 'pure'  # только инбаунды измерения — снимать безопасно
    MIXED = 'mixed'  # и те, и другие — снятие отберёт оплаченный обычный доступ


class BlockReason(str, Enum):
    """Почему измерение сейчас ограничено."""

    QUOTA_EXHAUSTED = 'quota_exhausted'
    # Топология сквадов не позволяет ограничить измерение, не задев обычный доступ.
    MIXED_SQUAD = 'mixed_squad'
    # Расход неизвестен (панель молчит или журнал не покрывает окно). Блокировка,
    # которая уже стоит, сохраняется: снимать её на незнании — значит выдать
    # квоту заново всем сразу.
    UNKNOWN_USAGE_HOLD = 'unknown_usage_hold'


class EnforcementMode(str, Enum):
    OBSERVE = 'observe'  # только считаем и логируем
    NOTIFY = 'notify'  # фиксируем исчерпание и уведомляем, доступ не трогаем
    ENFORCE = 'enforce'  # то же плюс снятие сквадов в панели


def resolve_mode(raw: str | None) -> EnforcementMode:
    value = (raw or '').strip().lower()
    for mode in EnforcementMode:
        if mode.value == value:
            return mode
    # Незнакомое значение не должно молча включать снятие доступа.
    return EnforcementMode.OBSERVE


# ============================== Топология сквадов ==============================


def classify_squad(squad_inbounds: frozenset[str], dimension_inbounds: frozenset[str]) -> SquadTopology:
    """К какому типу относится сквад с точки зрения измерения.

    Сквад без известных инбаундов считается FREE: про него ничего не известно,
    и снимать его на догадке нельзя.
    """
    if not squad_inbounds or not dimension_inbounds:
        return SquadTopology.FREE
    overlap = squad_inbounds & dimension_inbounds
    if not overlap:
        return SquadTopology.FREE
    return SquadTopology.PURE if overlap == squad_inbounds else SquadTopology.MIXED


@dataclass(frozen=True)
class StripPlan:
    """Что нужно снять, чтобы закрыть доступ к инбаундам измерения."""

    strip: frozenset[str] = frozenset()
    mixed: frozenset[str] = frozenset()
    unknown: frozenset[str] = frozenset()

    @property
    def refused(self) -> bool:
        """Ограничение невозможно без порчи оплаченного обычного доступа."""
        return bool(self.mixed)

    @property
    def is_noop(self) -> bool:
        return not self.strip


def plan_squad_strip(
    connected_squads: Iterable[str] | None,
    squad_index: Mapping[str, frozenset[str]],
    dimension_inbounds: frozenset[str],
) -> StripPlan:
    """Разбирает сквады подписки на «снять», «нельзя снять» и «не знаем».

    Достаточно одного смешанного сквада, чтобы ограничение стало невозможным:
    снять его нельзя, а оставить — значит оставить и доступ к измерению, ради
    закрытия которого всё и затевалось.
    """
    strip: set[str] = set()
    mixed: set[str] = set()
    unknown: set[str] = set()

    for raw in connected_squads or []:
        squad_uuid = str(raw).lower()
        inbounds = squad_index.get(squad_uuid)
        if inbounds is None:
            # Сквада нет в панельной карте: топология неизвестна.
            unknown.add(squad_uuid)
            continue
        topology = classify_squad(inbounds, dimension_inbounds)
        if topology is SquadTopology.PURE:
            strip.add(squad_uuid)
        elif topology is SquadTopology.MIXED:
            mixed.add(squad_uuid)

    return StripPlan(strip=frozenset(strip), mixed=frozenset(mixed), unknown=frozenset(unknown))


def panel_squads_for(connected_squads: Iterable[str] | None, stripped: Iterable[str] | None) -> list[str]:
    """Что отправлять в панель: право подписки минус снятое.

    Порядок исходного списка сохраняется — панель сравнивает множества, а логи
    и диффы читают люди.
    """
    removed = {str(uuid).lower() for uuid in (stripped or [])}
    return [str(uuid) for uuid in (connected_squads or []) if str(uuid).lower() not in removed]


def merge_panel_squads(
    panel_squads: Iterable[str] | None,
    entitled_squads: Iterable[str] | None,
    stripped: Iterable[str] | None,
) -> list[str]:
    """Приводит `connected_squads` к панели, не теряя снятого блокировкой.

    Синхронизация считает панель источником правды по составу сквадов. Но
    сквады, снятые из-за исчерпанной квоты измерения, отсутствуют в панели
    намеренно — это и есть блокировка. Простое присваивание стёрло бы их из
    права подписки навсегда: разблокировать было бы уже нечего, а пользователь
    молча потерял бы оплаченное направление.
    """
    result = [str(uuid) for uuid in (panel_squads or [])]
    removed = {str(uuid).lower() for uuid in (stripped or [])}
    if removed:
        result.extend(str(uuid) for uuid in (entitled_squads or []) if str(uuid).lower() in removed)
    return list(dict.fromkeys(result))


# ============================== Режим учёта ==============================

BYTES_IN_GB = 1024**3


def effective_panel_traffic_limit_bytes(base_limit_gb: int, states: Iterable) -> int:
    """Лимит, который надо держать в панели с учётом «щитующих» измерений.

    Панель складывает трафик всех инбаундов в один ``usedTrafficBytes`` —
    другого счётчика у неё нет. Поэтому в режиме ``subquota`` (по умолчанию)
    трафик измерения расходует и основную квоту: пользователь платит дважды,
    зато поведение ровно панельное.

    Режим ``shielded`` компенсирует это единственным доступным способом: лимит
    в панели поднимается на израсходованное «щитующими» измерениями, и основная
    квота остаётся нетронутой. Значение приходится переставлять по мере расхода
    — постоянного правильного числа тут не существует, — поэтому округляем до
    целых ГБ: так лимит меняется не чаще раза на гигабайт, а не на каждый байт.

    Расход, которому нельзя верить (``used_known=False``), в щит не идёт: он
    завысил бы лимит на выдуманную величину.
    """
    base = int(base_limit_gb or 0)
    if base <= 0:
        # Безлимит остаётся безлимитом — поднимать нечего.
        return 0
    shield_gb = 0.0
    for state in states:
        spec = getattr(state, 'spec', None)
        if spec is None or not spec.shields_base_quota or not state.used_known:
            continue
        shield_gb += max(float(state.used_gb or 0.0), 0.0)
    return int((base + ceil(shield_gb)) * BYTES_IN_GB)


# ============================== Реестр блокировок ==============================


class DimensionSquadPolicy:
    """Какие сквады сняты у каких панельных пользователей прямо сейчас.

    Живёт в памяти процесса и читается на каждом исходящем обновлении панели,
    поэтому здесь не должно быть ни запросов к БД, ни блокировок: реконсилятор
    перекладывает сюда готовую карту, а граница API только смотрит в словарь.

    Подписки с открытым grace-оверлеем в карту не попадают: grace сам владеет
    ``active_internal_squads`` и сверяет ответ панели с запрошенным.
    """

    def __init__(self) -> None:
        self._stripped: dict[str, frozenset[str]] = {}

    def replace_all(self, mapping: Mapping[str, Iterable[str]]) -> None:
        """Заменяет карту целиком — так реконсилятор публикует итог цикла."""
        self._stripped = {
            str(uuid): frozenset(str(squad).lower() for squad in squads)
            for uuid, squads in mapping.items()
            if uuid and squads
        }

    def set_for(self, remnawave_uuid: str, squads: Iterable[str]) -> None:
        squad_set = frozenset(str(squad).lower() for squad in squads or [])
        if squad_set:
            self._stripped[str(remnawave_uuid)] = squad_set
        else:
            self._stripped.pop(str(remnawave_uuid), None)

    def clear_for(self, remnawave_uuid: str) -> None:
        self._stripped.pop(str(remnawave_uuid), None)

    def stripped_for(self, remnawave_uuid: str) -> frozenset[str]:
        return self._stripped.get(str(remnawave_uuid), frozenset())

    def blocked_uuids(self) -> frozenset[str]:
        return frozenset(self._stripped)

    def filter_squads(self, remnawave_uuid: str, squads: Sequence[str] | None) -> list[str] | None:
        """Убирает снятые сквады из исходящего обновления панели.

        Возвращает исходный список без изменений, если по пользователю ничего
        не снято, — вызывающему не нужно знать про измерения вообще.
        """
        if squads is None:
            return None
        stripped = self.stripped_for(remnawave_uuid)
        if not stripped:
            return list(squads)
        filtered = panel_squads_for(squads, stripped)
        if len(filtered) != len(squads):
            logger.debug(
                'Сняты сквады измерения из обновления панели',
                remnawave_uuid=remnawave_uuid,
                stripped=sorted(stripped),
            )
        return filtered


dimension_squad_policy = DimensionSquadPolicy()


# ============================== Решение по подписке ==============================


class EnforcementAction(str, Enum):
    NONE = 'none'
    BLOCK = 'block'
    UNBLOCK = 'unblock'
    HOLD = 'hold'  # блокировка остаётся, потому что расход неизвестен
    REFUSE = 'refuse'  # топология не позволяет ограничить


@dataclass(frozen=True)
class EnforcementDecision:
    """Что делать с одним измерением одной подписки."""

    action: EnforcementAction
    reason: BlockReason | None = None
    plan: StripPlan | None = None

    @property
    def changes_state(self) -> bool:
        return self.action in (EnforcementAction.BLOCK, EnforcementAction.UNBLOCK)


def decide(state, plan: StripPlan | None) -> EnforcementDecision:
    """Чистое решение по текущему состоянию измерения.

    Порядок проверок здесь и есть политика:

    1. Безлимит — ограничивать нечего.
    2. Цифра, по которой нельзя принимать решение (панель молчала или журнал не
       покрывает начало окна), не снимает уже стоящую блокировку и не ставит
       новую.
    3. Квота исчерпана — блокируем, если топология позволяет.
    4. Квота снова есть (новое окно или докупка) — снимаем блокировку.
    """
    if state.is_unlimited:
        return EnforcementDecision(EnforcementAction.UNBLOCK if state.blocked else EnforcementAction.NONE)

    if not state.is_enforceable:
        if state.blocked:
            return EnforcementDecision(EnforcementAction.HOLD, BlockReason.UNKNOWN_USAGE_HOLD)
        return EnforcementDecision(EnforcementAction.NONE)

    if state.used_gb >= state.limit_gb:
        if state.blocked:
            return EnforcementDecision(EnforcementAction.NONE, BlockReason.QUOTA_EXHAUSTED, plan)
        if plan is not None and plan.refused:
            return EnforcementDecision(EnforcementAction.REFUSE, BlockReason.MIXED_SQUAD, plan)
        return EnforcementDecision(EnforcementAction.BLOCK, BlockReason.QUOTA_EXHAUSTED, plan)

    if state.blocked:
        return EnforcementDecision(EnforcementAction.UNBLOCK, plan=plan)
    return EnforcementDecision(EnforcementAction.NONE)


# ============================== Предохранитель ==============================


@dataclass
class BlastGuard:
    """Не даёт одному сбою измерения выключить доступ половине базы.

    Массовая блокировка почти всегда означает не массовое исчерпание квоты, а
    поломку: сменившиеся uuid инбаундов, потерянную карту сквадов, кривую
    миграцию. Дешевле не сделать ничего и позвать человека.
    """

    max_blocks: int = 0
    max_percent: int = 0
    scanned: int = 0
    planned: int = 0
    tripped_by: str | None = field(default=None)

    def would_trip(self) -> bool:
        if self.max_blocks > 0 and self.planned > self.max_blocks:
            self.tripped_by = f'больше {self.max_blocks} блокировок за цикл'
            return True
        if self.max_percent > 0 and self.scanned > 0:
            share = self.planned * 100 / self.scanned
            if share > self.max_percent:
                self.tripped_by = f'{share:.0f}% подписок против порога {self.max_percent}%'
                return True
        return False
