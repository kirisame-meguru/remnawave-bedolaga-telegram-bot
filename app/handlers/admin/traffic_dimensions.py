"""Админка измерений трафика.

Измерение — отдельный счётчик трафика по своему набору инбаундов панели.
Обычный трафик (`base`) тоже показан в списке, но правится только частично:
его лимиты живут в тарифах и подписках, а не здесь.

Раньше WL настраивался тремя переменными окружения; теперь всё, что тогда
задавалось ими, — набор инбаундов, лимит по умолчанию, включённость —
редактируется здесь, а измерений может быть сколько угодно.
"""

import math

import structlog
from aiogram import Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.traffic_dimension import (
    TrafficDimensionKeyError,
    count_dimension_subscriptions,
    create_traffic_dimension,
    delete_traffic_dimension,
    get_traffic_dimension,
    list_traffic_dimensions,
    set_dimension_inbounds,
    update_traffic_dimension,
)
from app.database.models import TrafficAccountingMode, TrafficDimension, TrafficDimensionEnforcement, User
from app.services.remnawave_service import RemnaWaveService
from app.states import AdminStates
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

INBOUNDS_PAGE_SIZE = 8

BACK_TO_SETTINGS = 'admin_submenu_settings'
LIST_CALLBACK = 'admin_traffic_dims'

_ENFORCEMENT_LABELS = {
    TrafficDimensionEnforcement.SQUAD_STRIP.value: '🚫 Снимать сквады',
    TrafficDimensionEnforcement.NOTIFY_ONLY.value: '🔔 Только уведомлять',
    TrafficDimensionEnforcement.PANEL_LIMIT.value: '⚙️ Лимит панели',
}

_ACCOUNTING_LABELS = {
    TrafficAccountingMode.SUBQUOTA.value: '📉 Расходует общий трафик',
    TrafficAccountingMode.SHIELDED.value: '🛡️ Не расходует общий трафик',
}


def _title_of(dimension: TrafficDimension) -> str:
    titles = dimension.title or {}
    return titles.get('ru') or dimension.fallback_title or dimension.key


def _label_of(dimension: TrafficDimension) -> str:
    title = _title_of(dimension)
    return f'{dimension.icon} {title}'.strip() if dimension.icon else title


# ============================ Список ============================


