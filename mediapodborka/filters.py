from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import re


def fresh_at(value, anchor):
    try:
        published = datetime.fromisoformat(value.replace('Z', '+00:00'))
        current = datetime.fromisoformat(anchor.replace('Z', '+00:00'))
        if published.tzinfo is None or current.tzinfo is None:
            return False
        return current - timedelta(hours=24) <= published <= current
    except (ValueError, TypeError, AttributeError):
        return False


def source_key(url):
    parsed = urlsplit(url)
    host, path = parsed.hostname or '', parsed.path
    if host in ('vk.com', 'vk.ru'):
        match = re.match(r'/wall(-?\d+)_', path)
        if match:
            return host + '/wall' + match[1]
    if host in ('ok.ru', 'max.ru'):
        return host + '/' + '/'.join(path.strip('/').split('/')[:2 if path.startswith('/group/') else 1])
    return host


def result_groups(publications):
    groups = {}
    for pub in publications:
        identity = source_key(pub['url'])
        group = groups.setdefault(identity, {'id': identity, 'title': pub['source'], 'count': 0, 'publication_ids': []})
        group['count'] += 1
        group['publication_ids'].append(pub['id'])
    return list(groups.values())
