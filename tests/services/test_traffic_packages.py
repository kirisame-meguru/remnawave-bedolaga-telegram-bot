"""Пакеты трафика: разбор обоих форматов, проекция в старый вид, разбиение цены.

Два свойства держат совместимость: старый формат читается без изменений, а
новые пакеты из одного обычного начисления выглядят для старого кода ровно как
раньше. Третье свойство — арифметическое: сумма долей равна цене пакета.
"""

import pytest

from app.services.traffic_packages import (
    TrafficGrant,
    TrafficPackage,
    legacy_package_id,
    legacy_projection,
    package_by_id,
    parse_packages,
    split_price,
)


# ------------------------------ разбор ------------------------------


def test_legacy_mapping_is_read_unchanged():
    """Тарифы, настроенные до появления измерений, обязаны работать как были."""
    packages = parse_packages({'50': 4900, '100': 8900})

    assert [p.total_gb for p in packages] == [50, 100]
    assert [p.price_kopeks for p in packages] == [4900, 8900]
    assert all(p.is_legacy_base for p in packages)
    assert all(p.grants[0].dimension == 'base' for p in packages)


def test_legacy_mapping_skips_garbage_entries():
    assert parse_packages({'сто': 4900, '50': 'дорого', '10': 990})[0].total_gb == 10


def test_descriptor_list_is_parsed():
    packages = parse_packages(
        [
            {
                'id': 'mix10',
                'title': '10 + 5 WL',
                'price_kopeks': 14900,
                'grants': [{'dim': 'base', 'gb': 10}, {'dim': 'wl', 'gb': 5}],
            }
        ]
    )

    assert len(packages) == 1
    package = packages[0]
    assert package.id == 'mix10'
    assert package.total_gb == 15
    assert package.dimensions == ('base', 'wl')
    assert not package.is_legacy_base


def test_wl_only_package_is_valid():
    """Один из трёх заявленных видов пакета — только трафик измерения."""
    packages = parse_packages([{'id': 'wl10', 'price_kopeks': 9900, 'grants': [{'dim': 'wl', 'gb': 10}]}])

    assert packages[0].dimensions == ('wl',)
    assert not packages[0].is_legacy_base


@pytest.mark.parametrize(
    'entry',
    [
        {'id': 'no-grants', 'price_kopeks': 100, 'grants': []},
        {'price_kopeks': 100, 'grants': [{'dim': 'wl', 'gb': 5}]},
        {'id': '', 'price_kopeks': 100, 'grants': [{'dim': 'wl', 'gb': 5}]},
        {'id': 'bad-price', 'price_kopeks': 'дорого', 'grants': [{'dim': 'wl', 'gb': 5}]},
    ],
)
def test_unusable_descriptors_are_dropped(entry):
    """Пакет, который нельзя купить, не должен доезжать до кнопки."""
    assert parse_packages([entry]) == ()


def test_duplicate_ids_keep_the_first():
    packages = parse_packages(
        [
            {'id': 'x', 'price_kopeks': 100, 'grants': [{'dim': 'wl', 'gb': 1}]},
            {'id': 'x', 'price_kopeks': 999, 'grants': [{'dim': 'wl', 'gb': 9}]},
        ]
    )
    assert len(packages) == 1
    assert packages[0].price_kopeks == 100


def test_grant_dimension_is_normalised():
    packages = parse_packages([{'id': 'x', 'price_kopeks': 100, 'grants': [{'dim': '  WL  ', 'gb': 5}]}])
    assert packages[0].dimensions == ('wl',)


def test_empty_input_is_empty_output():
    assert parse_packages(None) == ()
    assert parse_packages({}) == ()
    assert parse_packages([]) == ()


# ------------------------------ совместимость ------------------------------


def test_projection_keeps_old_readers_working():
    """Клавиатуры, кабинет и автопокупка читают именно этот словарь."""
    packages = parse_packages(
        [
            {'id': 'gb100', 'price_kopeks': 8900, 'grants': [{'dim': 'base', 'gb': 100}]},
            {'id': 'wl10', 'price_kopeks': 9900, 'grants': [{'dim': 'wl', 'gb': 10}]},
            {
                'id': 'mix',
                'price_kopeks': 14900,
                'grants': [{'dim': 'base', 'gb': 10}, {'dim': 'wl', 'gb': 5}],
            },
        ]
    )

    assert legacy_projection(packages) == {100: 8900}, 'смешанные и WL-пакеты старому коду не видны'


def test_projection_skips_disabled_packages():
    packages = parse_packages(
        [{'id': 'gb100', 'price_kopeks': 8900, 'enabled': False, 'grants': [{'dim': 'base', 'gb': 100}]}]
    )
    assert legacy_projection(packages) == {}


def test_legacy_round_trip():
    """Старый словарь → пакеты → тот же словарь."""
    raw = {'50': 4900, '100': 8900}
    assert legacy_projection(parse_packages(raw)) == {50: 4900, 100: 8900}


def test_legacy_callback_id_matches_the_synthesised_package():
    packages = parse_packages({'100': 8900})
    assert package_by_id(packages, legacy_package_id(100)) is packages[0]


def test_package_by_id_returns_none_for_unknown():
    assert package_by_id(parse_packages({'100': 8900}), 'nope') is None


# ------------------------------ разбиение цены ------------------------------


def test_single_grant_takes_the_whole_price():
    assert split_price(9900, [TrafficGrant('wl', 10)]) == [9900]


def test_price_is_split_by_gigabytes():
    shares = split_price(15000, [TrafficGrant('base', 10), TrafficGrant('wl', 5)])
    assert shares == [10000, 5000]