async def _render_list(callback: types.CallbackQuery, db: AsyncSession) -> None:
    dimensions = await list_traffic_dimensions(db)

    lines = [
        '📐 <b>Измерения трафика</b>',
        '',
        'Каждое измерение — отдельный счётчик по своему набору инбаундов панели.',
        'Когда его квота исчерпана, доступ к этим инбаундам снимается, а остальной',
        'трафик продолжает работать.',
        '',
    ]

    keyboard: list[list[types.InlineKeyboardButton]] = []
    for dimension in dimensions:
        if dimension.is_builtin:
            state_icon = '⚙️'
            suffix = 'обычный трафик'
        elif not dimension.is_enabled:
            state_icon = '⏸️'
            suffix = 'выключено'
        elif not (dimension.inbound_uuids or []):
            state_icon = '⚠️'
            suffix = 'нет инбаундов'
        else:
            state_icon = '✅'
            suffix = f'инбаундов: {len(dimension.inbound_uuids or [])}'
        lines.append(f'{state_icon} {_label_of(dimension)} — {suffix}')
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=f'{state_icon} {_label_of(dimension)}',
                    callback_data=f'admin_traffic_dim:{dimension.id}',
                )
            ]
        )

    if not dimensions:
        lines.append('Измерений пока нет.')

    keyboard.append([types.InlineKeyboardButton(text='➕ Новое измерение', callback_data='admin_traffic_dim_new')])
    keyboard.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data=BACK_TO_SETTINGS)])

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def show_dimensions(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    await _render_list(callback, db)
    await callback.answer()


# ============================ Карточка ============================


async def _render_dimension(callback: types.CallbackQuery, db: AsyncSession, dimension: TrafficDimension) -> None:
    inbounds = dimension.inbound_uuids or []
    users_count = await count_dimension_subscriptions(db, dimension.id)
    limit_display = '♾️ безлимит' if not dimension.default_limit_gb else f'{dimension.default_limit_gb} ГБ'

    lines = [
        f'📐 <b>{_label_of(dimension)}</b>',
        '',
        f'<b>Ключ:</b> <code>{dimension.key}</code>',
        f'<b>Состояние:</b> {"✅ включено" if dimension.is_enabled else "⏸️ выключено"}',
        f'<b>Инбаундов:</b> {len(inbounds)}',
        f'<b>Лимит по умолчанию:</b> {limit_display}',
        f'<b>При исчерпании:</b> {_ENFORCEMENT_LABELS.get(dimension.enforcement, dimension.enforcement)}',
        f'<b>Учёт:</b> {_ACCOUNTING_LABELS.get(dimension.accounting_mode, "по умолчанию из настроек")}',
        f'<b>Подписок с этим измерением:</b> {users_count}',
    ]

    if dimension.is_builtin:
        lines += [
            '',
            'Это обычный трафик. Его лимиты задаются тарифом и подпиской,',
            'а ограничение выполняет сама панель RemnaWave.',
        ]
        keyboard = [[types.InlineKeyboardButton(text='⬅️ Назад', callback_data=LIST_CALLBACK)]]
        await callback.message.edit_text(
            '\n'.join(lines),
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode='HTML',
        )
        return

    if dimension.is_enabled and not inbounds:
        lines += ['', '⚠️ Инбаунды не выбраны — считать нечего, измерение фактически не работает.']

    keyboard = [
        [
            types.InlineKeyboardButton(text='🔌 Инбаунды', callback_data=f'admin_traffic_dim_inb:{dimension.id}:1'),
            types.InlineKeyboardButton(text='🛠️ Лимит', callback_data=f'admin_traffic_dim_limit:{dimension.id}'),
        ],
        [
            types.InlineKeyboardButton(text='✏️ Название', callback_data=f'admin_traffic_dim_title:{dimension.id}'),
            types.InlineKeyboardButton(text='🙂 Иконка', callback_data=f'admin_traffic_dim_icon:{dimension.id}'),
        ],
        [
            types.InlineKeyboardButton(
                text=_ENFORCEMENT_LABELS.get(dimension.enforcement, dimension.enforcement),
                callback_data=f'admin_traffic_dim_enf:{dimension.id}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text=_ACCOUNTING_LABELS.get(dimension.accounting_mode, '⚖️ Учёт: по умолчанию'),
                callback_data=f'admin_traffic_dim_acc:{dimension.id}',
            )
        ],
        [
            types.InlineKeyboardButton(
                text='⏸️ Выключить' if dimension.is_enabled else '✅ Включить',
                callback_data=f'admin_traffic_dim_toggle:{dimension.id}',
            )
        ],
        [types.InlineKeyboardButton(text='🗑️ Удалить', callback_data=f'admin_traffic_dim_del:{dimension.id}')],
        [types.InlineKeyboardButton(text='⬅️ Назад', callback_data=LIST_CALLBACK)],
    ]

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode='HTML',
    )


async def _load(callback: types.CallbackQuery, db: AsyncSession) -> TrafficDimension | None:
    try:
        dimension_id = int(callback.data.split(':')[1])
    except (IndexError, ValueError):
        await callback.answer('Некорректный запрос', show_alert=True)
        return None
    dimension = await get_traffic_dimension(db, dimension_id)
    if dimension is None:
        await callback.answer('Измерение не найдено', show_alert=True)
        return None
    return dimension


