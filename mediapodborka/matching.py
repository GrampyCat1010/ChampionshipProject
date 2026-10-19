"""Dictionary-only, contiguous phrase matching. No stemming or predicted lexemes."""
import re
from functools import lru_cache

from pymorphy3 import MorphAnalyzer
from pymorphy3.units import DictionaryAnalyzer

TOKEN = re.compile(r'[^\W_]+(?:-[^\W_]+)*', re.UNICODE)


@lru_cache(maxsize=1)
def morphology():
    return MorphAnalyzer(units=[DictionaryAnalyzer()], char_substitutes={})


@lru_cache(maxsize=20000)
def lemmas(word):
    parses = morphology().parse(word.casefold())
    scores = {}
    for parsed in parses:
        scores[parsed.normal_form] = scores.get(parsed.normal_form, 0) + parsed.score
    # "дороги" is usually the noun, with several cases sharing one lexeme;
    # a rare short-adjective parse must not disable all noun inflections.
    # For genuinely ambiguous words keep literal-only matching.
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] >= 0.9:
            return frozenset([best])
    return frozenset(scores)


def phrase_match(query, text):
    wanted = list(TOKEN.finditer(query))
    words = list(TOKEN.finditer(text))
    if not wanted:
        return ''
    normalized = [w.group().casefold() for w in wanted]
    meanings = [lemmas(w) for w in normalized]
    for offset in range(len(words) - len(wanted) + 1):
        window = words[offset:offset + len(wanted)]
        # Do not match across punctuation/sentences or join partial words.
        if any(not text[a.end():b.start()].isspace() for a, b in zip(window, window[1:])):
            continue
        for expected, meaning, actual in zip(normalized, meanings, window):
            value = actual.group().casefold()
            if value != expected and not (len(meaning) == 1 and meaning & lemmas(value)):
                break
        else:
            return text[window[0].start():window[-1].end()]
    return ''


QUOTED = re.compile(r'"([^"\n]+)"|«([^»\n]+)»')
STOP_WORDS = frozenset('и в во на по из за с со о об от к ко для у а но это'.split())


@lru_cache(maxsize=1)
def city_names():
    from .locations import registry
    return frozenset(row['city'].casefold() for row in registry())


def query_parts(query):
    """Only user-supplied quotes require adjacency; geography is checked in context."""
    phrases = [next(p for p in match.groups() if p) for match in QUOTED.finditer(query)]
    rest = QUOTED.sub(' ', query)
    words = [m.group() for m in TOKEN.finditer(rest)]
    places = [w for w in words if w.casefold() in city_names()]
    terms = [w for w in words if w.casefold() not in STOP_WORDS and w not in places]
    return phrases, terms, places


def query_match(query, text):
    """Cheap lexical gate, followed by contextual model evaluation of the whole query.

    City names may be expressed as regional adjectives (Саратов / Саратовский).
    They are passed unchanged to the model, never guessed by a stem comparison.
    """
    phrases, terms, places = query_parts(query)
    required = phrases + terms
    if not required:
        required = places
    matches = [phrase_match(term, text) for term in required]
    if not matches or not all(matches):
        return ''
    return ' · '.join(dict.fromkeys(matches))
