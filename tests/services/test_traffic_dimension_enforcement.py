"""Политика ограничения измерений: топология сквадов и таблица решений.

Здесь проверяется то, что определяет, отберут ли у платящего пользователя
доступ. Два правила важнее остальных: смешанный сквад не снимается никогда, а
неизвестный расход не снимает уже стоящую блокировку.
"""

from datetime import date

import pytest

from app.services.traffic_dimension_enforcement import (
    BlastGuard,
    BlockReason,
    DimensionSquadPolicy,
    EnforcementAction,
    EnforcementMode,
    SquadTopology,
    classify_squad,
    decide,
    effective_panel_traffic_limit_bytes,
    merge_panel_squads,
    panel_squads_for,
    plan_squad_strip,
    resolve_mode,
)
from app.services.traffic_dimensions import DimensionState
from tests.services.test_traffic_dimension_ledger import make_spec


def make_state(**overrides) -> DimensionState:
    defaults = {
        'spec': make_spec(),
        'base_limit_gb': 10,
        'purchased_gb': 0,
        'limit_gb': 10,
        'used_gb': 0.0,
        'used_known': True,
        'blocked': False,
        'block_reason': None,
        'stripped_squads': (),
        'window_start': date(2026, 3, 1),
        'coverage_from': date(2026, 3, 1),
    }
    defaults.update(overrides)
    return DimensionState(**defaults)


# ------------------------------ режим ------------------------------


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('observe', EnforcementMode.OBSERVE),
        ('NOTIFY', EnforcementMode.NOTIFY),
        ('  enforce  ', EnforcementMode.ENFORCE),
    ],
)
def test_mode_parsing(raw, expected):
    assert resolve_mode(raw) == expected


@pytest.mark.parametrize('raw', [None, '', 'enfroce', 'yes', 'true'])
def test_unknown_mode_never_enables_cutting_access(raw):
    """Опечатка в настройке не должна молча включать снятие доступа."""
    assert resolve_mode(raw) is EnforcementMode.OBSERVE


# ------------------------------ топология ------------------------------


def test_squad_with_only_dimension_inbounds_is_pure():
    assert classify_squad(frozenset({'a', 'b'}), frozenset({'a', 'b', 'c'})) is SquadTopology.PURE


def test_squad_without_dimension_inbounds_is_free():
    assert classify_squad(frozenset({'x'}), frozenset({'a'})) is SquadTopology.FREE


def test_squad_with_both_kinds_is_mixed():
    assert classify_squad(frozenset({'a', 'x'}), frozenset({'a'})) is SquadTopology.MIXED


def test_squad_with_unknown_inbounds_is_free():
    """Про пустой сквад ничего не известно — снимать на догадке нельзя."""
    assert classify_squad(frozenset(), frozenset({'a'})) is SquadTopology.FREE


def test_plan_selects_only_pure_squads():
    index = {
        'pure': frozenset({'wl1'}),
        'normal': frozenset({'eu1'}),
    }
    plan = plan_squad_strip(['pure', 'normal'], index, frozenset({'wl1'}))

    assert plan.strip == frozenset({'pure'})
    assert plan.mixed == frozenset()
    assert not plan.refused


def test_mixed_squad_refuses_the_whole_plan():
    """Снять смешанный сквад — отобрать оплаченный обычный доступ."""
    index = {'mixed': frozenset({'wl1', 'eu1'}), 'pure': frozenset({'wl1'})}
    plan = plan_squad_strip(['mixed', 'pure'], index, frozenset({'wl1'}))

    assert plan.mixed == frozenset({'mixed'})
    assert plan.refused, 'частичное снятие не закрывает измерение и портит услугу'


def test_plan_records_squads_missing_from_the_panel_map():
    plan = plan_squad_strip(['ghost'], {'known': frozenset({'wl1'})}, frozenset({'wl1'}))

    assert plan.unknown == frozenset({'ghost'})
    assert plan.strip == frozenset()
    assert not plan.refused


def test_plan_is_case_insensitive():
    plan = plan_squad_strip(['PURE'], {'pure': frozenset({'wl1'})}, frozenset({'wl1'}))
    assert plan.strip == frozenset({'pure'})


# ------------------------------ состав для панели ------------------------------


def test_panel_squads_removes_stripped_and_keeps_order():
    assert panel_squads_for(['a', 'b', 'c'], ['b']) == ['a', 'c']


def test_panel_squads_without_stripping_is_identity():
    assert panel_squads_for(['a', 'b'], []) == ['a', 'b']


def test_merge_restores_stripped_squads_into_entitlement():
    """Панель их не отдаёт — они сняты нами; право подписки не должно их терять."""
    merged = merge_panel_squads(['a'], ['a', 'b'], ['b'])
    assert set(merged) == {'a', 'b'}


