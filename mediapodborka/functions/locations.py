"""Офлайн-проверка принадлежности города региону; источник справочника описан в data/README.md."""
import csv
import re
from functools import lru_cache
from pathlib import Path

# Регион, который подставляется, если пользователь ничего не указал
DEFAULT_REGION = 'Красноярский край'
# Название страны (если выбрана «Россия», проверяется только существование города)
COUNTRY = 'Россия'


def key(value):
    """Приводит строку к единому виду для сравнения:
    нижний регистр, «ё» -> «е», лишние пробелы убраны."""
    return re.sub(r'\s+', ' ', value.casefold().replace('ё', 'е').strip())


def region_key(value):
    """То же, что key(), но дополнительно убирает слова «область», «край», «респ.» и т.п.,
    чтобы «Красноярский край» и «Красноярский» считались одним регионом."""
    cleaned = re.sub(
        r'\b(область|обл\.?|край|республика|респ\.?|автономный округ|автономная область|г\.)\b',
        '',
        key(value),
    )
    return key(cleaned).strip(' .')


@lru_cache(maxsize=1)
def registry():
    """Читает справочник городов из data/cities.csv один раз и кэширует результат."""
    spisok = []
    # Расшифровка сокращённых типов регионов из справочника
    tipy = {
        'обл': 'область',
        'Респ': 'Республика',
        'АО': 'автономный округ',
        'Аобл': 'автономная область',
    }
    put = Path(__file__).resolve().parent.parent / 'data' / 'cities.csv'
    with put.open(encoding='utf-8-sig', newline='') as fayl:
        for stroka in csv.DictReader(fayl):
            region = stroka['region']
            vid = tipy.get(stroka['region_type'], stroka['region_type'])
            # Для городов федерального значения (Москва, СПб) тип не дописываем
            label = region if vid == 'г' else f'{region} {vid}'
            # Если города нет, берём населённый пункт; для Москвы/СПб город = регион
            city = stroka['city'] or stroka['settlement'] or (region if vid == 'г' else '')
            if city:
                spisok.append({'city': city, 'region': label, 'region_name': region})
    return spisok


def validate_location(region, city):
    """Проверяет, что город действительно относится к региону.
    Возвращает пару (регион, город) или бросает ValueError с понятным сообщением."""
    # Город не указан - проверять нечего
    if not city:
        return region, ''

    # Все записи справочника с таким названием города
    matches = [r for r in registry() if key(r['city']) == key(city)]

    # Из них оставляем те, что подходят под выбранный регион
    # (или любые, если выбрана вся Россия)
    allowed = [
        r for r in matches
        if region_key(region) in (region_key(r['region']), region_key(r['region_name']))
        or key(region) == key(COUNTRY)
    ]
    if allowed:
        itog_region = COUNTRY if key(region) == key(COUNTRY) else allowed[0]['region']
        return itog_region, allowed[0]['city']

    # Город существует, но в другом регионе - подсказываем, где именно
    if matches:
        alternatives = ', '.join(dict.fromkeys(r['region'] for r in matches))
        raise ValueError(
            f'Город «{city}» не относится к региону «{region}». '
            f'Укажите: {alternatives}, либо уберите город.'
        )

    # Города нет в справочнике вообще
    raise ValueError(
        f'Город «{city}» не найден в справочнике. Проверьте название или уберите город; '
        f'непроверенная принадлежность не допускается.'
    )