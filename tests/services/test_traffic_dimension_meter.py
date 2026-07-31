"""Разбор пер-инбаунд статистики панели.

Панель отдаёт посуточную матрицу (`categories` + `series[].data`) в том же
ответе, из которого раньше брались только итоги `topInbounds`, и раз в неделю
делает TRUNCATE всей истории. Тесты фиксируют оба факта: матрица обязана
сходиться с итогами, а окно за пределами панельной недели — подрезаться.
"""

from datetime import date

import pytest

from app.services.traffic_dimension_meter import (
    panel_history_floor,
    parse_inbound_usage,
    resolve_displayed_used_gb,
)


WL_UUID = 'aaaaaaaa-1111-2222-3333-444444444444'
OTHER_UUID = 'bbbbbbbb-1111-2222-3333-444444444444'


def _panel_response() -> dict:
    """Ответ в формате GET /api/bandwidth-stats/users/{uuid}/inbounds."""
    return {
        'categories': ['2026-07-27', '2026-07-28', '2026-07-29'],
        'sparklineData': [30, 20, 10],
        'topInbounds': [
            {'uuid': WL_UUID, 'tag': 'wl', 'type': 'vless', 'port': 443, 'total': 45},
            {'uuid': OTHER_UUID, 'tag': 'std', 'type': 'vless', 'port': 443, 'total': 15},
        ],
        'series': [
            {'uuid': WL_UUID, 'tag': 'wl', 'type': 'vless', 'port': 443, 'total': 45, 'data': [20, 15, 10]},
            {'uuid': OTHER_UUID, 'tag': 'std', 'type': 'vless', 'port': 443, 'total': 15, 'data': [10, 5, 0]},
        ],
    }


def test_daily_matrix_sums_to_top_inbound_totals():
    """Опора всей модели: посуточная разбивка сходится с итогами панели."""
    payload = _panel_response()
    matrix = parse_inbound_usage(payload)

    for entry in payload['topInbounds']:
        assert matrix.total_for([entry['uuid']]) == entry['total']

    assert matrix.has_daily_series is True
    assert matrix.dates == (date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29))


def test_matrix_keeps_per_day_breakdown():
    matrix = parse_inbound_usage(_panel_response())
    assert matrix.daily_totals_for([WL_UUID]) == {
        date(2026, 7, 27): 20,
        date(2026, 7, 28): 15,
        date(2026, 7, 29): 10,
    }


def test_only_requested_inbounds_are_summed():
    matrix = parse_inbound_usage(_panel_response())
    assert matrix.total_for([WL_UUID]) == 45
    assert matrix.total_for([WL_UUID.upper()]) == 45, 'uuid сравниваются без учёта регистра'
    assert matrix.total_for([]) == 0
    assert matrix.total_for(['unknown-uuid']) == 0


def test_falls_back_to_top_inbounds_when_panel_has_no_series():
    """Старая панель без рядов: суммы верны, посуточная разбивка — нет."""
    payload = _panel_response()
    payload.pop('series')

    matrix = parse_inbound_usage(payload)

    assert matrix.total_for([WL_UUID]) == 45
    assert matrix.has_daily_series is False


@pytest.mark.parametrize('payload', [None, {}, {'categories': [], 'series': []}])
def test_empty_payload_is_zero_not_crash(payload):
    assert parse_inbound_usage(payload).total_for([WL_UUID]) == 0


def test_series_longer_than_categories_is_clipped():
    """Сетка дат — источник истины: лишние точки ряда игнорируются."""
    payload = _panel_response()
    payload['series'][0]['data'] = [20, 15, 10, 999]

    assert parse_inbound_usage(payload).total_for([WL_UUID]) == 45


@pytest.mark.parametrize(
    ('today', 'expected'),
    [
        (date(2026, 7, 27), date(2026, 7, 27)),  # понедельник — очистка была сегодня
        (date(2026, 7, 28), date(2026, 7, 27)),
        (date(2026, 8, 2), date(2026, 7, 27)),  # воскресенье — всё ещё та же неделя
    ],
)
def test_panel_history_floor_is_current_panel_week(today, expected):
    assert panel_history_floor(today, truncate_weekday=0) == expected


def test_panel_history_floor_can_be_disabled():
    assert panel_history_floor(date(2026, 7, 28), truncate_weekday=-1) is None


class TestResolveDisplayedUsedGb:
    """Что показываем, когда измерение хуже кэша."""

    def test_measurement_wins_on_a_full_window(self):
        stats = {'enabled': True, 'known': True, 'truncated_window': False, 'wl_used_gb': 3.0}
        assert resolve_displayed_used_gb(stats, 40.0) == 3.0

    def test_cache_wins_when_panel_is_unreachable(self):
        stats = {'enabled': True, 'known': False, 'truncated_window': False, 'wl_used_gb': 0.0}
        assert resolve_displayed_used_gb(stats, 40.0) == 40.0

    def test_truncated_window_never_lowers_the_counter(self):
        """Иначе счётчик обнулялся бы каждый понедельник после TRUNCATE в панели."""
        stats = {'enabled': True, 'known': True, 'truncated_window': True, 'wl_used_gb': 1.0}
        assert resolve_displayed_used_gb(stats, 40.0) == 40.0

    def test_truncated_window_still_reports_growth(self):
        stats = {'enabled': True, 'known': True, 'truncated_window': True, 'wl_used_gb': 55.0}
        assert resolve_displayed_used_gb(stats, 40.0) == 55.0

    def test_disabled_feature_falls_back_to_cache(self):
        assert resolve_displayed_used_gb({'enabled': False}, 7.5) == 7.5
