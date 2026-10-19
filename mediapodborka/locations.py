"""Offline city membership validation; registry attribution in data/README.md."""
import csv
import re
from functools import lru_cache
from pathlib import Path

DEFAULT_REGION = 'Красноярский край'
COUNTRY = 'Россия'


def key(value):
    return re.sub(r'\s+', ' ', value.casefold().replace('ё', 'е').strip())


def region_key(value):
    return key(re.sub(r'\b(область|обл\.?|край|республика|респ\.?|автономный округ|автономная область|г\.)\b', '', key(value))).strip(' .')


@lru_cache(maxsize=1)
def registry():
    rows = []
    types = {'обл': 'область', 'Респ': 'Республика', 'АО': 'автономный округ', 'Аобл': 'автономная область'}
    with Path(__file__).with_name('data').joinpath('cities.csv').open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            region = row['region']
            kind = types.get(row['region_type'], row['region_type'])
            label = region if kind == 'г' else f'{region} {kind}'
            city = row['city'] or row['settlement'] or (region if kind == 'г' else '')
            if city:
                rows.append({'city': city, 'region': label, 'region_name': region})
    return rows


def validate_location(region, city):
    if not city:
        return region, ''
    matches = [r for r in registry() if key(r['city']) == key(city)]
    allowed = [r for r in matches if region_key(region) in (region_key(r['region']), region_key(r['region_name'])) or key(region) == key(COUNTRY)]
    if allowed:
        return (COUNTRY if key(region) == key(COUNTRY) else allowed[0]['region']), allowed[0]['city']
    if matches:
        alternatives = ', '.join(dict.fromkeys(r['region'] for r in matches))
        raise ValueError(f'Город «{city}» не относится к региону «{region}». Укажите: {alternatives}, либо уберите город.')
    raise ValueError(f'Город «{city}» не найден в справочнике. Проверьте название или уберите город; непроверенная принадлежность не допускается.')
