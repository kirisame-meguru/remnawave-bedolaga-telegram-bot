"""Фильтр блокировки на границе API.

Держится вся конструкция именно на этом: исходящих обновлений панели два
десятка (синк, продление, смена тарифа, админские правки), и ни одно из них про
измерения трафика не знает. Если фильтр стоит не на границе, а в реконсиляторе,
то первое же постороннее обновление вернёт заблокированному пользователю
снятые сквады — молча и до следующего цикла.
"""

from types import SimpleNamespace

import pytest

from app.external.remnawave_api import RemnaWaveAPI
from app.services.traffic_dimension_enforcement import dimension_squad_policy


PANEL_ID = 101
OTHER_PANEL_ID = 202


@pytest.fixture(autouse=True)
def clean_policy():
    dimension_squad_policy.replace_all({})
    yield
    dimension_squad_policy.replace_all({})


class RecordingAPI(RemnaWaveAPI):
    """Клиент, который вместо запроса запоминает payload."""

    def __init__(self):
        super().__init__(base_url='https://panel.invalid', api_key='k')
        self.payloads: list[dict] = []

    async def _make_request(self, method, endpoint, data=None, params=None):
        self.payloads.append(dict(data or {}))
        return {'response': {'id': PANEL_ID, 'username': 'x', 'activeInternalSquads': []}}

    def _parse_user(self, data):
        return SimpleNamespace(id=data.get('id'), hwid_device_limit=None)

    async def enrich_user_with_happ_link(self, user):
        return user


@pytest.mark.asyncio
async def test_blocked_squads_are_stripped_from_any_update():
    api = RecordingAPI()
    dimension_squad_policy.set_for(PANEL_ID, ['sq-wl'])

    await api.update_user(user_id=PANEL_ID, active_internal_squads=['sq-wl', 'sq-eu'])

    assert api.payloads[0]['activeInternalSquads'] == ['sq-eu']


@pytest.mark.asyncio
async def test_unblocked_users_pass_through_untouched():
    api = RecordingAPI()
    dimension_squad_policy.set_for(OTHER_PANEL_ID, ['sq-wl'])

    await api.update_user(user_id=PANEL_ID, active_internal_squads=['sq-wl', 'sq-eu'])

    assert api.payloads[0]['activeInternalSquads'] == ['sq-wl', 'sq-eu']


@pytest.mark.asyncio
async def test_updates_that_do_not_touch_squads_stay_untouched():
    """`active_internal_squads=None` означает «сквады не трогай»."""
    api = RecordingAPI()
    dimension_squad_policy.set_for(PANEL_ID, ['sq-wl'])

    await api.update_user(user_id=PANEL_ID, description='ping')

    assert 'activeInternalSquads' not in api.payloads[0]


@pytest.mark.asyncio
async def test_restoring_all_squads_is_blocked_until_the_policy_is_cleared():
    """Ровно тот сценарий, ради которого фильтр и стоит на границе."""
    api = RecordingAPI()
    dimension_squad_policy.set_for(PANEL_ID, ['sq-wl'])

    await api.update_user(user_id=PANEL_ID, active_internal_squads=['sq-wl', 'sq-eu'])
    assert api.payloads[-1]['activeInternalSquads'] == ['sq-eu']

    dimension_squad_policy.clear_for(PANEL_ID)
    await api.update_user(user_id=PANEL_ID, active_internal_squads=['sq-wl', 'sq-eu'])
    assert api.payloads[-1]['activeInternalSquads'] == ['sq-wl', 'sq-eu']


@pytest.mark.asyncio
async def test_filter_is_case_insensitive():
    api = RecordingAPI()
    dimension_squad_policy.set_for(PANEL_ID, ['SQ-WL'])

    await api.update_user(user_id=PANEL_ID, active_internal_squads=['sq-wl', 'sq-eu'])

    assert api.payloads[0]['activeInternalSquads'] == ['sq-eu']
