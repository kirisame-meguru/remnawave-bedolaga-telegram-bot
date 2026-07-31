"""Реестр измерений трафика.

Главное, что здесь проверяется: обычный трафик (`base`) читается из старых
колонок подписки, а не из новых таблиц, — именно это позволяет не трогать
сотни мест, где `traffic_limit_gb` уже используется.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from app.database.models import TrafficAccountingMode, TrafficDimensionEnforcement
from app.services.traffic_dimensions import (
    BASE_KEY,
    DEFAULT_DISCOUNT_CATEGORY,
    TrafficDimensionSpec,
    _base_state,
    _resolve_accounting_mode,
    _row_state,
    _spec_from_row,
    format_dimension_usage,
    format_dimension_value,
)


def _row(**overrides):
    """Строка traffic_dimensions в том виде, в каком её читает реестр."""
    defaults = {
        'id': 2,
        'key': 'wl',
        'title': {'ru': 'WL Трафик (БС)', 'en': 'WL Traffic'},
        'fallback_title': 'WL',
        'icon': '⚪',
        'inbound_uuids': ['AAAA-1111', 'bbbb-2222'],
        'default_limit_gb': 10,
        'accounting_mode': None,
        'enforcement': TrafficDimensionEnforcement.SQUAD_STRIP.value,
        'discount_category': None,
        'is_enabled': True,
        'is_builtin': False,
        'position': 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _state_row(**overrides) -> SimpleNamespace:
    """Заглушка строки `subscription_traffic_dimensions`."""
    defaults = {
        'base_limit_gb': 10,
        'purchased_gb': 0,
        'used_gb': 0.0,
        'measured_known': True,
        'blocked_at': None,
        'block_reason': None,
        'stripped_squads': [],
        'window_start': None,
        'coverage_from': None,
        'limit_gb': 10,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _spec(**overrides) -> TrafficDimensionSpec:
    return _spec_from_row(_row(**overrides))


class TestSpec:
    def test_inbound_uuids_are_normalised_to_lowercase(self):
        """UUID приходят из панели и от админа в разном регистре."""
        assert _spec().inbound_uuids == {'aaaa-1111', 'bbbb-2222'}

    def test_blank_inbound_entries_are_dropped(self):
        assert _spec(inbound_uuids=['  ', '', 'aaaa-1111']).inbound_uuids == {'aaaa-1111'}

    def test_discount_category_falls_back_to_shared_traffic_category(self):
        assert _spec().discount_category == DEFAULT_DISCOUNT_CATEGORY
        assert _spec(discount_category='wl').discount_category == 'wl'

    def test_title_prefers_requested_language_then_default_then_fallback(self):
        spec = _spec()
        assert spec.title('en') == 'WL Traffic'
        assert spec.title('fa') == 'WL Трафик (БС)', 'нет перевода — берём язык по умолчанию'
        assert _spec(title={}, fallback_title='Запасной').title('en') == 'Запасной'
        assert _spec(title={}, fallback_title='').title('en') == 'wl', 'последний рубеж — ключ'

    def test_label_prefixes_icon_when_present(self):
        assert _spec().label('en') == '⚪ WL Traffic'
        assert _spec(icon='').label('en') == 'WL Traffic'

    def test_base_dimension_never_shields_or_strips(self):
        base = _spec(key=BASE_KEY, enforcement=TrafficDimensionEnforcement.PANEL_LIMIT.value)
        assert base.is_base is True
        assert base.shields_base_quota is False
        assert base.strips_squads is False

    def test_shielding_is_opt_in_per_dimension(self):
        assert _spec(accounting_mode=TrafficAccountingMode.SHIELDED.value).shields_base_quota is True
        assert _spec(accounting_mode=TrafficAccountingMode.SUBQUOTA.value).shields_base_quota is False


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('shielded', TrafficAccountingMode.SHIELDED.value),
        ('SUBQUOTA', TrafficAccountingMode.SUBQUOTA.value),
        ('  shielded  ', TrafficAccountingMode.SHIELDED.value),
        ('nonsense', TrafficAccountingMode.SUBQUOTA.value),
    ],
)
def test_accounting_mode_resolution_defaults_to_subquota(raw, expected):
    """Незнакомое значение не должно молча включать компенсацию лимита в панели."""
    assert _resolve_accounting_mode(raw) == expected


class TestBaseState:
    """Обычный трафик живёт в колонках подписки, а не в новых таблицах."""

    def test_reads_the_legacy_subscription_columns(self):
        subscription = SimpleNamespace(traffic_limit_gb=150, purchased_traffic_gb=50, traffic_used_gb=12.5)
        state = _base_state(_spec(key=BASE_KEY), subscription)

        assert state.limit_gb == 150
        assert state.purchased_gb == 50
        assert state.base_limit_gb == 100, 'база восстанавливается как total - purchased'
        assert state.used_gb == 12.5
        assert state.used_known is True

    def test_unlimited_base_stays_unlimited(self):
        subscription = SimpleNamespace(traffic_limit_gb=0, purchased_traffic_gb=0, traffic_used_gb=3.0)
        state = _base_state(_spec(key=BASE_KEY), subscription)

        assert state.is_unlimited is True
        assert state.base_limit_gb == 0
        assert state.used_percent == 0.0
        assert state.is_exhausted is False


class TestRowState:
    def test_missing_row_falls_back_to_the_dimension_default(self):
        state = _row_state(_spec(default_limit_gb=25), None)

        assert state.limit_gb == 25
        assert state.used_gb == 0.0
        assert state.used_known is False, 'без измерения нулю верить нельзя'
        assert state.is_exhausted is False

    def test_limit_is_base_plus_purchased(self):
        row = _state_row(purchased_gb=5, used_gb=2.0, limit_gb=15)
        assert _row_state(_spec(), row).limit_gb == 15

    def test_unknown_measurement_never_counts_as_exhausted(self):
        """Молчащая панель не должна выглядеть как исчерпанная квота."""
        row = _state_row(measured_known=False)
        assert _row_state(_spec(), row).is_exhausted is False

    def test_exhaustion_requires_a_known_measurement_over_the_limit(self):
        row = _state_row(used_gb=10.0)
        assert _row_state(_spec(), row).is_exhausted is True

    def test_unlimited_dimension_is_never_exhausted(self):
        row = _state_row(base_limit_gb=0, used_gb=999.0, limit_gb=0)
        assert _row_state(_spec(), row).is_exhausted is False

    def test_coverage_gap_blocks_enforcement(self):
        """Начало окна не покрыто журналом — расход занижен, блокировать нельзя."""
        row = _state_row(used_gb=10.0, window_start=date(2026, 3, 1), coverage_from=date(2026, 3, 6))
        state = _row_state(_spec(), row)

        assert state.is_exhausted is True, 'цифра сама по себе выглядит исчерпанной'
        assert state.has_coverage_gap is True
        assert state.is_enforceable is False

    def test_full_coverage_allows_enforcement(self):
        row = _state_row(used_gb=10.0, window_start=date(2026, 3, 1), coverage_from=date(2026, 3, 1))
        state = _row_state(_spec(), row)

        assert state.has_coverage_gap is False
        assert state.is_enforceable is True

    def test_base_dimension_never_reports_a_coverage_gap(self):
        """Обычный трафик приходит из панели целиком — журнал ему не нужен."""
        subscription = SimpleNamespace(traffic_limit_gb=100, purchased_traffic_gb=0, traffic_used_gb=10.0)
        state = _base_state(_spec(key=BASE_KEY), subscription)

        assert state.has_coverage_gap is False
        assert state.is_enforceable is True

    def test_unmeasured_row_is_not_enforceable(self):
        row = _state_row(measured_known=False, window_start=date(2026, 3, 1), coverage_from=date(2026, 3, 1))
        assert _row_state(_spec(), row).is_enforceable is False


class TestFormatting:
    def _state(self, **overrides):
        return _row_state(_spec(), _state_row(**{'used_gb': 6.0, **overrides}))

    def test_value_and_line(self):
        state = self._state()
        assert format_dimension_value(state) == '6.0 / 10 ГБ'
        assert format_dimension_usage(state, 'en') == '⚪ WL Traffic: 6.0 / 10 ГБ'

    def test_unlimited_mark_is_configurable_per_surface(self):
        state = self._state(base_limit_gb=0, limit_gb=0)
        assert format_dimension_value(state) == '6.0 / ∞ ГБ'
        assert format_dimension_value(state, unlimited_mark='♾️') == '6.0 / ♾️ ГБ'

    def test_blocked_state_is_visible(self):
        state = self._state(blocked_at=object(), block_reason='quota_exhausted')
        assert format_dimension_value(state).endswith('— исчерпан')

    def test_unknown_measurement_is_marked_rather_than_shown_as_zero(self):
        state = self._state(measured_known=False, used_gb=0.0)
        assert format_dimension_value(state) == '0.0 / 10 ГБ (нет данных)'
