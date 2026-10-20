from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import re


def fresh_at(value, anchor):
    """Проверяет, что публикация вышла не раньше чем за 24 часа до момента запуска поиска.
    Если время или часовой пояс неизвестны - считаем публикацию не свежей."""
    try:
        published = datetime.fromisoformat(value.replace('Z', '+00:00'))
        current = datetime.fromisoformat(anchor.replace('Z', '+00:00'))
        # Без часового пояса сравнивать нельзя
        if published.tzinfo is None or current.tzinfo is None:
            return False
        return current - timedelta(hours=24) <= published <= current
    except (ValueError, TypeError, AttributeError):
        return False


def source_key(url):
    """Строит ключ источника (сообщество/канал/сайт) для группировки публикаций."""
    parsed = urlsplit(url)
    host, path = parsed.hostname or '', parsed.path

    # ВКонтакте: группируем по сообществу из адреса поста /wall-123_456
    if host in ('vk.com', 'vk.ru'):
        match = re.match(r'/wall(-?\d+)_', path)
        if match:
            return host + '/wall' + match[1]

    # ОК и MAX: группируем по первому (или двум) сегментам пути
    if host in ('ok.ru', 'max.ru'):
        chasti = 2 if path.startswith('/group/') else 1
        return host + '/' + '/'.join(path.strip('/').split('/')[:chasti])

    # Остальные сайты - по домену
    return host


def result_groups(publications):
    """Собирает публикации в группы по источнику (используется, когда результатов больше 50)."""
    groups = {}
    for pub in publications:
        identity = source_key(pub['url'])
        group = groups.setdefault(
            identity,
            {'id': identity, 'title': pub['source'], 'count': 0, 'publication_ids': []},
        )
        group['count'] += 1
        group['publication_ids'].append(pub['id'])
    return list(groups.values())

