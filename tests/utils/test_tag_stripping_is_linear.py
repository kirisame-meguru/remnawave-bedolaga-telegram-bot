"""Вырезание тегов не должно перебирать хвост заново с каждой позиции.

CodeQL (py/polynomial-redos, high) показал это на `app/utils/rich_notify.py`:
шаблон `<[^>]+>` на строке из одних `<` даёт квадратичное время, а текст
уведомления приходит от пользователя. Тот же шаблон стоял ещё в шести местах,
включая путь, через который проходит каждое исходящее сообщение бота.

Проверяется и семантика: `[^<>]` не только быстрее, но и правильнее — `1 < 2`
остаётся текстом, а не съедается как незакрытый тег.
"""

import time

import pytest

from app.utils.rich_notify import _visible_length


@pytest.mark.parametrize(
    ('source', 'expected'),
    [
        ('<b>жирный</b>', 'жирный'),
        ('<a href="https://example.com">ссылка</a>', 'ссылка'),
        # Главное отличие: [^>] съедал бы «< 2</b>» целиком как один тег.
        ('<b>1 < 2</b>', '1 < 2'),
        ('без тегов', 'без тегов'),
    ],
)
def test_stripping_keeps_text_and_bare_angle_brackets(source, expected):
    assert _visible_length(source) == len(expected)


def test_pathological_input_stays_fast():
    """Строка из одних «<» — ровно тот вход, на котором старый шаблон вставал."""
    payload = '<' * 40_000

    start = time.perf_counter()
    assert _visible_length(payload) == len(payload)
    elapsed = time.perf_counter() - start

    # Старый шаблон на этом входе тратил доли секунды и рос квадратично;
    # запас взят большой, чтобы тест не мигал на нагруженной машине.
    assert elapsed < 0.1, f'вырезание тегов заняло {elapsed:.3f}s — шаблон снова квадратичный'


def test_html_validator_stays_fast_on_unclosed_tags():
    """Проверка HTML правовых страниц: их длина из кабинета ничем не ограничена.

    Шаблон `[^>]*` после имени тега перебирал хвост заново с каждого «<»:
    на 80 КБ из «<a» разбор структуры занимал 10 секунд заблокированного CPU.
    """
    from app.utils.validators import validate_html_tags

    payload = '<a' * 40_000

    start = time.perf_counter()
    validate_html_tags(payload)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.5, f'проверка HTML заняла {elapsed:.3f}s — шаблон снова квадратичный'


@pytest.mark.parametrize(
    ('source', 'valid'),
    [
        ('<b>жирный</b>', True),
        ('<a href="https://example.com">ссылка</a>', True),
        ('<b>1 < 2</b>', True),
        ('<nosuchtag>текст</nosuchtag>', False),
    ],
)
def test_html_validator_verdicts_unchanged(source, valid):
    """Ускорение не должно менять вердикты на обычной разметке."""
    from app.utils.validators import validate_html_tags

    assert validate_html_tags(source)[0] is valid
