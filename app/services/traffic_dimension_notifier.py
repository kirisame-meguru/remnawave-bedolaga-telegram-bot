"""Уведомления об измерениях трафика.

Три события и ровно одно правило для каждого:

* **Предупреждение** («трафик заканчивается») — это подсказка, и она подчиняется
  пользовательской настройке ``traffic_warning_enabled`` и его же порогу.
* **Исчерпание и блокировка** — это изменение услуги, а не подсказка. Их
  отключить нельзя: молча закрыть доступ и не сказать об этом — худшее, что
  можно сделать с платящим пользователем.
* **Смешанные сквады** — проблема администратора, а не пользователя. Ему и
  уходит: пользователь ничего сделать с топологией не может.

Формулировка блокировки зависит от режима. В ``notify`` доступ ещё открыт, и
обещать пользователю обратное нельзя — поэтому текст другой: квота кончилась,
но пока работает. В ``observe`` не отправляется вообще ничего.

Доставка идёт через ``NotificationDeliveryService``, а не через
``bot.send_message``: у части пользователей нет Telegram, и им нужен email или
websocket кабинета.
"""

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.localization.texts import get_texts
from app.services.notification_delivery_service import (
    NotificationType,
    notification_delivery_service,
)
from app.services.traffic_dimension_enforcement import EnforcementAction, EnforcementMode
from app.utils.cache import cache
from app.utils.notification_prefs import get_traffic_warning_percent, is_traffic_warning_enabled


logger = structlog.get_logger(__name__)

# Одно предупреждение в сутки на измерение. Ключ включает начало окна, поэтому
# новый расчётный период взводит предупреждение заново, не дожидаясь суток.
_WARNING_TTL_SECONDS = 86400


