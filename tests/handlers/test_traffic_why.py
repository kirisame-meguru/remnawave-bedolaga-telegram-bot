"""Посуточная разбивка в `/traffic_why`: чей журнал читаем и за какой период.

Команда существует ради разбора жалобы «я столько не расходовал», поэтому
цена ошибки здесь — показанные админу чужие или неполные цифры. Ключ журнала
после 3.0.0 — числовой id панельного пользователя: подстановка сюда чего-то
другого не падает, а молча отдаёт пустую разбивку.
"""

from datetime import date

import pytest

from app.database.models import TrafficDimensionSample
from app.handlers.admin.traffic_why import _daily_breakdown
from tests.fixtures.sqlite_memory import memory_session
from tests.services.test_traffic_dimension_ledger import make_spec


TABLES = [TrafficDimensionSample.__table__]

SPEC = make_spec(inbounds=('in-a', 'in-b'))
WINDOW_START = date(2026, 3, 1)
PANEL_ID = 101
OTHER_PANEL_ID = 202
GB = 1024**3


async def seed(db) -> None:
    rows = [
        # Панель 101, внутри окна: два инбаунда за одни сутки — одна строка.
        (PANEL_ID, 'in-a', date(2026, 3, 5), 2 * GB),
        (PANEL_ID, 'in-b', date(2026, 3, 5), 1 * GB),
        (PANEL_ID, 'in-a', date(2026, 3, 6), 3 * GB),
        (PANEL_ID, 'in-a', date(2026, 2, 28), 9 * GB),  # до начала окна
        (PANEL_ID, 'in-z', date(2026, 3, 6), 9 * GB),  # инбаунд не из измерения
        (OTHER_PANEL_ID, 'in-a', date(2026, 3, 6), 9 * GB),  # чужой панельный юзер
    ]
    for panel_id, inbound, usage_date, value in rows:
        db.add(
            TrafficDimensionSample(
                remnawave_id=panel_id,
                inbound_uuid=inbound,
                usage_date=usage_date,
                bytes=value,
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_breakdown_sums_the_day_and_shows_the_newest_first(monkeypatch):
    """Сутки — одна строка: расход по инбаундам измерения складывается."""
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)

        assert await _daily_breakdown(db, PANEL_ID, SPEC, WINDOW_START) == [
            '    2026-03-06: 3.00 ГБ',
            '    2026-03-05: 3.00 ГБ',
        ]


@pytest.mark.asyncio
async def test_breakdown_reads_only_the_requested_panel_user(monkeypatch):
    """Ключ журнала — числовой id: соседняя запись не должна попасть в разбор."""
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)

        assert await _daily_breakdown(db, OTHER_PANEL_ID, SPEC, WINDOW_START) == ['    2026-03-06: 9.00 ГБ']


@pytest.mark.asyncio
async def test_breakdown_without_a_panel_id_is_empty_not_an_error(monkeypatch):
    """`remnawave_id IS NULL` — подписку ни разу не отдавали в панель.

    Разбор для админа обязан открыться и в этом случае: пустая разбивка — это
    ответ, а исключение прямо в обработчике команды — нет.
    """
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)

        assert await _daily_breakdown(db, None, SPEC, WINDOW_START) == []
        assert await _daily_breakdown(db, 999, SPEC, WINDOW_START) == []


@pytest.mark.asyncio
async def test_dimension_without_inbounds_reads_nothing(monkeypatch):
    """Измерение без инбаундов ничего не измеряет — запрос в БД не нужен."""
    async with memory_session(monkeypatch, TABLES) as db:
        await seed(db)

        assert await _daily_breakdown(db, PANEL_ID, make_spec(inbounds=()), WINDOW_START) == []