@admin_required
@error_handler
async def show_dimension(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    dimension = await _load(callback, db)
    if dimension is None:
        return
    await _render_dimension(callback, db, dimension)
    await callback.answer()


@admin_required
@error_handler
async def toggle_dimension(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    dimension = await _load(callback, db)
    if dimension is None:
        return
    if dimension.is_builtin:
        await callback.answer('Обычный трафик выключить нельзя', show_alert=True)
        return
    if not dimension.is_enabled and not (dimension.inbound_uuids or []):
        await callback.answer('Сначала выберите инбаунды', show_alert=True)
        return

    await update_traffic_dimension(db, dimension, is_enabled=not dimension.is_enabled)
    await _render_dimension(callback, db, dimension)
    await callback.answer('Включено' if dimension.is_enabled else 'Выключено')


@admin_required
@error_handler
async def cycle_enforcement(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    dimension = await _load(callback, db)
    if dimension is None or dimension.is_builtin:
        await callback.answer('Недоступно для обычного трафика', show_alert=True)
        return

    order = [TrafficDimensionEnforcement.SQUAD_STRIP.value, TrafficDimensionEnforcement.NOTIFY_ONLY.value]
    current = dimension.enforcement if dimension.enforcement in order else order[0]
    await update_traffic_dimension(db, dimension, enforcement=order[(order.index(current) + 1) % len(order)])

    await _render_dimension(callback, db, dimension)
    await callback.answer()


@admin_required
@error_handler
async def cycle_accounting(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    dimension = await _load(callback, db)
    if dimension is None or dimension.is_builtin:
        await callback.answer('Недоступно для обычного трафика', show_alert=True)
        return

    # None = «взять глобальную настройку»; поэтому цикл из трёх состояний.
    order = [None, TrafficAccountingMode.SUBQUOTA.value, TrafficAccountingMode.SHIELDED.value]
    current = dimension.accounting_mode if dimension.accounting_mode in order else None
    await update_traffic_dimension(db, dimension, accounting_mode=order[(order.index(current) + 1) % len(order)])

    await _render_dimension(callback, db, dimension)
    await callback.answer()


# ============================ Инбаунды ============================


async def _render_inbound_picker(
    callback: types.CallbackQuery,
    db: AsyncSession,
    dimension: TrafficDimension,
    page: int,
) -> None:
    inbounds = await RemnaWaveService().get_all_inbounds()
    selected = {str(uuid).lower() for uuid in (dimension.inbound_uuids or [])}

    total_count = len(inbounds)
    total_pages = max(1, math.ceil(total_count / INBOUNDS_PAGE_SIZE)) if total_count else 1
    page = min(max(1, page), total_pages)
    page_items = inbounds[(page - 1) * INBOUNDS_PAGE_SIZE : page * INBOUNDS_PAGE_SIZE]

    lines = [
        f'🔌 <b>Инбаунды: {_label_of(dimension)}</b>',
        '',
        f'Выбрано: {len(selected)}',
        'Трафик по всем выбранным инбаундам суммируется в один счётчик измерения.',
        '',
    ]
    if total_count == 0:
        lines.append('❌ Инбаунды не найдены (проверьте подключение к панели RemnaWave).')
    else:
        lines.append('Нажмите на инбаунд, чтобы включить или выключить его.')
        if total_pages > 1:
            lines.append(f'Страница {page}/{total_pages}')

    keyboard: list[list[types.InlineKeyboardButton]] = []
    for offset, inbound in enumerate(page_items):
        idx = (page - 1) * INBOUNDS_PAGE_SIZE + offset
        is_selected = str(inbound.get('uuid', '')).lower() in selected
        parts = ['✅' if is_selected else '⚪', str(inbound.get('tag', '—'))]
        inbound_type = inbound.get('type')
        port = inbound.get('port')
        if inbound_type and port:
            parts.append(f'({inbound_type}:{port})')
        elif inbound_type:
            parts.append(f'({inbound_type})')
        keyboard.append(
            [
                types.InlineKeyboardButton(
                    text=' '.join(parts),
                    callback_data=f'admin_traffic_dim_inbt:{dimension.id}:{idx}:{page}',
                )
            ]
        )

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(
                types.InlineKeyboardButton(text='⬅️', callback_data=f'admin_traffic_dim_inb:{dimension.id}:{page - 1}')
            )
        nav.append(types.InlineKeyboardButton(text=f'{page}/{total_pages}', callback_data='noop'))
        if page < total_pages:
            nav.append(
                types.InlineKeyboardButton(text='➡️', callback_data=f'admin_traffic_dim_inb:{dimension.id}:{page + 1}')
            )
        keyboard.append(nav)

    keyboard.append([types.InlineKeyboardButton(text='⬅️ Назад', callback_data=f'admin_traffic_dim:{dimension.id}')])

    await callback.message.edit_text(
        '\n'.join(lines),
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def show_inbound_picker(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    dimension = await _load(callback, db)
    if dimension is None:
        return
    parts = callback.data.split(':')
    try:
        page = max(1, int(parts[2])) if len(parts) > 2 else 1
    except ValueError:
        page = 1
    await _render_inbound_picker(callback, db, dimension, page)
    await callback.answer()


@admin_required
@error_handler
async def toggle_inbound(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    dimension = await _load(callback, db)
    if dimension is None:
        return

    parts = callback.data.split(':')
    try:
        idx = int(parts[2])
        page = max(1, int(parts[3]))
    except (IndexError, ValueError):
        await callback.answer('Некорректный запрос', show_alert=True)
        return

    inbounds = await RemnaWaveService().get_all_inbounds()
    if idx < 0 or idx >= len(inbounds):
        await callback.answer('Список инбаундов изменился, обновляю', show_alert=True)
        await _render_inbound_picker(callback, db, dimension, page)
        return

    target = str(inbounds[idx].get('uuid', '')).lower()
    selected = [str(uuid).lower() for uuid in (dimension.inbound_uuids or [])]
    if target in selected:
        selected = [uuid for uuid in selected if uuid != target]
        answer = 'Инбаунд убран'
    else:
        selected.append(target)
        answer = 'Инбаунд добавлен'

    await set_dimension_inbounds(db, dimension, selected)
    await _render_inbound_picker(callback, db, dimension, page)
    await callback.answer(answer)


# ============================ Текстовые поля ============================


async def _prompt(
    callback: types.CallbackQuery,
    state: FSMContext,
    dimension: TrafficDimension,
    prompt: str,
    fsm_state,
) -> None:
    await state.update_data(traffic_dimension_id=dimension.id)
    await state.set_state(fsm_state)
    await callback.message.edit_text(
        prompt,
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_traffic_dim:{dimension.id}')]
            ]
        ),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def start_limit_edit(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    dimension = await _load(callback, db)
    if dimension is None:
        return
    await _prompt(
        callback,
        state,
        dimension,
        f'🛠️ <b>Лимит по умолчанию: {_label_of(dimension)}</b>\n\n'
        'Введите число ГБ (0 — безлимит).\n'
        'Значение применяется к подпискам, у которых лимит этого измерения ещё не задан.',
        AdminStates.editing_traffic_dimension_limit,
    )
    await callback.answer()


@admin_required
@error_handler
async def start_title_edit(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    dimension = await _load(callback, db)
    if dimension is None:
        return
    await _prompt(
        callback,
        state,
        dimension,
        f'✏️ <b>Название: {_label_of(dimension)}</b>\n\nВведите новое название измерения.',
        AdminStates.editing_traffic_dimension_title,
    )
    await callback.answer()


@admin_required
@error_handler
async def start_icon_edit(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    dimension = await _load(callback, db)
    if dimension is None:
        return
    await _prompt(
        callback,
        state,
        dimension,
        f'🙂 <b>Иконка: {_label_of(dimension)}</b>\n\n'
        'Отправьте один эмодзи. Отправьте <code>-</code>, чтобы убрать иконку.',
        AdminStates.editing_traffic_dimension_icon,
    )
    await callback.answer()


async def _resolve_from_state(state: FSMContext, db: AsyncSession) -> TrafficDimension | None:
    data = await state.get_data()
    dimension_id = data.get('traffic_dimension_id')
    if not dimension_id:
        return None
    return await get_traffic_dimension(db, dimension_id)


def _back_keyboard(dimension_id: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text='📐 К измерению', callback_data=f'admin_traffic_dim:{dimension_id}')]
        ]
    )


@admin_required
@error_handler
async def process_limit_edit(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    dimension = await _resolve_from_state(state, db)
    if dimension is None:
        await message.answer('❌ Измерение не найдено')
        await state.clear()
        return

    try:
        limit_gb = int((message.text or '').strip())
    except ValueError:
        await message.answer('❌ Введите целое число ГБ')
        return
    if limit_gb < 0 or limit_gb > 1_000_000:
        await message.answer('❌ Лимит должен быть от 0 до 1000000 ГБ (0 = безлимит)')
        return

    await update_traffic_dimension(db, dimension, default_limit_gb=limit_gb)
    await state.clear()
    display = '♾️ безлимит' if limit_gb == 0 else f'{limit_gb} ГБ'
    await message.answer(f'✅ Лимит по умолчанию: {display}', reply_markup=_back_keyboard(dimension.id))


@admin_required
@error_handler
async def process_title_edit(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    dimension = await _resolve_from_state(state, db)
    if dimension is None:
        await message.answer('❌ Измерение не найдено')
        await state.clear()
        return

    title = (message.text or '').strip()
    if not title or len(title) > 64:
        await message.answer('❌ Название должно быть от 1 до 64 символов')
        return

    titles = dict(dimension.title or {})
    titles['ru'] = title
    await update_traffic_dimension(db, dimension, title=titles, fallback_title=title)
    await state.clear()
    await message.answer(f'✅ Название: {title}', reply_markup=_back_keyboard(dimension.id))


@admin_required
@error_handler
async def process_icon_edit(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    dimension = await _resolve_from_state(state, db)
    if dimension is None:
        await message.answer('❌ Измерение не найдено')
        await state.clear()
        return

    icon = (message.text or '').strip()
    if icon == '-':
        icon = ''
    elif len(icon) > 8:
        await message.answer('❌ Отправьте один эмодзи (или «-», чтобы убрать)')
        return

    await update_traffic_dimension(db, dimension, icon=icon)
    await state.clear()
    await message.answer(f'✅ Иконка обновлена: {icon or "—"}', reply_markup=_back_keyboard(dimension.id))


# ============================ Создание и удаление ============================


@admin_required
@error_handler
async def start_create(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(AdminStates.creating_traffic_dimension)
    await callback.message.edit_text(
        '➕ <b>Новое измерение трафика</b>\n\n'
        'Отправьте ключ и название через пробел, например:\n'
        '<code>wl WL Трафик (БС)</code>\n\n'
        'Ключ — латиница в нижнем регистре, цифры и подчёркивания. Он неизменяем: '
        'на него ссылаются записи о докупках трафика.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[[types.InlineKeyboardButton(text='❌ Отмена', callback_data=LIST_CALLBACK)]]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_create(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    raw = (message.text or '').strip()
    key, _, title = raw.partition(' ')
    title = title.strip()
    if not key or not title:
        await message.answer('❌ Нужны и ключ, и название: <code>wl WL Трафик</code>', parse_mode='HTML')
        return

    try:
        dimension = await create_traffic_dimension(db, key=key, title=title)
    except TrafficDimensionKeyError as error:
        await message.answer(f'❌ {error}')
        return

    await state.clear()
    await message.answer(
        f'✅ Измерение «{title}» создано.\n\nВыберите инбаунды и включите его.',
        reply_markup=_back_keyboard(dimension.id),
    )


@admin_required
@error_handler
async def confirm_delete(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    dimension = await _load(callback, db)
    if dimension is None:
        return
    if dimension.is_builtin:
        await callback.answer('Обычный трафик удалить нельзя', show_alert=True)
        return

    users_count = await count_dimension_subscriptions(db, dimension.id)
    await callback.message.edit_text(
        f'🗑️ <b>Удалить измерение «{_label_of(dimension)}»?</b>\n\n'
        f'Будет удалено состояние у {users_count} подписок и настройки этого измерения в тарифах.\n'
        'Купленный по нему трафик перестанет учитываться. Действие необратимо.',
        reply_markup=types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    types.InlineKeyboardButton(
                        text='🗑️ Удалить', callback_data=f'admin_traffic_dim_delok:{dimension.id}'
                    ),
                    types.InlineKeyboardButton(text='❌ Отмена', callback_data=f'admin_traffic_dim:{dimension.id}'),
                ]
            ]
        ),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def do_delete(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    dimension = await _load(callback, db)
    if dimension is None:
        return

    label = _label_of(dimension)
    if not await delete_traffic_dimension(db, dimension):
        await callback.answer('Это измерение удалить нельзя', show_alert=True)
        return

    logger.info('Админ удалил измерение трафика', admin_id=db_user.id, dimension=label)
    await _render_list(callback, db)
    await callback.answer(f'Удалено: {label}')


def register_handlers(dp: Dispatcher) -> None:
    # Порядок важен: `admin_traffic_dim:` — префикс всех остальных колбэков,
    # поэтому список и карточка регистрируются точным сравнением/двоеточием,
    # а более длинные префиксы объявлены до них.
    dp.callback_query.register(show_dimensions, F.data == LIST_CALLBACK)
    dp.callback_query.register(start_create, F.data == 'admin_traffic_dim_new')
    dp.callback_query.register(show_inbound_picker, F.data.startswith('admin_traffic_dim_inb:'))
    dp.callback_query.register(toggle_inbound, F.data.startswith('admin_traffic_dim_inbt:'))
    dp.callback_query.register(start_limit_edit, F.data.startswith('admin_traffic_dim_limit:'))
    dp.callback_query.register(start_title_edit, F.data.startswith('admin_traffic_dim_title:'))
    dp.callback_query.register(start_icon_edit, F.data.startswith('admin_traffic_dim_icon:'))
    dp.callback_query.register(cycle_enforcement, F.data.startswith('admin_traffic_dim_enf:'))
    dp.callback_query.register(cycle_accounting, F.data.startswith('admin_traffic_dim_acc:'))
    dp.callback_query.register(toggle_dimension, F.data.startswith('admin_traffic_dim_toggle:'))
    dp.callback_query.register(do_delete, F.data.startswith('admin_traffic_dim_delok:'))
    dp.callback_query.register(confirm_delete, F.data.startswith('admin_traffic_dim_del:'))
    dp.callback_query.register(show_dimension, F.data.startswith('admin_traffic_dim:'))

    dp.message.register(process_create, AdminStates.creating_traffic_dimension)
    dp.message.register(process_limit_edit, AdminStates.editing_traffic_dimension_limit)
    dp.message.register(process_title_edit, AdminStates.editing_traffic_dimension_title)
    dp.message.register(process_icon_edit, AdminStates.editing_traffic_dimension_icon)
