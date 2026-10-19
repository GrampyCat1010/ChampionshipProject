import hashlib
import json
import logging
import re
import uuid
import ssl
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from urllib.parse import urlsplit
from math import ceil
from itertools import zip_longest

from .content import DOMAINS, fetch_publication, normalize_url, platform_for, source_matches
from .db import connect, now, update_search
from .yandex import ServiceError, analyze, search_terms, web_search, summarize_reference
from .matching import query_match
from .filters import fresh_at

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
    search['reference'] = json.loads(search['reference'])
    return search


def build_queries(search, terms):
    scopes = []
    if search.get('scope', search['mode']) == 'sources':
        scopes = [('site:' + s['url'].split('://', 1)[1].rstrip('/'), s['name']) for s in search['sources']]
    else:
        # The common query finds indexed vk.ru posts that site:vk.com can miss.
        scopes.append(('', 'Общий поиск'))
        for platform in search['platforms']:
            if platform == 'web':
                continue
            else:
                domains = ('vk.ru', 'vk.com') if platform == 'vk' else DOMAINS[platform]
                scopes.extend(('site:' + d, {'vk': 'ВКонтакте', 'ok': 'Одноклассники', 'max': 'MAX'}[platform]) for d in domains)
    queries = []
    for scope, label in scopes:
        for term in terms:
            # Publication dates/geography are verified on originals. Mandatory date
            # and region operators can hide relevant posts with incomplete indexing.
            suffix = f' {scope}'
            available = 400 - len(suffix)
            if available < 20:
                raise ServiceError('Слишком длинный адрес источника для поиска Яндекса. Используйте короткую ссылку.')
            if search['mode'] == 'by_thesis' and len(term) > available:
                raise ServiceError('Фраза вместе с выбранным источником слишком длинна. Сократите фразу или адрес источника.')
            queries.append(((term[:available] + suffix).strip(), label))
    location = search.get('city') or (search['region'] if search['region'] != 'Россия' else '')
    if location:
        for term in terms:
            if location.casefold() not in term.casefold() and search.get('scope', search['mode']) != 'sources':
                queries.append(((term + ' ' + location)[:400], 'Поиск с географией'))
    # An additional dated query finds current news for broad topics, while the
    # undated queries above retain posts whose dates are missing from the index.
    for scope, label in scopes:
        for term in terms:
            context = location if location and location.casefold() not in term.casefold() else ''
            suffix = f" {context} {scope} date:{search['date_from'].replace('-', '')}..{search['date_to'].replace('-', '')}"
            if len(term + suffix) <= 400:
                queries.append(((term + suffix).strip(), label + ' · география и период'))
    if search.get('scope', search['mode']) != 'sources' and 'web' in search['platforms']:
        for term in terms:
            context = location if location and location.casefold() not in term.casefold() else ''
            news_query = f"{term} {context} новости date:{search['date_from'].replace('-', '')}..{search['date_to'].replace('-', '')}"
            if len(news_query) <= 400:
                queries.append((news_query.strip(), 'Новости по теме и географии'))
    return queries


def ordered_candidates(candidates, search, limit):
    """Reserve discovery/processing room for every requested platform."""
    buckets = {platform: [] for platform in search['platforms']}
    for candidate in candidates.values():
        buckets[platform_for(candidate['url'])].append(candidate)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: (
            -item.get('location_priority', 1 if item.get('location_hint') else 0),
            not bool(query_match(search['query'], item['title'])),
            not bool(query_match(search['query'], item['title'] + '\n' + item['snippet'])),
        ) if search['mode'] == 'by_thesis' else (False, False))
    return [item for group in zip_longest(*buckets.values()) for item in group if item][:limit]


def candidate_allowed(search, url):
    if platform_for(url) not in search['platforms']:
        return False
    if search.get('scope', search['mode']) == 'sources':
        return any(source_matches(url, s['url']) for s in search['sources'])
    return True