def test_merge_without_stripping_follows_the_panel():
    """Обычный случай: панель — источник правды, ничего не дописываем."""
    assert merge_panel_squads(['a', 'c'], ['a', 'b'], []) == ['a', 'c']


def test_merge_deduplicates():
    assert merge_panel_squads(['a', 'a'], ['a'], []) == ['a']


# ------------------------------ карта блокировок ------------------------------


def test_policy_filters_only_known_users():
    policy = DimensionSquadPolicy()
    policy.set_for('u-1', ['sq-block'])

    assert policy.filter_squads('u-1', ['sq-block', 'sq-keep']) == ['sq-keep']
    assert policy.filter_squads('u-2', ['sq-block', 'sq-keep']) == ['sq-block', 'sq-keep']


def test_policy_passes_none_through():
    """`active_internal_squads=None` означает «не трогай сквады»."""
    policy = DimensionSquadPolicy()
    policy.set_for('u-1', ['sq-block'])
    assert policy.filter_squads('u-1', None) is None


def test_policy_clear_restores_access():
    policy = DimensionSquadPolicy()
    policy.set_for('u-1', ['sq-block'])
    policy.clear_for('u-1')
    assert policy.filter_squads('u-1', ['sq-block']) == ['sq-block']


def test_policy_set_with_empty_squads_clears():
    policy = DimensionSquadPolicy()
    policy.set_for('u-1', ['sq-block'])
    policy.set_for('u-1', [])
    assert policy.blocked_uuids() == frozenset()


def test_policy_replace_all_drops_previous_entries():
    policy = DimensionSquadPolicy()
    policy.set_for('u-1', ['sq-a'])
    policy.replace_all({'u-2': ['sq-b']})

    assert policy.blocked_uuids() == frozenset({'u-2'})
    assert policy.filter_squads('u-1', ['sq-a']) == ['sq-a']


def test_policy_is_case_insensitive_about_squads():
    policy = DimensionSquadPolicy()
    policy.set_for('u-1', ['SQ-BLOCK'])
    assert policy.filter_squads('u-1', ['sq-block']) == []


# ------------------------------ таблица решений ------------------------------


PLAN = plan_squad_strip(['pure'], {'pure': frozenset({'aaa'})}, frozenset({'aaa'}))


def test_exhausted_quota_blocks():
    decision = decide(make_state(used_gb=10.0), PLAN)
    assert decision.action is EnforcementAction.BLOCK
    assert decision.reason is BlockReason.QUOTA_EXHAUSTED


def test_quota_within_limit_does_nothing():
    assert decide(make_state(used_gb=3.0), PLAN).action is EnforcementAction.NONE


def test_unlimited_dimension_is_never_blocked():
    assert decide(make_state(limit_gb=0, used_gb=999.0), PLAN).action is EnforcementAction.NONE


def test_unlimited_dimension_releases_an_existing_block():
    """Админ снял лимит — доступ обязан вернуться."""
    state = make_state(limit_gb=0, used_gb=999.0, blocked=True)
    assert decide(state, PLAN).action is EnforcementAction.UNBLOCK


def test_fresh_window_unblocks():
    state = make_state(used_gb=0.0, blocked=True)
    assert decide(state, PLAN).action is EnforcementAction.UNBLOCK


def test_already_blocked_and_still_exhausted_is_a_noop():
    """Иначе уведомление уходило бы каждый цикл."""
    state = make_state(used_gb=12.0, blocked=True)
    assert decide(state, PLAN).action is EnforcementAction.NONE


def test_unknown_usage_never_blocks():
    state = make_state(used_gb=99.0, used_known=False)
    assert decide(state, PLAN).action is EnforcementAction.NONE


def test_unknown_usage_holds_an_existing_block():
    """Молчащая панель не повод выдать квоту заново."""
    state = make_state(used_gb=0.0, used_known=False, blocked=True)
    decision = decide(state, PLAN)

    assert decision.action is EnforcementAction.HOLD
    assert decision.reason is BlockReason.UNKNOWN_USAGE_HOLD


def test_coverage_gap_holds_an_existing_block():
    """Журнал не покрывает начало окна — цифре нельзя верить в обе стороны."""
    state = make_state(
        used_gb=0.0,
        blocked=True,
        window_start=date(2026, 3, 1),
        coverage_from=date(2026, 3, 7),
    )
    assert decide(state, PLAN).action is EnforcementAction.HOLD


def test_coverage_gap_does_not_block_a_free_subscription():
    state = make_state(used_gb=50.0, window_start=date(2026, 3, 1), coverage_from=date(2026, 3, 7))
    assert decide(state, PLAN).action is EnforcementAction.NONE


