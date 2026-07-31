"""Чтение пер-инбаунд трафика пользователя из панели RemnaWave.

Панель раскладывает пер-инбаунд трафик пользователя по **UTC-суткам**
(`(NOW() AT TIME ZONE 'UTC')::date` в её upsert'е) и накапливает дельты в
бакет текущего дня, поэтому число за день только растёт, пока день открыт, и
замораживается, когда день закрылся.

Два поведения панели определяют всё, что здесь написано:

* `GET /api/bandwidth-stats/users/{uuid}/inbounds` уже возвращает посуточную
  матрицу — плотный список `categories[]` (`YYYY-MM-DD`) и выровненные по нему
  `series[].data[]`, — так что посуточная детализация не стоит ни одного
  лишнего запроса. `topInbounds` обрезается параметром `topInboundsLimit`,
  а `series` — нет, поэтому источником считаем `series`.
* Раз в неделю (`CLEAN_OLD_USAGE_RECORDS`, понедельник 00:30 UTC) панель делает
  TRUNCATE всей таблицы пер-инбаунд истории. Всё, что старше текущей панельной
  недели, просто отсутствует. Окно, уходящее глубже, — это не ошибка и не
  «нулевой трафик», а **неизвестность**, и вызывающий код обязан их различать.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import structlog


logger = structlog.get_logger(__name__)

BYTES_IN_GB = 1024**3


def _parse_iso_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class InboundUsageMatrix:
    """Байты по (UTC-сутки, uuid инбаунда) за запрошенное окно.

    `has_daily_series` = False означает, что панель не отдала посуточные ряды и
    значения свёрнуты в один синтетический день: суммы по инбаундам верны,
    посуточная разбивка — нет.
    """

    cells: Mapping[tuple[date, str], int] = field(default_factory=dict)
    dates: tuple[date, ...] = ()
    has_daily_series: bool = True

    def total_for(self, inbound_uuids: Iterable[str]) -> int:
        wanted = {str(uuid).lower() for uuid in inbound_uuids if uuid}
        if not wanted:
            return 0
        return sum(value for (_, inbound_uuid), value in self.cells.items() if inbound_uuid in wanted)

    def daily_totals_for(self, inbound_uuids: Iterable[str]) -> dict[date, int]:
        wanted = {str(uuid).lower() for uuid in inbound_uuids if uuid}
        totals: dict[date, int] = {}
        if not wanted:
            return totals
        for (usage_date, inbound_uuid), value in self.cells.items():
            if inbound_uuid in wanted:
                totals[usage_date] = totals.get(usage_date, 0) + value
        return totals

    @property
    def covered_from(self) -> date | None:
        return self.dates[0] if self.dates else None

    @property
    def covered_to(self) -> date | None:
        return self.dates[-1] if self.dates else None


@dataclass(frozen=True)
class InboundUsageReading:
    """Результат одного обращения к панели за пер-инбаунд трафиком.

    `known` — единственное, на что можно опираться при принятии решений:
    недоступная панель, отсутствующий эндпоинт или ошибка дают `known=False`,
    а не «ноль трафика».
    """

    matrix: InboundUsageMatrix
    known: bool
    requested_from: date
    requested_to: date
    covered_from: date
    truncated_window: bool
    error: str | None = None

    def total_for(self, inbound_uuids: Iterable[str]) -> int:
        return self.matrix.total_for(inbound_uuids)


def parse_inbound_usage(payload: Mapping | None) -> InboundUsageMatrix:
    """Разбирает ответ `/bandwidth-stats/users/{uuid}/inbounds` в матрицу.

    Предпочитает `series[].data[]`, выровненные по `categories[]`. Если панель
    старая и рядов нет, откатывается на `topInbounds[].total`, сворачивая всё в
    последний день окна и помечая матрицу как непосуточную.
    """
    if not payload:
        return InboundUsageMatrix()

    categories = [d for d in (_parse_iso_date(c) for c in payload.get('categories') or []) if d is not None]
    series = payload.get('series') or []

    cells: dict[tuple[date, str], int] = {}
    if categories and series:
        for entry in series:
            inbound_uuid = str((entry or {}).get('uuid') or '').lower()
            if not inbound_uuid:
                continue
            data = (entry or {}).get('data') or []
            for index, raw in enumerate(data):
                if index >= len(categories):
                    # Панель прислала ряд длиннее сетки дат — доверяем сетке.
                    break
                value = int(raw or 0)
                if value:
                    key = (categories[index], inbound_uuid)
                    cells[key] = cells.get(key, 0) + value
        return InboundUsageMatrix(cells=cells, dates=tuple(categories), has_daily_series=True)

    top_inbounds = payload.get('topInbounds') or []
    if not top_inbounds:
        return InboundUsageMatrix(dates=tuple(categories))

    fallback_day = categories[-1] if categories else datetime.now(UTC).date()
    for entry in top_inbounds:
        inbound_uuid = str((entry or {}).get('uuid') or '').lower()
        if not inbound_uuid:
            continue
        value = int((entry or {}).get('total') or 0)
        if value:
            key = (fallback_day, inbound_uuid)
            cells[key] = cells.get(key, 0) + value
    return InboundUsageMatrix(
        cells=cells,
        dates=tuple(categories) or (fallback_day,),
        has_daily_series=False,
    )


def resolve_displayed_used_gb(stats: Mapping, cached_gb: float | None) -> float:
    """Что показать пользователю: свежее измерение панели или кэш подписки.

    Кэш выигрывает, когда измерить не удалось, и когда окно урезано недельной
    очисткой панели — там измерение заведомо занижено и показывать его как факт
    нельзя (счётчик «падал» бы каждый понедельник).
    """
    cached = float(cached_gb or 0.0)
    if not stats.get('enabled') or not stats.get('known'):
        return cached
    measured = float(stats.get('wl_used_gb') or 0.0)
    if stats.get('truncated_window'):
        return max(measured, cached)
    return measured


def panel_history_floor(today: date, truncate_weekday: int) -> date | None:
    """Самая ранняя дата, за которую панель ещё хранит пер-инбаунд историю.

    Панель делает TRUNCATE всей таблицы в `truncate_weekday` (0 = понедельник),
    поэтому переживает только текущая панельная неделя. `truncate_weekday < 0`
    отключает clamp — для форков, где еженедельная очистка убрана.
    """
    if truncate_weekday is None or truncate_weekday < 0 or truncate_weekday > 6:
        return None
    return today - timedelta(days=(today.weekday() - truncate_weekday) % 7)
