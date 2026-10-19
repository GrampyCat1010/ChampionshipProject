import hashlib
import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from .content import DOMAINS, fetch_publication, normalize_url, platform_for, source_matches
from .db import connect, now, update_search
from .yandex import ServiceError, analyze, search_terms, web_search

log = logging.getLogger(__name__)


class JobRunner:
    """One process, bounded background work; the database holds all visible state."""
    def __init__(self, config):
        self.config = config
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='media-search')
        self.slots = BoundedSemaphore(6)

    def submit(self, search_id):
        if not self.slots.acquire(blocking=False):
            return False
        try:
            future = self.pool.submit(run_search, self.config, search_id)
            future.add_done_callback(lambda _: self.slots.release())
        except Exception:
            self.slots.release()
            raise
        return True


def load_search(config, search_id):
    with connect(config['DATABASE']) as db:
        row = db.execute('SELECT * FROM searches WHERE id=?', (search_id,)).fetchone()
    search = dict(row)
    for field in ('platforms', 'sources', 'warnings', 'coverage'):
        search[field] = json.loads(search[field])
    return search


def build_queries(search, terms):
    scopes = []
    if search['mode'] == 'sources':
        scopes = [('site:' + s['url'].split('://', 1)[1].rstrip('/'), s['name']) for s in search['sources']]
    else:
        for platform in search['platforms']:
            if platform == 'web':
                # Domain exclusions can suppress otherwise valid Yandex results.
                # Keep discovery broad; candidate_allowed enforces platforms locally.
                scopes.append(('', 'Новостные сайты'))
            else:
                scopes.append(('(' + ' | '.join('site:' + d for d in DOMAINS[platform]) + ')', {'vk': 'ВКонтакте', 'ok': 'Одноклассники', 'max': 'MAX'}[platform]))
    queries = []
    for scope, label in scopes:
        for term in terms:
            # Date operators are a discovery hint only; originals are filtered again below.
            suffix = f" {search['region']} {scope} date:{search['date_from'].replace('-', '')}..{search['date_to'].replace('-', '')}"
            available = 400 - len(suffix)
            if available < 20:
                raise ServiceError('Слишком длинный адрес источника для поиска Яндекса. Используйте короткую ссылку.')
            queries.append((term[:available] + suffix, label))
    return queries


def candidate_allowed(search, url):
    if platform_for(url) not in search['platforms']:
        return False
    if search['mode'] == 'sources':
        return any(source_matches(url, s['url']) for s in search['sources'])
    return True


def run_search(config, search_id):
    path = config['DATABASE']
    warnings, coverage = [], []
    def warning(message):
        if message not in warnings:
            warnings.append(message)
    try:
        search = load_search(config, search_id)
        update_search(path, search_id, state='running', stage='Ищем публикации в Яндексе')
        try:
            terms = search_terms(config, search)
        except ServiceError as exc:
            warning(str(exc))
            terms = [re.sub(r'\s+', ' ', search['query'])[:150]]
        queries = build_queries(search, terms)
        candidates = {}
        succeeded = 0
        limit = config['MAX_CANDIDATES']
        for query, label in queries:
            if len(candidates) >= limit:
                warning(f'Достигнут лимит {limit} кандидатов. Часть поисковых запросов не выполнена.')
                break
            for page in range(config['SEARCH_PAGES']):
                try:
                    results = web_search(config, query, page)
                    succeeded += 1
                    coverage.append({'source': label, 'page': page + 1, 'status': 'ok', 'found': len(results)})
                    for candidate in results:
                        try:
                            url = normalize_url(candidate['url'])
                        except ValueError:
                            continue
                        if candidate_allowed(search, url):
                            candidate['url'] = url
                            candidates.setdefault(url, candidate)
                        if len(candidates) >= limit:
                            warning(f'Обработка ограничена {limit} кандидатами; это не полный охват интернета.')
                            break
                except ServiceError as exc:
                    warning(str(exc))
                    coverage.append({'source': label, 'page': page + 1, 'status': 'error', 'found': 0})
                    break
                finally:
                    update_search(path, search_id, coverage=json.dumps(coverage, ensure_ascii=False), warnings=json.dumps(warnings, ensure_ascii=False))
                if not results or len(candidates) >= limit:
                    break
        if not succeeded:
            update_search(path, search_id, state='failed', stage='Поиск не выполнен', finished_at=now())
            return
        update_search(path, search_id, total=len(candidates), stage='Получаем оригиналы и проверяем содержание')
        seen_originals = set()
        for index, candidate in enumerate(candidates.values()):
            url = candidate['url']
            pub = {'id': uuid.uuid4().hex, 'search_id': search_id, 'url': url, 'title': candidate['title'][:500] or url,
                   'source': urlsplit(url).hostname, 'platform': platform_for(url), 'published_at': None, 'fetched_at': now(),
                   'original_text': '', 'snippet': candidate['snippet'][:3000], 'fetch_status': 'unavailable', 'fetch_note': '',
                   'relevance': 'review', 'reason': '', 'evidence': '', 'region_reason': '', 'verification': 'insufficient',
                   'included': 0, 'content_hash': ''}
            try:
                original = fetch_publication(url)
                resolved = normalize_url(original.pop('resolved_url', url))
                if not candidate_allowed(search, resolved):
                    raise ValueError('Переадресация ведёт за пределы выбранных источников.')
                if resolved in seen_originals:
                    continue
                seen_originals.add(resolved)
                pub['url'] = resolved
                pub.update(original)
                pub['fetch_status'] = 'received'
            except Exception as exc:
                pub['fetch_note'] = str(exc) if isinstance(exc, ValueError) else 'Источник не ответил или ограничил получение текста.'
                pub['reason'] = 'Оригинал не получен. Сниппет поиска не подтверждает содержание публикации.'
                warning('Не все оригиналы удалось получить. Недоступные материалы вынесены на проверку.')
            if pub['original_text']:
                date = pub['published_at']
                if date and not (search['date_from'] <= date <= search['date_to']):
                    pub['relevance'] = 'irrelevant'
                    pub['reason'] = 'Дата публикации выходит за выбранный период.'
                else:
                    try:
                        result = analyze(config, search, pub)
                        for field in ('relevance', 'reason', 'evidence', 'region_reason', 'verification'):
                            pub[field] = result.get(field, '')
                        if not date:
                            pub['relevance'] = 'review'
                            pub['reason'] = 'Дата публикации не подтверждена. ' + pub['reason']
                        pub['included'] = int(pub['relevance'] == 'relevant')
                    except ServiceError as exc:
                        pub['reason'] = str(exc)
                        warning(str(exc))
            with connect(path) as db:
                fields = list(pub)
                db.execute(f"INSERT OR IGNORE INTO publications ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", list(pub.values()))
            update_search(path, search_id, progress=index + 1, warnings=json.dumps(warnings, ensure_ascii=False))
        update_search(path, search_id, state='partial' if warnings else 'completed', stage='Подборка готова' if candidates else 'Поиск завершён: публикаций не найдено',
                      finished_at=now(), progress=len(candidates), warnings=json.dumps(warnings, ensure_ascii=False))
    except Exception as exc:
        warning(str(exc) if isinstance(exc, ServiceError) else 'Обработка прервана. Уже полученные материалы сохранены; можно повторить поиск.')
        log.warning('Search %s failed: %s', search_id, type(exc).__name__)
        update_search(path, search_id, state='failed', stage='Обработка прервана', warnings=json.dumps(warnings, ensure_ascii=False), finished_at=now())
