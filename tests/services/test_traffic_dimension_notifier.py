"""Уведомления об измерениях: что отправляется, кому и в каком режиме.

Главное, что здесь закреплено: `observe` молчит полностью, `notify` не обещает
пользователю отключения, которого не было, а откаченная блокировка не порождает
сообщения о закрытом доступе.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from app.services.notification_delivery_service import NotificationType
from app.services.traffic_dimension_enforcement import (
    BlockReason,
    EnforcementAction,
    EnforcementMode,
)
from app.services.traffic_dimension_notifier import TrafficDimensionNotifier
from tests.services.test_traffic_dimension_enforcement import make_state
from tests.services.test_traffic_dimension_ledger import make_spec


SPEC = make_spec(key='wl', inbounds=('aaa',))


def make_user(user_id=1, language='ru', **prefs):
    return SimpleNamespace(
        id=user_id,
        telegram_id=1000 + user_id,
        language=language,
        notification_settings=dict(prefs),
        status='active',
        email=None,
        email_verified=False,
    )


def make_transition(action, *, applied=True, used_gb=12.0, user_id=1, reason=None):
    return SimpleNamespace(
        subscription_id=10,
        user_id=user_id,
        remnawave_uuid='u-1',
        spec=SPEC,
        state=make_state(spec=SPEC, used_gb=used_gb, limit_gb=10),
        action=action,
        reason=reason,
        applied=applied,
        stripped_squads=(),
    )


def make_snapshot(*, used_gb=8.5, user_id=1):
    return SimpleNamespace(
        subscription_id=10,
        user_id=user_id,
        spec=SPEC,
        state=make_state(spec=SPEC, used_gb=used_gb, limit_gb=10, window_start=date(2026, 3, 1)),
    )


def make_report(mode, *, transitions=(), near_limit=(), blast_guard_tripped=None):
    return SimpleNamespace(
        mode=mode,
        transitions=list(transitions),
        near_limit=list(near_limit),
        blast_guard_tripped=blast_guard_tripped,
    )


class Harness:
    """Уведомитель с подменёнными БД, доставкой и кэшем."""

    def __init__(self, monkeypatch, users):
        import app.services.traffic_dimension_notifier as module

        self.sent: list[tuple[int, NotificationType, str]] = []
        self.admin: list[str] = []
        self.notifier = TrafficDimensionNotifier()
        self.notifier.set_bot(object())
        self._users = {user.id: user for user in users}

        async def fake_load_users(db, report):
            return dict(self._users)

        monkeypatch.setattr(self.notifier, '_load_users', fake_load_users)

        class FakeSession:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        monkeypatch.setattr(module, 'AsyncSessionLocal', lambda: FakeSession())

        async def fake_send(*, user, notification_type, context, bot=None, telegram_message=None, **kw):
            self.sent.append((user.id, notification_type, telegram_message))
            return True

        monkeypatch.setattr(module.notification_delivery_service, 'send_notification', fake_send)

        # Кэш в тестах не поднят: пустой заглушкой окно предупреждений всегда свободно.
        seen: set[str] = set()

        async def fake_get(key):
            return '1' if key in seen else None

        async def fake_set(key, value, expire=None):
            seen.add(key)
            return True

        monkeypatch.setattr(module.cache, 'get', fake_get)
        monkeypatch.setattr(module.cache, 'set', fake_set)

        async def fake_alert(report):
            self.admin.append(str(report.blast_guard_tripped))

        monkeypatch.setattr(self.notifier, '_alert_admins', fake_alert)

    @property
    def kinds(self):
        return [kind for _, kind, _ in self.sent]


# ------------------------------ режимы ------------------------------


@pytest.mark.asyncio
async def test_observe_sends_nothing(monkeypatch):
    """Наблюдение обязано быть беззвучным — иначе это уже не наблюдение."""
    harness = Harness(monkeypatch, [make_user()])
    report = make_report(
        EnforcementMode.OBSERVE,
        transitions=[make_transition(EnforcementAction.BLOCK)],
        near_limit=[make_snapshot()],
    )

    assert await harness.notifier.notify(report) == 0
    assert harness.sent == []


@pytest.mark.asyncio
async def test_notify_mode_does_not_claim_access_was_cut(monkeypatch):
    """В notify доступ ещё открыт: обещать обратное нельзя."""
    harness = Harness(monkeypatch, [make_user()])
    report = make_report(EnforcementMode.NOTIFY, transitions=[make_transition(EnforcementAction.BLOCK)])

    await harness.notifier.notify(report)

    assert harness.kinds == [NotificationType.TRAFFIC_DIMENSION_EXHAUSTED]


@pytest.mark.asyncio
async def test_enforce_mode_reports_the_block(monkeypatch):
    harness = Harness(monkeypatch, [make_user()])
    report = make_report(EnforcementMode.ENFORCE, transitions=[make_transition(EnforcementAction.BLOCK)])

    await harness.notifier.notify(report)

    assert harness.kinds == [NotificationType.TRAFFIC_DIMENSION_BLOCKED]


@pytest.mark.asyncio
async def test_rolled_back_block_sends_nothing(monkeypatch):
    """Панель не приняла запись — доступ открыт, сообщать не о чем."""
    harness = Harness(monkeypatch, [make_user()])
    report = make_report(
        EnforcementMode.ENFORCE,
        transitions=[make_transition(EnforcementAction.BLOCK, applied=False)],
    )

    await harness.notifier.notify(report)

    assert harness.sent == []


@pytest.mark.asyncio
async def test_unblock_is_reported(monkeypatch):
    harness = Harness(monkeypatch, [make_user()])
    report = make_report(
        EnforcementMode.ENFORCE,
        transitions=[make_transition(EnforcementAction.UNBLOCK, used_gb=0.0)],
    )

    await harness.notifier.notify(report)

    assert harness.kinds == [NotificationType.TRAFFIC_DIMENSION_RESTORED]


@pytest.mark.asyncio
@pytest.mark.parametrize('action', [EnforcementAction.HOLD, EnforcementAction.REFUSE])
async def test_hold_and_refuse_are_not_user_facing(monkeypatch, action):
    """Пользователь не может ни починить топологию, ни оживить панель."""
    harness = Harness(monkeypatch, [make_user()])
    report = make_report(
        EnforcementMode.ENFORCE,
        transitions=[make_transition(action, reason=BlockReason.MIXED_SQUAD)],
    )

    await harness.notifier.notify(report)

    assert harness.sent == []


# ------------------------------ предупреждения ------------------------------


@pytest.mark.asyncio
async def test_warning_respects_the_user_threshold(monkeypatch):
    harness = Harness(monkeypatch, [make_user(traffic_warning_percent=90)])
    report = make_report(EnforcementMode.ENFORCE, near_limit=[make_snapshot(used_gb=8.5)])

    await harness.notifier.notify(report)
    assert harness.sent == [], '85% ниже выбранного порога 90%'

    report = make_report(EnforcementMode.ENFORCE, near_limit=[make_snapshot(used_gb=9.5)])
    await harness.notifier.notify(report)
    assert harness.kinds == [NotificationType.TRAFFIC_DIMENSION_WARNING]


@pytest.mark.asyncio
async def test_warning_respects_the_user_opt_out(monkeypatch):
    harness = Harness(monkeypatch, [make_user(traffic_warning_enabled=False)])
    report = make_report(EnforcementMode.ENFORCE, near_limit=[make_snapshot(used_gb=9.9)])

    await harness.notifier.notify(report)

    assert harness.sent == []


@pytest.mark.asyncio
async def test_block_ignores_the_warning_opt_out(monkeypatch):
    """Отключение подсказок не отключает сообщение о закрытом доступе."""
    harness = Harness(monkeypatch, [make_user(traffic_warning_enabled=False)])
    report = make_report(EnforcementMode.ENFORCE, transitions=[make_transition(EnforcementAction.BLOCK)])

    await harness.notifier.notify(report)

    assert harness.kinds == [NotificationType.TRAFFIC_DIMENSION_BLOCKED]


@pytest.mark.asyncio
async def test_warning_is_sent_once_per_window(monkeypatch):
    harness = Harness(monkeypatch, [make_user()])
    report = make_report(EnforcementMode.ENFORCE, near_limit=[make_snapshot(used_gb=9.0)])

    await harness.notifier.notify(report)
    await harness.notifier.notify(report)

    assert len(harness.sent) == 1, 'второй цикл через три часа не должен повторять предупреждение'


@pytest.mark.asyncio
async def test_new_window_rearms_the_warning(monkeypatch):
    """Ключ включает начало окна: новый период — новое предупреждение."""
    harness = Harness(monkeypatch, [make_user()])

    first = make_snapshot(used_gb=9.0)
    await harness.notifier.notify(make_report(EnforcementMode.ENFORCE, near_limit=[first]))

    second = make_snapshot(used_gb=9.0)
    second.state = make_state(spec=SPEC, used_gb=9.0, limit_gb=10, window_start=date(2026, 4, 1))
    await harness.notifier.notify(make_report(EnforcementMode.ENFORCE, near_limit=[second]))

    assert len(harness.sent) == 2


# ------------------------------ текст ------------------------------


@pytest.mark.asyncio
async def test_message_carries_the_admin_authored_dimension_title(monkeypatch):
    """Заголовки измерений — строки из БД, а не ключи локализации."""
    harness = Harness(monkeypatch, [make_user()])
    report = make_report(EnforcementMode.ENFORCE, transitions=[make_transition(EnforcementAction.BLOCK)])

    await harness.notifier.notify(report)

    _, _, message = harness.sent[0]
    assert SPEC.label('ru') in message
    assert '12.0' in message and '10' in message


@pytest.mark.asyncio
async def test_message_follows_the_user_language(monkeypatch):
    harness = Harness(monkeypatch, [make_user(user_id=1)])
    harness._users[1].language = 'en'
    report = make_report(EnforcementMode.ENFORCE, transitions=[make_transition(EnforcementAction.BLOCK)])

    await harness.notifier.notify(report)

    _, _, message = harness.sent[0]
    assert 'access suspended' in message.lower()


@pytest.mark.asyncio
async def test_unknown_user_is_skipped_without_crashing(monkeypatch):
    harness = Harness(monkeypatch, [])
    report = make_report(
        EnforcementMode.ENFORCE,
        transitions=[make_transition(EnforcementAction.BLOCK, user_id=999)],
    )

    assert await harness.notifier.notify(report) == 0


# ------------------------------ email-канал ------------------------------


@pytest.mark.parametrize(
    'kind',
    [
        NotificationType.TRAFFIC_DIMENSION_WARNING,
        NotificationType.TRAFFIC_DIMENSION_EXHAUSTED,
        NotificationType.TRAFFIC_DIMENSION_BLOCKED,
        NotificationType.TRAFFIC_DIMENSION_RESTORED,
    ],
)
def test_every_dimension_event_has_an_email_template(kind):
    """Пользователь без Telegram обязан узнать о закрытом доступе тоже."""
    from app.cabinet.services.email_templates import EmailNotificationTemplates

    template = EmailNotificationTemplates().get_template(
        kind,
        'ru',
        {'dimension': '⚪ WL', 'used': 12.0, 'limit': 10, 'percent': 120.0},
    )

    assert template is not None, f'{kind.value} молча пропустил бы email'
    assert template['subject']
    assert '⚪ WL' in template['body_html']
    assert '<b>' not in template['subject'], 'тема письма — текст, а не разметка'


def test_email_copy_comes_from_the_same_locale_key():
    """Одна формулировка на оба канала: иначе правку внесут в одном месте."""
    from app.cabinet.services.email_templates import EmailNotificationTemplates
    from app.localization.texts import get_texts

    context = {'dimension': '⚪ WL', 'used': 12.0, 'limit': 10, 'percent': 120.0}
    template = EmailNotificationTemplates().get_template(NotificationType.TRAFFIC_DIMENSION_BLOCKED, 'en', context)
    telegram = get_texts('en').t('TRAFFIC_DIMENSION_BLOCKED', '').format(**context)

    tail = telegram.split('\n')[-1].strip()
    assert tail and tail in template['body_html'].replace('<br>', '\n')
