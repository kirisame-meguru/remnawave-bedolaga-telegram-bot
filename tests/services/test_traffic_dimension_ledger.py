"""Журнал наблюдений: границы окна, топология сквадов, отбор ячеек.

Проверяется то, на чём потом строятся блокировки: окно должно совпадать с
окном сброса обычного трафика, «не знаем» не должно превращаться в «ноль», а
матрица без посуточной детализации не должна попадать в журнал.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest

from app.services.traffic_dimension_ledger import (
    DimensionMeasurement,
    WindowUsage,
    applicable_specs,
    reachable_inbounds,
    sample_rows_from_matrix,
    squad_inbound_index,
    window_start_for,
)
from app.services.traffic_dimension_meter import InboundUsageMatrix
from app.services.traffic_dimensions import TrafficDimensionSpec


def make_spec(key='wl', inbounds=('aaa',), **kwargs) -> TrafficDimensionSpec:
    defaults = {
        'id': 2,
        'key': key,
        'titles': {'ru': 'WL'},
        'fallback_title': 'WL',
        'icon': '⚪',
        'inbound_uuids': frozenset(inbounds),
        'default_limit_gb': 10,
        'accounting_mode': 'subquota',
        'enforcement': 'squad_strip',
        'discount_category': 'traffic',
        'is_enabled': True,
        'is_builtin': False,
        'position': 1,
    }
    defaults.update(kwargs)
    return TrafficDimensionSpec(**defaults)


# ------------------------------ границы окна ------------------------------

START = date(2026, 1, 15)


@pytest.mark.parametrize(
    ('strategy', 'today', 'expected'),
    [
        ('DAY', date(2026, 3, 10), date(2026, 3, 10)),
        # 2026-03-11 — среда, начало недели 2026-03-09.
        ('WEEK', date(2026, 3, 11), date(2026, 3, 9)),
        ('MONTH', date(2026, 3, 11), date(2026, 3, 1)),
        ('NO_RESET', date(2026, 3, 11), START),
        (None, date(2026, 3, 11), START),
        ('ЧТО-ТО НЕПОНЯТНОЕ', date(2026, 3, 11), START),
        # Скользящий месяц: последняя годовщина 15-го числа.
        ('MONTH_ROLLING', date(2026, 3, 11), date(2026, 2, 15)),
        ('MONTH_ROLLING', date(2026, 3, 15), date(2026, 3, 15)),
    ],
)
def test_window_start_matches_reset_strategy(strategy, today, expected):
    assert window_start_for(strategy, today=today, subscription_start=START) == expected


def test_window_never_predates_subscription():
    """Трафик до появления подписки — не её трафик."""
    # 2026-03-24 — вторник, неделя началась 2026-03-23, месяц — 2026-03-01.
    late_start = date(2026, 3, 24)
    assert window_start_for('MONTH', today=date(2026, 3, 25), subscription_start=late_start) == late_start
    assert window_start_for('WEEK', today=date(2026, 3, 25), subscription_start=late_start) == late_start


def test_month_rolling_short_month_uses_last_day():
    """31-е число в феврале — последний день месяца, а не исключение."""
    anchor = date(2026, 1, 31)
    # 2026-02-28 ещё не наступило -> откатываемся на январскую годовщину.
    assert window_start_for('MONTH_ROLLING', today=date(2026, 2, 20), subscription_start=anchor) == date(2026, 1, 31)
    # После 28 февраля годовщина этого месяца — последний его день.
    assert window_start_for('MONTH_ROLLING', today=date(2026, 3, 1), subscription_start=anchor) == date(2026, 2, 28)


def test_panel_last_reset_wins_for_month_rolling():
    """Если панель сказала, когда сбросила, спорить не с чем."""
    assert window_start_for(
        'MONTH_ROLLING',
        today=date(2026, 3, 11),
        subscription_start=START,
        last_reset_at=date(2026, 3, 4),
    ) == date(2026, 3, 4)


# ------------------------------ топология сквадов ------------------------------


@dataclass
class FakeInbound:
    uuid: str


@dataclass
class FakeSquad:
    uuid: str
    inbounds: list


def test_squad_index_lowercases_everything():
    index = squad_inbound_index([FakeSquad(uuid='SQ-1', inbounds=[FakeInbound('AAA'), FakeInbound('bbb')])])
    assert index == {'sq-1': frozenset({'aaa', 'bbb'})}


def test_squad_index_skips_squads_without_uuid():
    assert squad_inbound_index([FakeSquad(uuid='', inbounds=[FakeInbound('aaa')])]) == {}


def test_reachable_inbounds_unions_connected_squads():
    index = {'sq-1': frozenset({'aaa'}), 'sq-2': frozenset({'bbb'})}
    assert reachable_inbounds(['SQ-1', 'sq-2'], index) == frozenset({'aaa', 'bbb'})


def test_reachable_inbounds_ignores_unknown_squad():
    assert reachable_inbounds(['ghost'], {'sq-1': frozenset({'aaa'})}) == frozenset()


def test_reachable_inbounds_handles_empty_input():
    assert reachable_inbounds(None, {}) == frozenset()


def test_applicable_specs_needs_overlap():
    """Нет инбаунда измерения в сквадах — трафик по нему физически невозможен."""
    wl = make_spec(inbounds=('aaa',))
    torrent = make_spec(key='torrent', inbounds=('zzz',))
    assert applicable_specs([wl, torrent], frozenset({'aaa'})) == (wl,)
    assert applicable_specs([wl, torrent], frozenset()) == ()


# ------------------------------ отбор ячеек ------------------------------


FETCHED_AT = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)


def test_sample_rows_keeps_only_wanted_inbounds():
    matrix = InboundUsageMatrix(
        cells={
            (date(2026, 3, 10), 'aaa'): 100,
            (date(2026, 3, 10), 'other'): 500,
            (date(2026, 3, 11), 'aaa'): 200,
        },
        dates=(date(2026, 3, 10), date(2026, 3, 11)),
    )
    rows = sample_rows_from_matrix('uuid-1', matrix, frozenset({'aaa'}), fetched_at=FETCHED_AT)
    assert {(row['usage_date'], row['bytes']) for row in rows} == {
        (date(2026, 3, 10), 100),
        (date(2026, 3, 11), 200),
    }


def test_sample_rows_drop_zero_cells():
    matrix = InboundUsageMatrix(cells={(date(2026, 3, 10), 'aaa'): 0}, dates=(date(2026, 3, 10),))
    assert sample_rows_from_matrix('uuid-1', matrix, frozenset({'aaa'}), fetched_at=FETCHED_AT) == []


def test_sample_rows_refuse_matrix_without_daily_series():
    """Свёрнутая в один день матрица сломала бы GREATEST-апсерт навсегда."""
    matrix = InboundUsageMatrix(
        cells={(date(2026, 3, 11), 'aaa'): 10**12},
        dates=(date(2026, 3, 11),),
        has_daily_series=False,
    )
    assert sample_rows_from_matrix('uuid-1', matrix, frozenset({'aaa'}), fetched_at=FETCHED_AT) == []


def test_sample_rows_need_uuid():
    matrix = InboundUsageMatrix(cells={(date(2026, 3, 11), 'aaa'): 5}, dates=(date(2026, 3, 11),))
    assert sample_rows_from_matrix('', matrix, frozenset({'aaa'}), fetched_at=FETCHED_AT) == []


# ------------------------------ агрегаты ------------------------------


def test_window_usage_sums_requested_inbounds_only():
    usage = WindowUsage(by_inbound={'aaa': 10, 'bbb': 5, 'ccc': 100}, covered_from=date(2026, 3, 1))
    assert usage.bytes_for(['AAA', 'bbb']) == 15
    assert usage.bytes_for(['ghost']) == 0


def test_measurement_reports_coverage_gap():
    spec = make_spec()
    covered = DimensionMeasurement(
        spec=spec,
        used_gb=1.0,
        known=True,
        window_start=date(2026, 3, 1),
        coverage_from=date(2026, 3, 1),
    )
    assert not covered.has_coverage_gap

    gapped = DimensionMeasurement(
        spec=spec,
        used_gb=1.0,
        known=True,
        window_start=date(2026, 3, 1),
        coverage_from=date(2026, 3, 6),
    )
    assert gapped.has_coverage_gap

    empty = DimensionMeasurement(
        spec=spec,
        used_gb=0.0,
        known=True,
        window_start=date(2026, 3, 1),
        coverage_from=None,
    )
    assert empty.has_coverage_gap


def test_unknown_measurement_is_not_a_coverage_gap():
    """`known=False` — отдельная категория: о покрытии речи вообще нет."""
    measurement = DimensionMeasurement(
        spec=make_spec(),
        used_gb=0.0,
        known=False,
        window_start=date(2026, 3, 1),
        coverage_from=None,
    )
    assert not measurement.has_coverage_gap
