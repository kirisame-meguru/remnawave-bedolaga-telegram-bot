"""`/traffic_why <telegram_id>` — почему у пользователя такие цифры по измерениям.

Отвечает на единственный вопрос, который возникает в поддержке: «клиент
говорит, что не расходовал столько» или «почему у него отключилось». Без этой
команды разбираться пришлось бы по логам и SQL, а к тому моменту панель уже
вычистит пер-инбаунд историю и проверить будет нечего.

Показывает то, чего не видно ни на одном пользовательском экране: границу
расчётного окна, покрытие журнала наблюдений, посуточную разбивку и причину
блокировки. Именно по этим четырём вещам расходятся ожидание и реальность.
"""

from datetime import UTC, datetime

import structlog
from aiogram import Dispatcher, types
from aiogram.filters import Command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.subscription import get_subscription_by_user_id
from app.database.crud.user import get_user_by_telegram_id
from app.database.models import TrafficDimensionSample, User
from app.services.traffic_dimension_enforcement import BlockReason, dimension_squad_policy
from app.services.traffic_dimension_ledger import resolve_window_start
from app.services.traffic_dimensions import get_dimension_states, traffic_dimensions
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

# Сколько последних суток показывать в разбивке. Больше не влезает в сообщение,
# а для разбора жалобы хватает недели — ровно столько хранит и сама панель.
_DAILY_ROWS = 10

_BLOCK_REASON_TEXT = {
    BlockReason.QUOTA_EXHAUSTED.value: 'квота исчерпана',
    BlockReason.MIXED_SQUAD.value: 'смешанные сквады — ограничение не применено',
    BlockReason.UNKNOWN_USAGE_HOLD.value: 'расход неизвестен, блокировка удержана',
}


def _fmt_date(value) -> str:
    return value.isoformat() if value else '—'


async def _daily_breakdown(db: AsyncSession, remnawave_uuid: str, spec, window_start) -> list[str]:
    """Посуточные наблюдения журнала по инбаундам измерения."""
    if not remnawave_uuid or not spec.inbound_uuids:
        return []

    result = await db.execute(
        select(TrafficDimensionSample.usage_date, TrafficDimensionSample.bytes)
        .where(
            TrafficDimensionSample.remnawave_uuid == remnawave_uuid,
            TrafficDimensionSample.inbound_uuid.in_(sorted(spec.inbound_uuids)),
            TrafficDimensionSample.usage_date >= window_start,
        )
        .order_by(TrafficDimensionSample.usage_date.desc())
    )

    per_day: dict = {}
    for usage_date, value in result.all():
        per_day[usage_date] = per_day.get(usage_date, 0) + int(value or 0)

    lines = []
    for usage_date in sorted(per_day, reverse=True)[:_DAILY_ROWS]:
        gb = per_day[usage_date] / (1024**3)
        lines.append(f'    {usage_date.isoformat()}: {gb:.2f} ГБ')
    return lines


@admin_required
@error_handler
async def traffic_why(message: types.Message, db_user: User, db: AsyncSession):
    """Разбор состояния измерений трафика конкретного пользователя."""
    parts = (message.text or '').split()
    if len(parts) < 2:
        await message.answer(
            '📐 <b>Разбор измерений трафика</b>\n\nИспользование: <code>/traffic_why &lt;telegram_id&gt;</code>',
            parse_mode='HTML',
        )
        return

    try:
        telegram_id = int(parts[1])
    except ValueError:
        await message.answer('Telegram ID должен быть числом.')
        return

    user = await get_user_by_telegram_id(db, telegram_id)
    if not user:
        await message.answer(f'Пользователь {telegram_id} не найден.')
        return

    subscription = await get_subscription_by_user_id(db, user.id)
    if not subscription:
        await message.answer(f'У пользователя {telegram_id} нет подписки.')
        return

    specs = await traffic_dimensions.non_base(db)
    if not specs:
        await message.answer('Измерения трафика не заведены.')
        return

    today = datetime.now(UTC).date()
    window_start = resolve_window_start(subscription, today=today)
    states = await get_dimension_states(db, subscription, specs=specs)
    stripped = dimension_squad_policy.stripped_for(subscription.remnawave_uuid or '')

    lines = [
        '📐 <b>Разбор измерений трафика</b>',
        '',
        f'Пользователь: <code>{telegram_id}</code>',
        f'Подписка: <code>#{subscription.id}</code>, панель: <code>{subscription.remnawave_uuid or "—"}</code>',
        f'Расчётное окно с: <code>{window_start.isoformat()}</code> (сегодня {today.isoformat()})',
        f'Сквады подписки: <code>{", ".join(subscription.connected_squads or []) or "—"}</code>',
    ]
    if stripped:
        lines.append(f'Сняты блокировкой: <code>{", ".join(sorted(stripped))}</code>')
    lines.append('')

    for state in states:
        limit = '♾️' if state.is_unlimited else f'{state.limit_gb} ГБ'
        lines.append(f'<b>{state.spec.label(db_user.language)}</b> (<code>{state.spec.key}</code>)')
        lines.append(f'  Израсходовано: {state.used_gb:.2f} из {limit}')
        lines.append(f'  База тарифа: {state.base_limit_gb} ГБ, докуплено: {state.purchased_gb} ГБ')

        if not state.used_known:
            lines.append('  ⚠️ Последнее измерение не удалось — цифре верить нельзя.')
        elif state.has_coverage_gap:
            # Самая частая причина расхождения с ожиданием пользователя.
            lines.append(
                f'  ⚠️ Журнал покрывает окно только с {_fmt_date(state.coverage_from)} — '
                'расход занижен, блокировка по нему не применяется.'
            )
        else:
            lines.append(f'  ✅ Журнал покрывает окно с {_fmt_date(state.coverage_from)}')

        if state.blocked:
            reason = _BLOCK_REASON_TEXT.get(state.block_reason, state.block_reason or 'причина не указана')
            lines.append(f'  🚫 Заблокировано: {reason}')
            if state.stripped_squads:
                lines.append(f'  Сняты сквады: <code>{", ".join(state.stripped_squads)}</code>')
        elif state.is_exhausted:
            lines.append('  ⏳ Квота исчерпана, блокировка ещё не применена (режим или предохранитель).')

        lines.append(f'  Инбаунды: <code>{", ".join(sorted(state.spec.inbound_uuids)) or "не заданы"}</code>')

        breakdown = await _daily_breakdown(db, subscription.remnawave_uuid or '', state.spec, window_start)
        if breakdown:
            lines.append('  По суткам (UTC):')
            lines.extend(breakdown)
        else:
            lines.append('  По суткам: наблюдений нет')
        lines.append('')

    text = '\n'.join(lines)
    # Telegram режет длинные сообщения — при многих измерениях отдаём частями.
    for chunk_start in range(0, len(text), 4000):
        await message.answer(text[chunk_start : chunk_start + 4000], parse_mode='HTML')


def register_handlers(dp: Dispatcher):
    dp.message.register(traffic_why, Command('traffic_why'))