@pytest.mark.parametrize(
    'price',
    [1, 7, 99, 100, 9901, 14999, 123457],
)
def test_shares_always_sum_to_the_package_price(price):
    """Иначе отчёты по измерениям расходятся с суммой транзакции."""
    grants = [TrafficGrant('base', 7), TrafficGrant('wl', 3), TrafficGrant('torrent', 11)]
    shares = split_price(price, grants)

    assert sum(shares) == price
    assert len(shares) == len(grants)


def test_remainder_goes_to_the_first_grant():
    shares = split_price(100, [TrafficGrant('base', 1), TrafficGrant('wl', 1), TrafficGrant('x', 1)])
    assert shares == [34, 33, 33]
    assert sum(shares) == 100


def test_zero_gb_grants_split_evenly():
    """Безлимитные начисления не имеют пропорции — делим поровну."""
    shares = split_price(100, [TrafficGrant('base', 0), TrafficGrant('wl', 0)])
    assert shares == [50, 50]
    assert sum(shares) == 100


def test_no_grants_is_no_shares():
    assert split_price(100, []) == []


def test_free_package_splits_to_zeros():
    assert split_price(0, [TrafficGrant('base', 10), TrafficGrant('wl', 5)]) == [0, 0]


# ------------------------------ свойства пакета ------------------------------


def test_unlimited_base_grant_is_recognised():
    package = TrafficPackage(id='unl', price_kopeks=50000, grants=(TrafficGrant('base', 0),))
    assert package.grants[0].is_unlimited
    assert package.is_legacy_base


def test_grant_lookup_by_dimension():
    package = parse_packages(
        [{'id': 'mix', 'price_kopeks': 100, 'grants': [{'dim': 'base', 'gb': 10}, {'dim': 'wl', 'gb': 5}]}]
    )[0]

    assert package.grant_for('wl').gb == 5
    assert package.grant_for('torrent') is None


# ------------------------------ админский формат ------------------------------


def test_legacy_admin_syntax_is_unchanged():
    """Строки, введённые до появления измерений, обязаны значить то же самое."""
    from app.services.traffic_packages import parse_package_spec

    descriptors = parse_package_spec('5:5000, 10:9000, 20:15000')
    packages = parse_packages(descriptors)

    assert legacy_projection(packages) == {5: 5000, 10: 9000, 20: 15000}
    assert all(p.is_legacy_base for p in packages)


def test_dimension_only_syntax():
    from app.services.traffic_packages import parse_package_spec

    packages = parse_packages(parse_package_spec('wl=10:9900'))

    assert len(packages) == 1
    assert packages[0].dimensions == ('wl',)
    assert packages[0].grants[0].gb == 10
    assert packages[0].price_kopeks == 9900


def test_mixed_syntax():
    from app.services.traffic_packages import parse_package_spec

    packages = parse_packages(parse_package_spec('5+wl=3:12000'))

    assert packages[0].dimensions == ('base', 'wl')
    assert [g.gb for g in packages[0].grants] == [5, 3]
    assert packages[0].price_kopeks == 12000


def test_all_three_shapes_in_one_line():
    """Ровно те три вида пакета, ради которых всё затевалось."""
    from app.services.traffic_packages import parse_package_spec

    packages = parse_packages(parse_package_spec('10:8900, wl=5:4900, 10+wl=5:12900'))

    assert [p.dimensions for p in packages] == [('base',), ('wl',), ('base', 'wl')]


def test_ids_are_stable_across_edits():
    """id уходит в callback и корзину: пересохранение не должно его менять."""
    from app.services.traffic_packages import parse_package_spec

    first = parse_package_spec('10:8900, wl=5:4900')
    second = parse_package_spec('wl=5:5900, 10:8900')  # порядок и цена другие

    assert {d['id'] for d in first} == {d['id'] for d in second}


def test_spec_round_trip():
    from app.services.traffic_packages import format_package_spec, parse_package_spec

    text = '10:8900, wl=5:4900, 10+wl=5:12900'
    packages = parse_packages(parse_package_spec(text))

    assert format_package_spec(packages) == text


@pytest.mark.parametrize(
    'text',
    ['', 'мусор', '10', 'wl=:100', '10:', '10:abc', '10:0', '10:-5', 'wl=abc:100'],
)
def test_unparseable_specs_yield_nothing(text):
    from app.services.traffic_packages import parse_package_spec

    assert parse_package_spec(text) == []


def test_separators_are_forgiving():
    from app.services.traffic_packages import parse_package_spec

    assert len(parse_package_spec('10:100; wl=5:200\n20:300')) == 3


def test_duplicate_compositions_keep_the_first():
    from app.services.traffic_packages import parse_package_spec

    descriptors = parse_package_spec('10:100, 10:999')
    assert len(descriptors) == 1
    assert descriptors[0]['price_kopeks'] == 100


def test_dimension_key_case_is_normalised():
    from app.services.traffic_packages import parse_package_spec

    assert parse_package_spec('WL=5:100')[0]['grants'][0]['dim'] == 'wl'


def test_describe_uses_admin_titles():
    from app.services.traffic_packages import describe_package, parse_package_spec

    package = parse_packages(parse_package_spec('10+wl=5:12900'))[0]
    assert describe_package(package, {'wl': '⚪ WL Трафик'}) == '10 ГБ + 5 ГБ ⚪ WL Трафик'


def test_describe_marks_unlimited_base():
    from app.services.traffic_packages import describe_package, parse_package_spec

    package = parse_packages(parse_package_spec('0:50000'))[0]
    assert 'безлимит' in describe_package(package)