def test_mixed_topology_refuses_instead_of_blocking():
    mixed_plan = plan_squad_strip(['mixed'], {'mixed': frozenset({'aaa', 'eu1'})}, frozenset({'aaa'}))
    decision = decide(make_state(used_gb=10.0), mixed_plan)

    assert decision.action is EnforcementAction.REFUSE
    assert decision.reason is BlockReason.MIXED_SQUAD


def test_notify_only_dimension_blocks_without_a_plan():
    """enforcement=notify_only: состояние фиксируем, сквады не трогаем."""
    decision = decide(make_state(used_gb=10.0), None)
    assert decision.action is EnforcementAction.BLOCK


# ------------------------------ предохранитель ------------------------------


def test_blast_guard_allows_normal_volume():
    guard = BlastGuard(max_blocks=50, max_percent=10, scanned=1000, planned=20)
    assert not guard.would_trip()


def test_blast_guard_trips_on_absolute_count():
    guard = BlastGuard(max_blocks=50, max_percent=0, scanned=100000, planned=51)
    assert guard.would_trip()
    assert '50' in guard.tripped_by


def test_blast_guard_trips_on_share():
    """Мелкая база: 30 из 100 — это поломка, а не массовое исчерпание квоты."""
    guard = BlastGuard(max_blocks=0, max_percent=10, scanned=100, planned=30)
    assert guard.would_trip()
    assert '%' in guard.tripped_by


def test_blast_guard_thresholds_are_individually_disableable():
    assert not BlastGuard(max_blocks=0, max_percent=0, scanned=10, planned=10).would_trip()


def test_blast_guard_ignores_share_without_scanned():
    assert not BlastGuard(max_blocks=0, max_percent=10, scanned=0, planned=5).would_trip()


# ------------------------------ режим учёта ------------------------------

GB = 1024**3
SHIELDED = make_spec(key='wl', accounting_mode='shielded')
SUBQUOTA = make_spec(key='wl', accounting_mode='subquota')


def test_subquota_leaves_the_panel_limit_alone():
    """Поведение «как панель»: трафик измерения расходует и основную квоту."""
    state = make_state(spec=SUBQUOTA, used_gb=7.0)
    assert effective_panel_traffic_limit_bytes(100, [state]) == 100 * GB


def test_shielded_raises_the_limit_by_the_dimension_usage():
    state = make_state(spec=SHIELDED, used_gb=7.0)
    assert effective_panel_traffic_limit_bytes(100, [state]) == 107 * GB


def test_shield_rounds_up_to_whole_gigabytes():
    """Гистерезис: лимит переставляется не чаще раза на гигабайт."""
    assert effective_panel_traffic_limit_bytes(100, [make_state(spec=SHIELDED, used_gb=0.1)]) == 101 * GB
    assert effective_panel_traffic_limit_bytes(100, [make_state(spec=SHIELDED, used_gb=0.9)]) == 101 * GB


def test_unknown_usage_never_inflates_the_limit():
    """Иначе молчащая панель раздала бы лишнюю квоту на выдуманную величину."""
    state = make_state(spec=SHIELDED, used_gb=50.0, used_known=False)
    assert effective_panel_traffic_limit_bytes(100, [state]) == 100 * GB


def test_unlimited_base_stays_unlimited():
    state = make_state(spec=SHIELDED, used_gb=50.0)
    assert effective_panel_traffic_limit_bytes(0, [state]) == 0


def test_shields_from_several_dimensions_add_up():
    first = make_state(spec=SHIELDED, used_gb=3.0)
    second = make_state(spec=make_spec(key='torrent', accounting_mode='shielded'), used_gb=4.0)
    ignored = make_state(spec=SUBQUOTA, used_gb=100.0)

    assert effective_panel_traffic_limit_bytes(50, [first, second, ignored]) == 57 * GB


# ------------------------------ отбор для предупреждений ------------------------------


def test_near_limit_selects_only_actionable_states():
    """Уведомителю отдаём только то, по чему он может принять решение."""
    from app.services.traffic_dimension_reconciler import _is_near_limit

    assert _is_near_limit(make_state(used_gb=8.0)), '80% — в диапазоне порогов'
    assert not _is_near_limit(make_state(used_gb=4.0)), 'ниже минимально возможного порога 50%'
    assert not _is_near_limit(make_state(limit_gb=0, used_gb=999.0)), 'безлимит не предупреждают'
    assert not _is_near_limit(make_state(used_gb=10.0)), 'исчерпано — это уже блокировка, а не подсказка'
    assert not _is_near_limit(make_state(used_gb=9.0, blocked=True)), 'уже заблокировано'
    assert not _is_near_limit(make_state(used_gb=9.0, used_known=False)), 'цифре нельзя верить'
    assert not _is_near_limit(make_state(used_gb=9.0, window_start=date(2026, 3, 1), coverage_from=date(2026, 3, 7))), (
        'дыра в журнале — расход занижен'
    )
