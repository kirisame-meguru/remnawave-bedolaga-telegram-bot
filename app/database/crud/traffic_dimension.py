"""CRUD по измерениям трафика.

Измерения заводит администратор, поэтому запись всегда идёт через эти функции:
они сбрасывают кэш реестра, иначе изменение доехало бы до бота только через
минуту (TTL) и админ решил бы, что кнопка не работает.
"""

import re

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    BASE_TRAFFIC_DIMENSION_KEY,
    SubscriptionTrafficDimension,
    TariffTrafficDimension,
    TrafficDimension,
    TrafficDimensionEnforcement,
)
from app.services.traffic_dimensions import traffic_dimensions


logger = structlog.get_logger(__name__)

KEY_PATTERN = re.compile(r'^[a-z][a-z0-9_]{1,31}$')


class TrafficDimensionKeyError(ValueError):
    """Ключ измерения не подходит: занят, зарезервирован или не того формата."""


async def list_traffic_dimensions(db: AsyncSession) -> list[TrafficDimension]:
    result = await db.execute(select(TrafficDimension).order_by(TrafficDimension.position, TrafficDimension.id))
    return list(result.scalars().all())


async def get_traffic_dimension(db: AsyncSession, dimension_id: int) -> TrafficDimension | None:
    result = await db.execute(select(TrafficDimension).where(TrafficDimension.id == dimension_id))
    return result.scalar_one_or_none()


async def get_traffic_dimension_by_key(db: AsyncSession, key: str) -> TrafficDimension | None:
    result = await db.execute(select(TrafficDimension).where(TrafficDimension.key == key))
    return result.scalar_one_or_none()


async def create_traffic_dimension(
    db: AsyncSession,
    *,
    key: str,
    title: str,
    icon: str = '',
    inbound_uuids: list[str] | None = None,
    default_limit_gb: int = 0,
) -> TrafficDimension:
    """Заводит измерение. Ключ неизменяем: на него ссылается леджер докупок."""
    key = (key or '').strip().lower()
    if not KEY_PATTERN.match(key):
        raise TrafficDimensionKeyError(
            'Ключ: латиница в нижнем регистре, цифры и подчёркивания, 2–32 символа, начиная с буквы.'
        )
    if key == BASE_TRAFFIC_DIMENSION_KEY:
        raise TrafficDimensionKeyError(f'Ключ «{BASE_TRAFFIC_DIMENSION_KEY}» занят обычным трафиком.')
    if await get_traffic_dimension_by_key(db, key) is not None:
        raise TrafficDimensionKeyError(f'Измерение с ключом «{key}» уже существует.')

    max_position = (await db.execute(select(func.max(TrafficDimension.position)))).scalar() or 0

    dimension = TrafficDimension(
        key=key,
        title={'ru': title},
        fallback_title=title,
        icon=icon,
        inbound_uuids=[str(uuid).strip().lower() for uuid in (inbound_uuids or []) if str(uuid).strip()],
        default_limit_gb=max(0, int(default_limit_gb or 0)),
        enforcement=TrafficDimensionEnforcement.SQUAD_STRIP.value,
        # Новое измерение включается вручную: пока не выбраны инбаунды, считать нечего.
        is_enabled=False,
        is_builtin=False,
        position=max_position + 1,
    )
    db.add(dimension)
    await db.commit()
    await db.refresh(dimension)
    traffic_dimensions.invalidate()

    logger.info('Создано измерение трафика', key=key, dimension_id=dimension.id)
    return dimension


async def update_traffic_dimension(db: AsyncSession, dimension: TrafficDimension, **fields) -> TrafficDimension:
    """Точечно правит поля измерения. `key` менять нельзя."""
    fields.pop('key', None)
    for name, value in fields.items():
        setattr(dimension, name, value)
    await db.commit()
    await db.refresh(dimension)
    traffic_dimensions.invalidate()
    return dimension


async def set_dimension_inbounds(
    db: AsyncSession, dimension: TrafficDimension, inbound_uuids: list[str]
) -> TrafficDimension:
    normalized = [str(uuid).strip().lower() for uuid in inbound_uuids if str(uuid).strip()]
    return await update_traffic_dimension(db, dimension, inbound_uuids=normalized)


async def delete_traffic_dimension(db: AsyncSession, dimension: TrafficDimension) -> bool:
    """Удаляет измерение вместе с его состоянием у подписок и настройками тарифов.

    Обычный трафик удалить нельзя: его состояние живёт в колонках подписки, и
    строка `base` — единственное, что связывает его с реестром.
    """
    if dimension.is_builtin or dimension.key == BASE_TRAFFIC_DIMENSION_KEY:
        return False

    # FK стоят на CASCADE, но чистим явно: удаление идёт из админки, и молчаливая
    # зависимость от настроек БД здесь — лишний риск.
    await db.execute(
        delete(SubscriptionTrafficDimension).where(SubscriptionTrafficDimension.dimension_id == dimension.id)
    )
    await db.execute(delete(TariffTrafficDimension).where(TariffTrafficDimension.dimension_id == dimension.id))
    await db.delete(dimension)
    await db.commit()
    traffic_dimensions.invalidate()

    logger.info('Удалено измерение трафика', key=dimension.key, dimension_id=dimension.id)
    return True


async def count_dimension_subscriptions(db: AsyncSession, dimension_id: int) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(SubscriptionTrafficDimension)
        .where(SubscriptionTrafficDimension.dimension_id == dimension_id)
    )
    return int(result.scalar() or 0)
