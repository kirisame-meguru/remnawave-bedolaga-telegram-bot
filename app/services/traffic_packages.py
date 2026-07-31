"""Пакеты трафика, выдающие несколько измерений сразу.

До появления измерений пакет был парой «ГБ → цена», и этого хватало: измерение
было одно. Теперь администратору нужно продавать три разные вещи — обычный
трафик, трафик измерения и их смесь, — а цена у смеси одна на весь пакет.
Поэтому пакет описывается идентификатором, ценой и списком начислений.

Обратная совместимость важнее красоты: старые пакеты (`{"100": 9900}`) читаются
как есть, а новые дескрипторы, состоящие из одного обычного начисления,
проецируются обратно в тот же словарь. Полдюжины мест — клавиатуры, кабинет,
мини-апп, автопокупка — продолжают работать, ничего не зная про измерения.

Разбиение цены по начислениям нужно ровно для одного: `TrafficPurchase` пишется
по строке на измерение, а сумма долей обязана совпадать с ценой пакета до
копейки. Остаток от деления отдаётся первому начислению — тогда сумма сходится
всегда, без «плавающей» копейки в отчётах.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

import structlog

from app.database.models import BASE_TRAFFIC_DIMENSION_KEY


logger = structlog.get_logger(__name__)

# Синтетический id для пакетов, пришедших из старого формата `{ГБ: цена}`.
LEGACY_ID_PREFIX = 'gb'


def legacy_package_id(gb: int) -> str:
    """`add_traffic_100` и дескриптор `gb100` — один и тот же пакет."""
    return f'{LEGACY_ID_PREFIX}{int(gb)}'


@dataclass(frozen=True)
class TrafficGrant:
    """Сколько и какому измерению начисляет пакет."""

    dimension: str
    gb: int

    @property
    def is_base(self) -> bool:
        return self.dimension == BASE_TRAFFIC_DIMENSION_KEY

    @property
    def is_unlimited(self) -> bool:
        """0 ГБ у обычного трафика исторически означает «безлимит»."""
        return self.gb == 0


@dataclass(frozen=True)
class TrafficPackage:
    """Один покупаемый пакет."""

    id: str
    price_kopeks: int
    grants: tuple[TrafficGrant, ...]
    title: str = ''
    validity_days: int = 30
    enabled: bool = True

    @property
    def total_gb(self) -> int:
        return sum(grant.gb for grant in self.grants)

    @property
    def dimensions(self) -> tuple[str, ...]:
        return tuple(grant.dimension for grant in self.grants)

    @property
    def is_legacy_base(self) -> bool:
        """Пакет, полностью описываемый старой парой «ГБ → цена»."""
        return len(self.grants) == 1 and self.grants[0].is_base

    def grant_for(self, dimension: str) -> TrafficGrant | None:
        for grant in self.grants:
            if grant.dimension == dimension:
                return grant
        return None


def parse_packages(raw) -> tuple[TrafficPackage, ...]:
    """Читает и старый, и новый формат.

    Старый — словарь `{"100": 9900}`. Новый — список дескрипторов. Различаются
    по типу, а не по флагу: флаг рано или поздно разъедется с данными.
    """
    if not raw:
        return ()
    if isinstance(raw, Mapping):
        return _parse_legacy_mapping(raw)
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        return _parse_descriptors(raw)
    logger.warning('Неизвестный формат пакетов трафика', type=type(raw).__name__)
    return ()


def _parse_legacy_mapping(raw: Mapping) -> tuple[TrafficPackage, ...]:
    packages = []
    for key, value in raw.items():
        try:
            gb = int(key)
            price = int(value)
        except (TypeError, ValueError):
            continue
        packages.append(
            TrafficPackage(
                id=legacy_package_id(gb),
                price_kopeks=price,
                grants=(TrafficGrant(BASE_TRAFFIC_DIMENSION_KEY, gb),),
            )
        )
    return tuple(sorted(packages, key=lambda p: p.total_gb))


def _parse_descriptors(raw: Sequence) -> tuple[TrafficPackage, ...]:
    packages = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        package = _parse_descriptor(entry)
        if package is None or package.id in seen:
            continue
        seen.add(package.id)
        packages.append(package)
    return tuple(packages)


def _parse_descriptor(entry: Mapping) -> TrafficPackage | None:
    grants: list[TrafficGrant] = []
    for raw_grant in entry.get('grants') or []:
        if not isinstance(raw_grant, Mapping):
            continue
        dimension = str(raw_grant.get('dim') or raw_grant.get('dimension') or '').strip().lower()
        if not dimension:
            continue
        try:
            gb = int(raw_grant.get('gb') or 0)
        except (TypeError, ValueError):
            continue
        if gb < 0:
            continue
        grants.append(TrafficGrant(dimension, gb))

    if not grants:
        # Пакет без начислений ничего не продаёт — молча его не показываем.
        return None

    package_id = str(entry.get('id') or '').strip()
    if not package_id:
        # id нужен для callback'а и корзины: без него пакет не купить.
        return None

    try:
        price = int(entry.get('price_kopeks') or entry.get('price') or 0)
    except (TypeError, ValueError):
        return None
    try:
        validity_days = int(entry.get('validity_days') or 30)
    except (TypeError, ValueError):
        validity_days = 30

    return TrafficPackage(
        id=package_id,
        price_kopeks=max(price, 0),
        grants=tuple(grants),
        title=str(entry.get('title') or ''),
        validity_days=max(validity_days, 1),
        enabled=bool(entry.get('enabled', True)),
    )


def legacy_projection(packages: Iterable[TrafficPackage]) -> dict[int, int]:
    """Старый вид `{ГБ: цена}` для кода, который про измерения не знает.

    Пакеты со смешанными или неосновными начислениями сюда не попадают: для
    старых читателей их просто не существует, и это правильнее, чем показать
    цену смеси как цену обычного трафика.
    """
    return {
        package.grants[0].gb: package.price_kopeks for package in packages if package.enabled and package.is_legacy_base
    }


def package_by_id(packages: Iterable[TrafficPackage], package_id: str) -> TrafficPackage | None:
    for package in packages:
        if package.id == package_id:
            return package
    return None


def split_price(price_kopeks: int, grants: Sequence[TrafficGrant]) -> list[int]:
    """Разносит цену пакета по начислениям пропорционально гигабайтам.

    Сумма долей всегда равна цене пакета: остаток от деления уходит первому
    начислению. Иначе отчёты по измерениям не сошлись бы с суммой транзакции —
    на копейку, зато навсегда.

    Пакет из одних безлимитных/нулевых начислений делится поровну: пропорции
    по гигабайтам там не существует.
    """
    count = len(grants)
    if count == 0:
        return []
    if count == 1:
        return [int(price_kopeks)]

    total_gb = sum(grant.gb for grant in grants)
    if total_gb <= 0:
        shares = [price_kopeks // count] * count
    else:
        shares = [price_kopeks * grant.gb // total_gb for grant in grants]

    shares[0] += price_kopeks - sum(shares)
    return shares


# ============================== Текстовый формат админки ==============================

# Администратор задаёт пакеты строкой. Формат расширяет прежний, а не заменяет:
# «5:5000» как значило «5 ГБ обычного трафика за 50 ₽», так и значит. Начисление
# по измерению пишется как «ключ=ГБ», несколько начислений — через «+».
#
#   5:5000              → обычный трафик, 5 ГБ
#   wl=10:9900          → только WL, 10 ГБ
#   5+wl=3:12000        → смесь: 5 ГБ обычного и 3 ГБ WL за одну цену
_SPEC_SEPARATORS = str.maketrans({';': ',', '\n': ','})


def _grant_id_part(grant: TrafficGrant) -> str:
    return f'{LEGACY_ID_PREFIX}{grant.gb}' if grant.is_base else f'{grant.dimension}{grant.gb}'


def build_package_id(grants: Sequence[TrafficGrant]) -> str:
    """Устойчивый id из состава начислений.

    Детерминированный, а не случайный: id уходит в callback и в корзину, и
    после правки списка пакетов он должен остаться прежним у тех пакетов,
    состав которых не менялся.
    """
    return '-'.join(_grant_id_part(grant) for grant in grants)


def parse_package_spec(text: str) -> list[dict]:
    """Разбирает админскую строку в дескрипторы пакетов.

    Молча пропускает нераспознанное — вызывающий сравнивает длину результата с
    ожиданием и сам решает, ругаться ли. Отдаёт готовый к записи JSON, а не
    объекты: в БД лежит именно он.
    """
    descriptors: list[dict] = []
    seen: set[str] = set()

    for chunk in (text or '').translate(_SPEC_SEPARATORS).split(','):
        part = chunk.strip()
        if not part or ':' not in part:
            continue
        grants_text, _, price_text = part.rpartition(':')
        try:
            price = int(price_text.strip())
        except ValueError:
            continue
        if price <= 0:
            continue

        grants = _parse_grant_spec(grants_text)
        if not grants:
            continue

        package_id = build_package_id(grants)
        if package_id in seen:
            continue
        seen.add(package_id)
        descriptors.append(
            {
                'id': package_id,
                'price_kopeks': price,
                'grants': [{'dim': grant.dimension, 'gb': grant.gb} for grant in grants],
            }
        )

    return descriptors


def _parse_grant_spec(text: str) -> list[TrafficGrant]:
    grants: list[TrafficGrant] = []
    for raw in text.split('+'):
        token = raw.strip()
        if not token:
            continue
        if '=' in token:
            dimension, _, gb_text = token.partition('=')
            dimension = dimension.strip().lower()
        else:
            dimension, gb_text = BASE_TRAFFIC_DIMENSION_KEY, token
        try:
            gb = int(gb_text.strip())
        except ValueError:
            return []
        if gb < 0 or not dimension:
            return []
        grants.append(TrafficGrant(dimension, gb))
    return grants


def format_package_spec(packages: Iterable[TrafficPackage]) -> str:
    """Обратная операция: пакеты → строка, которую можно отредактировать."""
    parts = []
    for package in packages:
        grants = '+'.join(
            str(grant.gb) if grant.is_base else f'{grant.dimension}={grant.gb}' for grant in package.grants
        )
        parts.append(f'{grants}:{package.price_kopeks}')
    return ', '.join(parts)


def describe_package(package: TrafficPackage, labels: Mapping[str, str] | None = None) -> str:
    """Человекочитаемый состав пакета для админских экранов."""
    labels = labels or {}
    parts = []
    for grant in package.grants:
        label = labels.get(grant.dimension, grant.dimension)
        if grant.is_base:
            parts.append('♾️ безлимит' if grant.is_unlimited else f'{grant.gb} ГБ')
        else:
            parts.append(f'{grant.gb} ГБ {label}')
    return ' + '.join(parts)
