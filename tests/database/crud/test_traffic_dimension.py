"""Валидация ключа измерения трафика.

Ключ неизменяем и попадает в `traffic_purchases.dimension`, поэтому проверка
формата — единственный момент, когда ошибку ещё можно поймать дёшево.
"""

import pytest

from app.database.crud.traffic_dimension import KEY_PATTERN


@pytest.mark.parametrize('key', ['wl', 'torrent', 'streaming_ru', 'x2', 'a' * 32])
def test_valid_keys(key):
    assert KEY_PATTERN.match(key)


@pytest.mark.parametrize(
    ('key', 'why'),
    [
        ('', 'пустой'),
        ('a', 'слишком короткий'),
        ('a' * 33, 'длиннее колонки String(32)'),
        ('WL', 'верхний регистр'),
        ('2wl', 'начинается с цифры'),
        ('wl-traffic', 'дефис'),
        ('wl traffic', 'пробел'),
        ('wl.traffic', 'точка'),
        ('трафик', 'кириллица'),
    ],
)
def test_invalid_keys(key, why):
    assert not KEY_PATTERN.match(key), why