class TrafficDimensionNotifier:
    """Превращает отчёт реконсилятора в сообщения пользователям и админам."""

    def __init__(self) -> None:
        self.bot = None

    def set_bot(self, bot) -> None:
        self.bot = bot

    async def __call__(self, report) -> int:
        """Хук реконсилятора: он зовёт уведомителя, ничего о нём не зная."""
        return await self.notify(report)

    async def notify(self, report) -> int:
        if report.mode is EnforcementMode.OBSERVE:
            # Наблюдение не отправляет ничего — иначе это уже не наблюдение.
            return 0

        sent = 0
        async with AsyncSessionLocal() as db:
            users = await self._load_users(db, report)
            sent += await self._notify_transitions(report, users)
            sent += await self._notify_near_limit(report, users)
        await self._alert_admins(report)
        return sent

    # ------------------------------ пользователи ------------------------------

    async def _load_users(self, db, report) -> dict[int, User]:
        """Один запрос на весь отчёт вместо запроса на сообщение."""
        user_ids = {item.user_id for item in report.transitions} | {item.user_id for item in report.near_limit}
        if not user_ids:
            return {}
        result = await db.execute(select(User).where(User.id.in_(user_ids)))
        return {user.id: user for user in result.scalars().all()}

    async def _notify_transitions(self, report, users: dict[int, User]) -> int:
        sent = 0
        for transition in report.transitions:
            user = users.get(transition.user_id)
            if user is None:
                continue

            if transition.action is EnforcementAction.BLOCK:
                if report.mode is EnforcementMode.ENFORCE and transition.applied:
                    kind = NotificationType.TRAFFIC_DIMENSION_BLOCKED
                elif report.mode is EnforcementMode.NOTIFY:
                    # Доступ ещё открыт — обещать обратное нельзя.
                    kind = NotificationType.TRAFFIC_DIMENSION_EXHAUSTED
                else:
                    # enforce, но в панель не доехало: блокировка откачена,
                    # сообщать не о чем.
                    continue
            elif transition.action is EnforcementAction.UNBLOCK:
                kind = NotificationType.TRAFFIC_DIMENSION_RESTORED
            else:
                # HOLD и REFUSE пользователю не адресованы.
                continue

            if await self._send(user, kind, transition.spec, transition.state):
                sent += 1
        return sent

    async def _notify_near_limit(self, report, users: dict[int, User]) -> int:
        sent = 0
        for snapshot in report.near_limit:
            user = users.get(snapshot.user_id)
            if user is None or not is_traffic_warning_enabled(user):
                continue
            if snapshot.state.used_percent < get_traffic_warning_percent(user):
                continue
            if not await self._claim_warning_slot(snapshot):
                continue
            if await self._send(
                user,
                NotificationType.TRAFFIC_DIMENSION_WARNING,
                snapshot.spec,
                snapshot.state,
            ):
                sent += 1
        return sent

    async def _claim_warning_slot(self, snapshot) -> bool:
        """Не чаще раза в сутки на измерение и не более раза на окно.

        Начало окна входит в ключ: новый расчётный период сбрасывает счётчик и
        предупреждение приходит снова, не дожидаясь истечения суток.
        """
        key = f'tdim_warn:{snapshot.subscription_id}:{snapshot.spec.id}:{snapshot.state.window_start}'
        try:
            if await cache.get(key):
                return False
            await cache.set(key, '1', expire=_WARNING_TTL_SECONDS)
        except Exception:
            return True
        return True

    async def _send(self, user: User, kind: NotificationType, spec, state) -> bool:
        language = getattr(user, 'language', None) or 'ru'
        texts = get_texts(language)
        template = texts.t(kind.value.upper(), '')
        if not template:
            logger.warning('Нет текста уведомления об измерении', key=kind.value)
            return False

        context = {
            'dimension': spec.label(language),
            'used': float(state.used_gb or 0.0),
            'limit': int(state.limit_gb or 0),
            'percent': float(state.used_percent),
        }
        try:
            message = template.format(**context)
        except (KeyError, IndexError, ValueError) as e:
            logger.error('Сломан шаблон уведомления об измерении', key=kind.value, error=e)
            return False

        try:
            return await notification_delivery_service.send_notification(
                user=user,
                notification_type=kind,
                context=context,
                bot=self.bot,
                telegram_message=message,
            )
        except Exception as e:
            logger.warning('Не удалось отправить уведомление об измерении', user_id=user.id, error=e)
            return False

    # ------------------------------ администраторы ------------------------------

    async def _alert_admins(self, report) -> None:
        """Сигналит о том, что пользователь исправить не может.

        Смешанный сквад — это ошибка настройки: измерение объявлено, но снять
        его нельзя, не отобрав оплаченный обычный доступ. Пока топология такая,
        квота не работает вовсе, и знать об этом должен администратор.
        """
        refused = [t for t in report.transitions if t.action is EnforcementAction.REFUSE]
        if not refused and not report.blast_guard_tripped:
            return
        if not self.bot:
            return

        from app.services.admin_notification_service import AdminNotificationService, NotificationCategory

        service = AdminNotificationService(self.bot)
        lines: list[str] = []

        if report.blast_guard_tripped:
            lines.append(
                '🛑 <b>Предохранитель ограничений трафика</b>\n\n'
                f'Блокировки цикла отменены: {report.blast_guard_tripped}.\n'
                f'Проверьте инбаунды измерений и карту сквадов — массовая блокировка '
                f'почти всегда означает поломку настройки, а не исчерпание квоты.'
            )

        if refused:
            names = sorted({t.spec.key for t in refused})
            lines.append(
                '⚠️ <b>Измерение нельзя ограничить</b>\n\n'
                f'Подписок: {len(refused)}. Измерения: {", ".join(names)}.\n'
                'В сквадах этих подписок инбаунды измерения соседствуют с обычными, '
                'поэтому снятие сквада отобрало бы оплаченный обычный доступ. '
                'Квота по измерению не применяется, пока инбаунды не разнесены '
                'по отдельным сквадам.'
            )

        for text in lines:
            try:
                await service.send_admin_notification(text, category=NotificationCategory.INFRASTRUCTURE)
            except Exception as e:
                logger.warning('Не удалось отправить админское уведомление об измерениях', error=e)


traffic_dimension_notifier = TrafficDimensionNotifier()


def install_notifier(bot) -> None:
    """Связывает уведомителя с реконсилятором на старте бота."""
    from app.services.traffic_dimension_reconciler import traffic_dimension_reconciler

    traffic_dimension_notifier.set_bot(bot)
    traffic_dimension_reconciler.set_notifier(traffic_dimension_notifier)
    logger.info(
        'Уведомления об измерениях трафика подключены',
        mode=getattr(settings, 'TRAFFIC_DIMENSION_ENFORCEMENT_MODE', 'observe'),
    )
