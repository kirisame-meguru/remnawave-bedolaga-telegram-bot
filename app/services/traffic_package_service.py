"""Начисление купленного пакета трафика.

Пакет может выдавать обычный трафик, трафик измерения или и то, и другое. Здесь
он раскладывается на отдельные начисления, каждое из которых уходит в свой
учёт: обычный трафик — в старую механику (`add_subscription_traffic`, колонки
подписки), измерения — в строки `subscription_traffic_dimensions`.

Разделение принципиальное. Инвариант обычного трафика (`traffic_limit_gb =
база + докупки`) пересобирается housekeeping'ом, который считает докупки по
таблице; если бы туда попадали пакеты измерений, истёкший WL-пакет ронял бы
обычный лимит подписки. Поэтому `TrafficPurchase.dimension` — не украшение, а
разделитель двух независимых счётов в одной таблице.
"""

from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Subscription
from app.services.traffic_packages import TrafficGrant, TrafficPackage, split_price


logger = structlog.get_logger(__name__)


@dataclass
class AppliedGrant:
    """Одно начисление пакета, доведённое до учёта."""

    grant: TrafficGrant
    price_share_kopeks: int
    applied: bool
    skipped_reason: str | None = None


@dataclass
class PackageApplication:
    """Итог начисления пакета целиком."""

    package: TrafficPackage
    grants: list[AppliedGrant] = field(default_factory=list)

    @property
    def applied_any(self) -> bool:
        return any(item.applied for item in self.grants)

    @property
    def base_gb(self) -> int:
        return sum(item.grant.gb for item in self.grants if item.applied and item.grant.is_base)

    @property
    def dimension_gb(self) -> dict[str, int]:
        return {item.grant.dimension: item.grant.gb for item in self.grants if item.applied and not item.grant.is_base}

    def describe(self) -> str:
        """Короткое описание для транзакции и логов."""
        parts = [f'{item.grant.dimension}:{item.grant.gb}ГБ' for item in self.grants if item.applied]
        return ', '.join(parts) if parts else 'ничего не начислено'


async def apply_traffic_package(
    db: AsyncSession,
    subscription: Subscription,
    package: TrafficPackage,
    *,
    price_kopeks: int | None = None,
) -> PackageApplication:
    """Начисляет пакет подписке. Не коммитит — транзакцией владеет вызывающий.

    Цена разносится по начислениям пропорционально гигабайтам: сумма долей
    равна цене пакета до копейки, иначе отчёты по измерениям не сойдутся с
    суммой списания.

    Начисление по измерению, которого нет в реестре или которое выключено,
    пропускается с явной причиной, а не падает: пакет мог пережить удаление
    измерения администратором, и остальные его начисления по-прежнему честные.
    """
    from app.services.traffic_dimensions import grant_dimension_traffic, traffic_dimensions

    total_price = package.price_kopeks if price_kopeks is None else int(price_kopeks)
    shares = split_price(total_price, package.grants)
    result = PackageApplication(package=package)

    specs = {spec.key: spec for spec in await traffic_dimensions.enabled(db)}

    for grant, share in zip(package.grants, shares, strict=True):
        if grant.is_base:
            await _apply_base_grant(db, subscription, grant)
            result.grants.append(AppliedGrant(grant=grant, price_share_kopeks=share, applied=True))
            continue

        spec = specs.get(grant.dimension)
        if spec is None:
            logger.warning(
                'Пакет ссылается на неизвестное или выключенное измерение',
                package_id=package.id,
                dimension=grant.dimension,
            )
            result.grants.append(
                AppliedGrant(
                    grant=grant,
                    price_share_kopeks=share,
                    applied=False,
                    skipped_reason='unknown_dimension',
                )
            )
            continue

        await grant_dimension_traffic(db, subscription, spec, grant.gb, days=package.validity_days)
        result.grants.append(AppliedGrant(grant=grant, price_share_kopeks=share, applied=True))

    logger.info(
        '📦 Начислен пакет трафика',
        package_id=package.id,
        subscription_id=subscription.id,
        granted=result.describe(),
        price_kopeks=total_price,
    )
    return result


async def _apply_base_grant(db: AsyncSession, subscription: Subscription, grant: TrafficGrant) -> None:
    """Обычный трафик идёт по старому пути — он же владеет инвариантом лимита."""
    from sqlalchemy import delete

    from app.database.crud.subscription import add_subscription_traffic
    from app.database.models import BASE_TRAFFIC_DIMENSION_KEY, TrafficPurchase

    if grant.is_unlimited:
        # Переход на безлимит обнуляет докупки обычного трафика: складывать их
        # с безлимитом бессмысленно. Пакеты измерений при этом не трогаем —
        # их квоты живут отдельно и остаются в силе.
        subscription.traffic_limit_gb = 0
        await db.execute(
            delete(TrafficPurchase)
            .where(
                TrafficPurchase.subscription_id == subscription.id,
                TrafficPurchase.dimension == BASE_TRAFFIC_DIMENSION_KEY,
            )
            .execution_options(synchronize_session='fetch')
        )
        subscription.purchased_traffic_gb = 0
        subscription.traffic_reset_at = None
        return

    await add_subscription_traffic(db, subscription, grant.gb)