def run_search(config, search_id):
    path = config['DATABASE']
    warnings, coverage, diagnostics = [], [], []
    processed = 0
    def warning(message):
        if message not in warnings:
            warnings.append(message)
    def record(candidate, stage, reason):
        diagnostics.append({'url': candidate['url'], 'title': candidate.get('title', '')[:500],
                            'stage': stage, 'reason': reason[:1200]})
        update_search(path, search_id, diagnostics=json.dumps(diagnostics, ensure_ascii=False))
    try:
        # Claim once: retries of the worker must not duplicate history or paid work.
        with connect(path) as db:
            claimed = db.execute("UPDATE searches SET state='running' WHERE id=? AND state='queued'", (search_id,)).rowcount
        if not claimed:
            return
        search = load_search(config, search_id)
        update_search(path, search_id, state='running', stage='Ищем публикации в Яндексе')
        if search['mode'] == 'compare':
            update_search(path, search_id, stage='Получаем эталон и готовим сокращённую версию')
            if search['query'].startswith(('https://', 'http://')):
                try:
                    reference = fetch_publication(normalize_url(search['query']))
                    reference['url'] = normalize_url(reference.pop('resolved_url', search['query']))
                except Exception as exc:
                    raise ServiceError('Не удалось получить текст эталона по ссылке. Вставьте текст новости вручную.') from exc
            else:
                reference = {'original_text': search['query'], 'title': 'Исходный текст', 'url': '', 'published_at': None}
            update_search(path, search_id, reference=json.dumps(reference, ensure_ascii=False))
            reference.update(summarize_reference(config, reference['original_text']))
            search['reference'] = reference
            update_search(path, search_id, reference=json.dumps(reference, ensure_ascii=False))
        try:
            terms = search_terms(config, search)
        except ServiceError as exc:
            warning(str(exc))
            terms = [re.sub(r'\s+', ' ', search['query'])[:150]]
        queries = build_queries(search, terms)
        candidates = {}
        succeeded = 0
        result_limit = search['result_limit']
        limit = min(500, max(config['MAX_CANDIDATES'], result_limit * 3))
        # Explicit hard budget used by integration checks and deployments.
        limit = min(limit, config.get('HARD_CANDIDATE_LIMIT', 500))
        # Query every selected platform before advancing to the next index page.
        exhausted = set()
        pages = max(config['SEARCH_PAGES'], ceil(min(result_limit * 3, limit) / 20))
        for page in range(pages):
            for query, label in queries:
                if query in exhausted:
                    continue
                try:
                    results = web_search(config, query, page)
                    succeeded += 1
                    coverage.append({'source': label, 'query': query.strip(), 'page': page + 1, 'status': 'ok', 'found': len(results)})
                    for candidate in results:
                        try:
                            url = normalize_url(candidate['url'])
                        except ValueError:
                            continue
                        if candidate_allowed(search, url):
                            candidate['url'] = url
                            existing = candidates.setdefault(url, candidate)
                            if label == 'Поиск с географией':
                                existing['location_hint'] = True
                                existing['location_priority'] = max(1, existing.get('location_priority',0))
                            elif label.endswith('география и период'):
                                existing['location_priority'] = 2
                            elif label == 'Новости по теме и географии':
                                existing['location_priority'] = 3
                    if not results:
                        exhausted.add(query)
                except ServiceError as exc:
                    warning(str(exc))
                    coverage.append({'source': label, 'page': page + 1, 'status': 'error', 'found': 0})
                    exhausted.add(query)
                finally:
                    update_search(path, search_id, coverage=json.dumps(coverage, ensure_ascii=False), warnings=json.dumps(warnings, ensure_ascii=False))
            if len(candidates) >= limit:
                break
        if not succeeded:
            update_search(path, search_id, state='failed', stage='Поиск не выполнен', finished_at=now())
            return
        selected = ordered_candidates(candidates, search, limit)
        if len(candidates) > limit:
            warning(f'Найдено {len(candidates)} ссылок; обработка ограничена {limit} кандидатами с распределением между площадками.')
        update_search(path, search_id, total=len(candidates), stage='Получаем оригиналы и проверяем содержание')
        seen_originals = set()
        saved = 0
        pending_social = []
        for index, candidate in enumerate(selected):
            if saved >= result_limit:
                break
            update_search(path, search_id, progress=index + 1)
            processed = index + 1
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
                    record(candidate, 'duplicate', 'Повтор уже обработанной публикации.')
                    continue
                seen_originals.add(resolved)
                pub['url'] = resolved
                pub.update(original)
                pub['fetch_status'] = 'received'
                if search['mode'] == 'compare' and (resolved == search['reference'].get('url') or pub['original_text'] == search['reference']['original_text']):
                    record(candidate, 'same_reference', 'Это сам эталон или его точная копия.')
                    continue
            except Exception as exc:
                pub['fetch_note'] = str(exc) if isinstance(exc, ValueError) else 'Источник не ответил или ограничил получение текста.'
                pub['reason'] = 'Оригинал не получен. Сниппет поиска не подтверждает содержание публикации.'
                warning('Не все оригиналы удалось получить. Доступность текста указана в карточках; неподходящие ссылки пропущены.')
                if isinstance(exc, ssl.SSLError):
                    reason = 'Не удалось установить защищённое соединение с сайтом (TLS). Содержание не проверено.'
                elif isinstance(exc, (TimeoutError, socket.timeout)):
                    reason = 'Сайт не ответил вовремя. Содержание не проверено.'
                elif isinstance(exc, ValueError):
                    reason = str(exc)
                else:
                    reason = 'Не удалось загрузить оригинал (' + type(exc).__name__ + '). Содержание не проверено.'
                record(candidate, 'fetch', reason)
                # Put indexed social posts in the common list as review cards.
                # Append after originals so they cannot exhaust the result budget.
                if platform_for(url) != 'web' and search['mode'] == 'by_thesis' and query_match(search['query'], candidate['title'] + '\n' + candidate['snippet']):
                    if not search['fresh_only']:
                        try:
                            preliminary = analyze(config, search, {'original_text':candidate['title'] + '\n' + candidate['snippet'],
                                'title':candidate['title'], 'content_source':'search_snippet'})
                            if preliminary['relevance'] == 'irrelevant':
                                diagnostics[-1].update(stage='analysis', reason='По поисковому описанию: ' + preliminary['reason'])
                                update_search(path, search_id, diagnostics=json.dumps(diagnostics, ensure_ascii=False))
                                continue
                        except ServiceError:
                            pass  # Keep only an explicitly unverified review card.
                    diagnostics[-1].update(index_match=True, snippet=candidate['snippet'][:3000], platform=platform_for(url))
                    update_search(path, search_id, diagnostics=json.dumps(diagnostics, ensure_ascii=False))
                    if not search['fresh_only']:
                        pub.update(fetch_note=reason, reason='Совпадение в поисковом описании. Оригинал и дата не подтверждены; откройте источник для проверки.')
                        pending_social.append(pub)
                continue
            if pub['original_text']:
                date = pub['published_at']
                if date and not (search['date_from'] <= date[:10] <= search['date_to']):
                    record(candidate, 'date', f'Дата публикации {date} вне выбранного периода.')
                    continue
                if search['fresh_only'] and not fresh_at(date, search['created_at']):
                    record(candidate, 'freshness', 'Публикация старше 24 часов либо точное время с часовым поясом неизвестно.')
                    continue
                if search['mode'] == 'by_thesis':
                    pub['exact_match'] = query_match(search['query'], pub['title'] + '\n' + pub['original_text'])
                    if not pub['exact_match']:
                        record(candidate, 'exact_match', f'В оригинале нет всех ключевых слов или указанной в кавычках фразы из запроса «{search["query"]}».')
                        continue
                try:
                    result = analyze(config, search, pub)
                    for field in ('relevance', 'reason', 'evidence', 'region_reason', 'verification', 'emotion', 'emotion_reason', 'style', 'style_reason', 'difference', 'reference_evidence'):
                        if field in result:
                            pub[field] = result[field]
                    if pub['relevance'] == 'irrelevant':
                        record(candidate, 'analysis', pub['reason'])
                        continue
                    if not date:
                        pub['reason'] = 'Дата публикации не подтверждена. ' + pub['reason']
                    # Relevance describes the content. Unknown date is a separate
                    # limitation: keep it visible and require manual selection.
                    pub['included'] = int(pub['relevance'] == 'relevant' and bool(date))
                except ServiceError as exc:
                    pub['reason'] = str(exc)
                    warning(str(exc))
                    if search['mode'] == 'compare':
                        record(candidate, 'analysis_error', str(exc))
                        continue
                if pub.get('exact_match'):
                    pub['reason'] += f' Найденные слова: «{pub["exact_match"]}».'
            with connect(path) as db:
                fields = list(pub)
                saved += db.execute(f"INSERT OR IGNORE INTO publications ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", list(pub.values())).rowcount
            update_search(path, search_id, progress=index + 1, warnings=json.dumps(warnings, ensure_ascii=False))
        for pub in pending_social[:max(0, result_limit - saved)]:
            with connect(path) as db:
                fields = list(pub)
                saved += db.execute(f"INSERT OR IGNORE INTO publications ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", list(pub.values())).rowcount
        if saved == 0 and processed and all(d['stage'] == 'fetch' for d in diagnostics):
            warning('Яндекс нашёл ссылки, но тексты источников недоступны. Это не означает, что подходящих новостей нет. Причины и ссылки приведены ниже.')
        if not saved:
            log.info('empty_search id=%s mode=%s candidates=%s', search_id, search['mode'], len(candidates))
        update_search(path, search_id, state='partial' if warnings else 'completed', stage='Подборка готова' if saved else 'Подходящих публикаций не найдено',
                      finished_at=now(), progress=processed, warnings=json.dumps(warnings, ensure_ascii=False))
    except Exception as exc:
        warning(str(exc) if isinstance(exc, ServiceError) else 'Обработка прервана. Уже полученные материалы сохранены; можно повторить поиск.')
        log.warning('Search %s failed: %s', search_id, type(exc).__name__)
        update_search(path, search_id, state='failed', stage='Обработка прервана', warnings=json.dumps(warnings, ensure_ascii=False), finished_at=now())
