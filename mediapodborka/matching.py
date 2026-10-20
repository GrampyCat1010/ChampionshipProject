"""Сопоставление по словарю, только непрерывные фразы. Без стемминга и угадывания лексем."""
import re
from functools import lru_cache

from pymorphy3 import MorphAnalyzer
from pymorphy3.units import DictionaryAnalyzer

# Слово (допускаются дефисные слова вроде «Камень-на-Оби»)
TOKEN = re.compile(r'[^\W_]+(?:-[^\W_]+)*', re.UNICODE)


@lru_cache(maxsize=1)
def morphology():
    """Создаёт морфологический анализатор один раз (только по словарю, без угадывания)."""
    return MorphAnalyzer(units=[DictionaryAnalyzer()], char_substitutes={})


@lru_cache(maxsize=20000)
def lemmas(word):
    """Возвращает множество начальных форм слова."""
    razbory = morphology().parse(word.casefold())
    scores = {}
    for razbor in razbory:
        scores[razbor.normal_form] = scores.get(razbor.normal_form, 0) + razbor.score

    # «дороги» чаще всего существительное, у которого несколько падежей одной лексемы;
    # редкий разбор краткого прилагательного не должен отключать все формы существительного.
    # Для действительно неоднозначных слов остаётся только буквальное совпадение.
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] >= 0.9:
            return frozenset([best])
    return frozenset(scores)


def phrase_match(query, text):
    """Ищет в тексте идущие подряд слова запроса (с учётом словоформ).
    Возвращает найденный фрагмент или пустую строку."""
    wanted = list(TOKEN.finditer(query))
    words = list(TOKEN.finditer(text))
    if not wanted:
        return ''

    normalized = [w.group().casefold() for w in wanted]
    meanings = [lemmas(w) for w in normalized]

    # Скользящее окно по словам текста
    for offset in range(len(words) - len(wanted) + 1):
        window = words[offset:offset + len(wanted)]

        # Не склеиваем слова через знаки препинания / границы предложений
        if any(not text[a.end():b.start()].isspace() for a, b in zip(window, window[1:])):
            continue

        for expected, meaning, actual in zip(normalized, meanings, window):
            value = actual.group().casefold()
            if value != expected and not (len(meaning) == 1 and meaning & lemmas(value)):
                break
        else:
            # Все слова окна совпали
            return text[window[0].start():window[-1].end()]
    return ''


# Фразы в кавычках: "..." или «...»
QUOTED = re.compile(r'"([^"\n]+)"|«([^»\n]+)»')
# Служебные слова, которые не требуем от текста
STOP_WORDS = frozenset('и в во на по из за с со о об от к ко для у а но это'.split())


@lru_cache(maxsize=1)
def city_names():
    """Множество названий городов (нужно, чтобы отделять географию от тезиса)."""
    from .locations import registry
    return frozenset(row['city'].casefold() for row in registry())


def query_parts(query):
    """Делит запрос на фразы в кавычках, обычные слова и города.
    Только фразы, заданные пользователем в кавычках, требуют соседства слов;
    география проверяется в контексте."""
    phrases = [next(p for p in match.groups() if p) for match in QUOTED.finditer(query)]
    rest = QUOTED.sub(' ', query)
    words = [m.group() for m in TOKEN.finditer(rest)]
    places = [w for w in words if w.casefold() in city_names()]
    terms = [w for w in words if w.casefold() not in STOP_WORDS and w not in places]
    return phrases, terms, places


def query_match(query, text):
    """Дешёвая лексическая проверка; смысл всего запроса затем оценивает модель.

    Названия городов могут стоять в виде прилагательных (Саратов / Саратовский).
    Их передаём модели как есть, никогда не угадывая по основе слова.
    """
    phrases, terms, places = query_parts(query)
    required = phrases + terms
    # Если в запросе только город - ищем его
    if not required:
        required = places
    matches = [phrase_match(term, text) for term in required]
    if not matches or not all(matches):
        return ''
    return ' · '.join(dict.fromkeys(matches))