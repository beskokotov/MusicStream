"""Generate bounded alternatives for human-entered Deezer search queries."""

import re
import unicodedata

_EN_KEYS = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
_RU_KEYS = "йцукенгшщзхъфывапролджэячсмитьбюё"
_TO_RUSSIAN = str.maketrans(_EN_KEYS, _RU_KEYS)
_TO_ENGLISH = str.maketrans(_RU_KEYS, _EN_KEYS)
_CYRILLIC_WORD = re.compile(r"[а-яё]+")
_LATIN_WORD = re.compile(r"[a-z]+")
_LATIN_KEYS = re.compile(r"[a-z\[\];',.`]+")
_TRANSLITERATION = str.maketrans(dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ("a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m",
     "n", "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "",
     "y", "", "e", "yu", "ya"),
)))
_ALIASES = {"металлика": "metallica", "металика": "metallica"}
_GERMAN_SUFFIX = re.compile(r"шт(?:ай|ей|еи)н$")
_PHONETIC = re.compile(r"кс|эй|дж|ф")
_PHONETIC_FORMS = {"кс": "x", "эй": "ay", "дж": "j", "ф": "ph"}
_FIELD_QUERY = re.compile(r'(?:^|\s)(?:artist|track|album|label|dur_min|dur_max|bpm_min|bpm_max):', re.I)


def _romanize(query: str, *, phonetic: bool = False) -> str:
    def word(match):
        value = match[0]
        if value in _ALIASES:
            return _ALIASES[value]
        value = _GERMAN_SUFFIX.sub("stein", value)
        if phonetic:
            value = _PHONETIC.sub(lambda segment: _PHONETIC_FORMS[segment[0]], value)
        return value.translate(_TRANSLITERATION)

    return _CYRILLIC_WORD.sub(word, query)


def alternatives(query: str) -> list[str]:
    if _FIELD_QUERY.search(query):
        return []
    value = query.lower()
    russian_words = _CYRILLIC_WORD.findall(value)
    if russian_words:
        layout = value.translate(_TO_ENGLISH)
        romanized = _romanize(value)
        phonetic = _romanize(value, phonetic=True)
        keyboard_first = any(word.startswith(("ы", "ъ", "ь")) or re.search("[ъь]{2}", word)
                             for word in russian_words)
        variants = (layout, romanized, phonetic) if keyboard_first else (romanized, phonetic, layout)
    else:
        words = _LATIN_WORD.findall(value)
        if sum(map(len, words)) < 3:
            return []
        # Leave intact Latin words in queries such as "hfvvinfqy Sonne".
        suspect = {word for word in words if re.search(r"[bcdfghjklmnpqrstvwxyz]{4}", word)}
        if suspect and len(suspect) < len(set(words)):
            restored = _LATIN_WORD.sub(lambda word: word[0].translate(_TO_RUSSIAN)
                                       if word[0] in suspect else word[0], value)
        else:
            restored = _LATIN_KEYS.sub(lambda word: word[0].translate(_TO_RUSSIAN), value)
        variants = (_romanize(restored), restored)
    return [variant for variant in dict.fromkeys(variants) if variant and variant != value][:2]


def _terms(value: str) -> frozenset[str]:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return frozenset(re.findall(r"[^\W_]+", plain))


def query_terms(query: str) -> list[frozenset[str]]:
    value = query.lower()
    return [terms for form in dict.fromkeys((value, _romanize(value), _romanize(value, phonetic=True)))
            if (terms := _terms(form))]


def relevant(terms: list[frozenset[str]], *metadata: str | None, exact: bool = False) -> bool:
    actual = _terms(" ".join(field for field in metadata if field))
    return any(wanted == actual if exact else wanted <= actual for wanted in terms)
