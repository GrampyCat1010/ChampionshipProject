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
import os
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED


from .content import DOMAINS, fetch_publication, normalize_url, platform_for, source_matches
from ..database.db import connect, now, update_search
from .yandex import ServiceError, analyze, search_terms, web_search, summarize_reference
from .matching import query_match
from .filters import fresh_at

log = logging.getLogger(__name__)


class JobRunner:
    """Один процесс, ограниченная фоновая работа; всё видимое состояние хранится в БД."""

    def __init__(self, config):
        self.config = config
        # Одновременно работают не больше двух поисков
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='media-search')
        # В очереди и в работе вместе - не больше шести
        self.slots = BoundedSemaphore(6)

    def submit(self, search_id):
        """Ставит поиск в очередь. Возвращает False, если очередь заполнена."""
        if not self.slots.acquire(blocking=False):
            return False
        try:
            future = self.pool.submit(run_search, self.config, search_id)
            # Когда поиск завершится - освобождаем место в очереди
            future.add_done_callback(lambda _: self.slots.release())
        except Exception:
            self.slots.release()
            raise
        return True


def load_search(config, search_id):
    """Читает поиск из БД и разворачивает JSON-поля."""
    with connect(config['DATABASE']) as db:
        row = db.execute('SELECT * FROM searches WHERE id=?', (search_id,)).fetchone()
    search = dict(row)
    for field in ('platforms', 'sources', 'warnings', 'coverage'):
        search[field] = json.loads(search[field])
    search['reference'] = json.loads(search['reference'])
    return search


def build_queries(search, terms):
    """Строит список поисковых запросов для Яндекса: [(текст запроса, подпись), ...]."""
    scopes = []
    if search.get('scope', search['mode']) == 'sources':
        # Поиск только по выбранным источникам: site:адрес
        scopes = [('site:' + s['url'].split('://', 1)[1].rstrip('/'), s['name']) for s in search['sources']]
    else:
        # Общий запрос находит проиндексированные посты vk.ru, которые site:vk.com может пропустить.
        scopes.append(('', 'Общий поиск'))
        for platform in search['platforms']:
            if platform == 'web':
                continue
            domains = ('vk.ru', 'vk.com') if platform == 'vk' else DOMAINS[platform]
            nazvanie = {'vk': 'ВКонтакте', 'ok': 'Одноклассники', 'max': 'MAX'}[platform]
            scopes.extend(('site:' + d, nazvanie) for d in domains)

    queries = []
    for scope, label in scopes:
        for term in terms:
            # Даты и география проверяются на оригиналах. Обязательные операторы даты и региона
            # могут скрыть подходящие посты с неполной индексацией.
            suffix = f' {scope}'
            available = 400 - len(suffix)
            if available < 20:
                raise ServiceError('Слишком длинный адрес источника для поиска Яндекса. Используйте короткую ссылку.')
            if search['mode'] == 'by_thesis' and len(term) > available:
                raise ServiceError('Фраза вместе с выбранным источником слишком длинна. Сократите фразу или адрес источника.')
            queries.append(((term[:available] + suffix).strip(), label))

    # Место для запросов с географией: город важнее региона, «Россия» не добавляем
    location = search.get('city') or (search['region'] if search['region'] != 'Россия' else '')
    if location:
        for term in terms:
            if location.casefold() not in term.casefold() and search.get('scope', search['mode']) != 'sources':
                queries.append(((term + ' ' + location)[:400], 'Поиск с географией'))

    # Дополнительный запрос с датой находит свежие новости по широким темам,
    # а запросы без даты выше сохраняют посты, у которых дата не попала в индекс.
    ot = search['date_from'].replace('-', '')
    do = search['date_to'].replace('-', '')
    for scope, label in scopes:
        for term in terms:
            context = location if location and location.casefold() not in term.casefold() else ''
            suffix = f" {context} {scope} date:{ot}..{do}"
            if len(term + suffix) <= 400:
                queries.append(((term + suffix).strip(), label + ' · география и период'))

    # Отдельный новостной запрос для сайтов и СМИ
    if search.get('scope', search['mode']) != 'sources' and 'web' in search['platforms']:
        for term in terms:
            context = location if location and location.casefold() not in term.casefold() else ''
            news_query = f"{term} {context} новости date:{ot}..{do}"
            if len(news_query) <= 400:
                queries.append((news_query.strip(), 'Новости по теме и географии'))
    return queries


def ordered_candidates(candidates, search, limit):
    """Упорядочивает кандидатов так, чтобы каждая выбранная площадка получила место в обработке."""
    # Раскладываем по «корзинам» площадок
    buckets = {platform: [] for platform in search['platforms']}
    for candidate in candidates.values():
        buckets[platform_for(candidate['url'])].append(candidate)

    # Внутри корзины сначала те, у кого есть география и совпадение в заголовке/описании
    for bucket in buckets.values():
        bucket.sort(key=lambda item: (
            -item.get('location_priority', 1 if item.get('location_hint') else 0),
            not bool(query_match(search['query'], item['title'])),
            not bool(query_match(search['query'], item['title'] + '\n' + item['snippet'])),
        ) if search['mode'] == 'by_thesis' else (False, False))

    # Чередуем площадки: по одному кандидату из каждой, пока не набрали лимит
    return [item for group in zip_longest(*buckets.values()) for item in group if item][:limit]


def candidate_allowed(search, url):
    """Проверяет, что ссылка подходит под выбранные площадки и источники."""
    if platform_for(url) not in search['platforms']:
        return False
    if search.get('scope', search['mode']) == 'sources':
        return any(source_matches(url, s['url']) for s in search['sources'])
    return True



def run_search(config, search_id):
    """Поиск публикаций с параллельной загрузкой и анализом кандидатов."""
    path = config['DATABASE']
    warnings, coverage, diagnostics = [], [], []
    processed = 0
    max_parallel = max(1, min(int(os.getenv('MAX_PARALLEL_CANDIDATES', '100')), 1000))

    def warning(message):
        if message and message not in warnings:
            warnings.append(message)

    def record(candidate, stage, reason, **extra):
        item = {
            'url': candidate['url'],
            'title': candidate.get('title', '')[:500],
            'stage': stage,
            'reason': reason[:1200],
        }
        item.update(extra)
        diagnostics.append(item)

    def persist_status(**kwargs):
        kwargs.setdefault('warnings', json.dumps(warnings, ensure_ascii=False))
        kwargs.setdefault('coverage', json.dumps(coverage, ensure_ascii=False))
        kwargs.setdefault('diagnostics', json.dumps(diagnostics, ensure_ascii=False))
        update_search(path, search_id, **kwargs)

    def process_candidate(search, candidate):
        """Обрабатывает одного кандидата в рабочем потоке.
        SQLite и общие списки здесь не изменяются.
        """
        local_warnings = []
        local_diagnostics = []

        def warn(message):
            if message and message not in local_warnings:
                local_warnings.append(message)

        def diag(stage, reason, **extra):
            item = {
                'url': candidate['url'],
                'title': candidate.get('title', '')[:500],
                'stage': stage,
                'reason': reason[:1200],
            }
            item.update(extra)
            local_diagnostics.append(item)

        url = candidate['url']
        pub = {
            'id': uuid.uuid4().hex,
            'search_id': search_id,
            'url': url,
            'title': (candidate.get('title') or '')[:500] or url,
            'source': urlsplit(url).hostname,
            'platform': platform_for(url),
            'published_at': None,
            'fetched_at': now(),
            'original_text': '',
            'snippet': candidate.get('snippet', '')[:3000],
            'fetch_status': 'unavailable',
            'fetch_note': '',
            'relevance': 'review',
            'reason': '',
            'evidence': '',
            'region_reason': '',
            'verification': 'insufficient',
            'included': 0,
            'content_hash': '',
        }

        outcome = {
            'pub': None,
            'resolved': None,
            'diagnostics': local_diagnostics,
            'warnings': local_warnings,
            'pending_social': None,
        }

        try:
            original = fetch_publication(url)
            resolved = normalize_url(original.pop('resolved_url', url))

            if not candidate_allowed(search, resolved):
                raise ValueError(
                    'Переадресация ведёт за пределы выбранных источников.'
                )

            pub['url'] = resolved
            pub.update(original)
            pub['fetch_status'] = 'received'
            outcome['resolved'] = resolved

        except Exception as exc:
            pub['fetch_note'] = (
                str(exc) if isinstance(exc, ValueError)
                else 'Источник не ответил или ограничил получение текста.'
            )
            pub['reason'] = (
                'Оригинал не получен. Сниппет поиска не подтверждает '
                'содержание публикации.'
            )

            if isinstance(exc, ssl.SSLError):
                reason = (
                    'Не удалось установить защищённое соединение '
                    'с сайтом (TLS). Содержание не проверено.'
                )
            elif isinstance(exc, (TimeoutError, socket.timeout)):
                reason = 'Сайт не ответил вовремя. Содержание не проверено.'
            elif isinstance(exc, ValueError):
                reason = str(exc)
            else:
                reason = (
                    'Не удалось загрузить оригинал ('
                    + type(exc).__name__ + '). Содержание не проверено.'
                )

            diag('fetch', reason)
            warn(
                'Не все оригиналы удалось получить. Доступность текста '
                'указана в карточках; неподходящие ссылки пропущены.'
            )

            # Сохраняем предусмотренную исходным кодом проверку сниппетов
            # социальных сетей, если оригинал получить не удалось.
            is_social = platform_for(url) != 'web'
            snippet_text = (
                candidate.get('title', '') + '\n'
                + candidate.get('snippet', '')
            ).strip()

            if (
                is_social
                and search['mode'] == 'by_thesis'
                and query_match(search['query'], snippet_text)
            ):
                if not search['fresh_only']:
                    try:
                        preliminary = analyze(config, search, {
                            'original_text': snippet_text,
                            'title': candidate.get('title', ''),
                            'content_source': 'search_snippet',
                        })
                        if preliminary.get('relevance') == 'irrelevant':
                            local_diagnostics[-1].update(
                                stage='analysis',
                                reason='По поисковому описанию: '
                                + preliminary.get('reason', ''),
                            )
                            return outcome
                    except ServiceError:
                        pass

                local_diagnostics[-1].update(
                    index_match=True,
                    snippet=candidate.get('snippet', '')[:3000],
                    platform=platform_for(url),
                )

                if not search['fresh_only']:
                    pub['reason'] = (
                        'Совпадение в поисковом описании. Оригинал и дата '
                        'не подтверждены; откройте источник для проверки.'
                    )
                    outcome['pending_social'] = pub

            elif (
                is_social
                and search['mode'] == 'compare'
                and not search['fresh_only']
            ):
                if len(snippet_text) >= 30:
                    try:
                        preliminary = analyze(config, search, {
                            'original_text': snippet_text,
                            'title': candidate.get('title', ''),
                            'content_source': 'search_snippet',
                        })

                        if preliminary.get('relevance') == 'relevant':
                            local_diagnostics.pop()

                            for field in (
                                'relevance', 'reason', 'evidence',
                                'region_reason', 'verification', 'emotion',
                                'emotion_reason', 'style', 'style_reason',
                                'difference', 'reference_evidence',
                            ):
                                if field in preliminary:
                                    pub[field] = preliminary[field]

                            pub['fetch_note'] = reason
                            pub['included'] = 1
                            outcome['pub'] = pub
                        else:
                            local_diagnostics[-1].update(
                                stage='analysis',
                                reason='По поисковому описанию: '
                                + preliminary.get('reason', ''),
                            )
                    except ServiceError:
                        pass

            return outcome

        # Оригинал успешно получен. Дубликаты проверяются в главном потоке.
        if (
            search['mode'] == 'compare'
            and outcome['resolved'] == search['reference'].get('url')
        ):
            diag('same_reference', 'Это сам эталон или его точная копия.')
            return outcome

        if pub['original_text']:
            date = pub['published_at']

            if date and not (
                search['date_from'] <= date[:10] <= search['date_to']
            ):
                diag('date', f'Дата публикации {date} вне выбранного периода.')
                return outcome

            if search['fresh_only'] and not fresh_at(
                date, search['created_at']
            ):
                diag(
                    'freshness',
                    'Публикация старше 24 часов либо точное время '
                    'с часовым поясом неизвестно.',
                )
                return outcome

            if search['mode'] == 'by_thesis':
                pub['exact_match'] = query_match(
                    search['query'],
                    pub['title'] + '\n' + pub['original_text'],
                )
                if not pub['exact_match']:
                    diag(
                        'exact_match',
                        'В оригинале нет всех ключевых слов или указанной '
                        f'в кавычках фразы из запроса «{search["query"]}».',
                    )
                    return outcome

            try:
                result = analyze(config, search, pub)

                for field in (
                    'relevance', 'reason', 'evidence', 'region_reason',
                    'verification', 'emotion', 'emotion_reason',
                    'style', 'style_reason', 'difference',
                    'reference_evidence',
                ):
                    if field in result:
                        pub[field] = result[field]

                if pub['relevance'] == 'irrelevant':
                    diag('analysis', pub['reason'])
                    return outcome

                if not date:
                    pub['reason'] = (
                        'Дата публикации не подтверждена. ' + pub['reason']
                    )

                pub['included'] = int(
                    pub['relevance'] == 'relevant' and bool(date)
                )

            except ServiceError as exc:
                pub['reason'] = str(exc)
                warn(str(exc))

                if search['mode'] == 'compare':
                    diag('analysis_error', str(exc))
                    return outcome

            if pub.get('exact_match'):
                pub['reason'] += (
                    f' Найденные слова: «{pub["exact_match"]}».'
                )

        outcome['pub'] = pub
        return outcome

    try:
        with connect(path) as db:
            claimed = db.execute(
                "UPDATE searches SET state='running' "
                "WHERE id=? AND state='queued'",
                (search_id,),
            ).rowcount

        if not claimed:
            return

        search = load_search(config, search_id)
        update_search(
            path, search_id,
            state='running',
            stage='Ищем публикации в Яндексе',
        )

        if search['mode'] == 'compare':
            update_search(
                path, search_id,
                stage='Получаем эталон и готовим сокращённую версию',
            )

            if search['query'].startswith(('https://', 'http://')):
                try:
                    reference = fetch_publication(
                        normalize_url(search['query'])
                    )
                    reference['url'] = normalize_url(
                        reference.pop('resolved_url', search['query'])
                    )
                except Exception as exc:
                    raise ServiceError(
                        'Не удалось получить текст эталона по ссылке. '
                        'Вставьте текст новости вручную.'
                    ) from exc
            else:
                reference = {
                    'original_text': search['query'],
                    'title': 'Исходный текст',
                    'url': '',
                    'published_at': None,
                }

            update_search(
                path, search_id,
                reference=json.dumps(reference, ensure_ascii=False),
            )
            reference.update(
                summarize_reference(config, reference['original_text'])
            )
            search['reference'] = reference
            update_search(
                path, search_id,
                reference=json.dumps(reference, ensure_ascii=False),
            )

        try:
            terms = search_terms(config, search)
        except ServiceError as exc:
            warning(str(exc))
            terms = [re.sub(r'\s+', ' ', search['query'])[:150]]

        queries = build_queries(search, terms)
        candidates = {}
        succeeded = 0
        result_limit = max(1, int(search['result_limit']))
        max_parallel = min(max_parallel, result_limit)
        limit = min(
            500,
            max(config['MAX_CANDIDATES'], result_limit),
        )
        limit = min(limit, config.get('HARD_CANDIDATE_LIMIT', 500))

        exhausted = set()
        pages = max(
            config['SEARCH_PAGES'],
            ceil(min(result_limit * 3, limit) / 20),
        )

        max_parallel_searches = max(1, min(int(os.getenv('MAX_PARALLEL_SEARCHES', '50')), 1000))

        with ThreadPoolExecutor(
            max_workers=max_parallel_searches,
            thread_name_prefix='yandex-search',
        ) as search_executor:
            for page in range(pages):
                page_queries = [
                    (query, label)
                    for query, label in queries
                    if query not in exhausted
                ]

                # Запускаем запросы страницы одновременно.
                futures = [
                    (
                        query,
                        label,
                        search_executor.submit(
                            web_search, config, query, page
                        ),
                    )
                    for query, label in page_queries
                ]

                # Обрабатываем ответы в исходном порядке запросов,
                # чтобы сохранить прежний приоритет метаданных кандидатов.
                for query, label, future in futures:
                    try:
                        results = future.result()
                        succeeded += 1

                        coverage.append({
                            'source': label,
                            'query': query.strip(),
                            'page': page + 1,
                            'status': 'ok',
                            'found': len(results),
                        })

                        for candidate in results:
                            try:
                                url = normalize_url(candidate['url'])
                            except (ValueError, KeyError, TypeError):
                                continue

                            if not candidate_allowed(search, url):
                                continue

                            candidate['url'] = url
                            existing = candidates.setdefault(url, candidate)

                            if label == 'Поиск с географией':
                                existing['location_hint'] = True
                                existing['location_priority'] = max(
                                    1,
                                    existing.get('location_priority', 0),
                                )
                            elif label.endswith('география и период'):
                                existing['location_priority'] = 2
                            elif label == 'Новости по теме и географии':
                                existing['location_priority'] = 3

                        if not results:
                            exhausted.add(query)

                    except ServiceError as exc:
                        warning(str(exc))
                        coverage.append({
                            'source': label,
                            'query': query.strip(),
                            'page': page + 1,
                            'status': 'error',
                            'found': 0,
                        })
                        exhausted.add(query)

                    finally:
                        persist_status()

                if len(candidates) >= limit:
                    break

        if not succeeded:
            update_search(
                path, search_id,
                state='failed',
                stage='Поиск не выполнен',
                finished_at=now(),
            )
            return

        selected = ordered_candidates(candidates, search, result_limit)

        if len(candidates) > limit:
            warning(
                f'Найдено {len(candidates)} ссылок; обработка ограничена '
                f'{result_limit} кандидатами с распределением между площадками.'
            )

        update_search(
            path, search_id,
            total=len(candidates),
            stage='Получаем оригиналы и проверяем содержание',
            progress=0,
        )

        seen_originals = set()
        saved = 0
        pending_social = []
        next_index = 0

        # Ограниченная очередь: не запускаем все кандидаты разом.
        # Одновременно выполняется не более max_parallel задач.
        with ThreadPoolExecutor(
            max_workers=max_parallel,
            thread_name_prefix='candidate',
        ) as executor:
            futures = {}

            while next_index < len(selected) and len(futures) < max_parallel:
                candidate = selected[next_index]
                future = executor.submit(
                    process_candidate, search, candidate
                )
                futures[future] = candidate
                next_index += 1

            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)

                for future in done:
                    candidate = futures.pop(future)
                    processed += 1

                    try:
                        outcome = future.result()
                    except Exception as exc:
                        outcome = {
                            'pub': None,
                            'resolved': None,
                            'diagnostics': [{
                                'url': candidate['url'],
                                'title': candidate.get('title', '')[:500],
                                'stage': 'processing',
                                'reason': (
                                    'Ошибка обработки кандидата: '
                                    + type(exc).__name__
                                ),
                            }],
                            'warnings': [],
                            'pending_social': None,
                        }
                        log.warning(
                            'Candidate failed url=%s error=%s',
                            candidate['url'],
                            type(exc).__name__,
                        )

                    for message in outcome['warnings']:
                        warning(message)

                    resolved = outcome['resolved']
                    duplicate = bool(
                        resolved and resolved in seen_originals
                    )

                    if resolved and not duplicate:
                        seen_originals.add(resolved)

                    if duplicate:
                        record(
                            candidate,
                            'duplicate',
                            'Повтор уже обработанной публикации.',
                        )
                    else:
                        diagnostics.extend(outcome['diagnostics'])

                        pub = outcome['pub']
                        if pub is not None and saved < result_limit:
                            with connect(path) as db:
                                fields = list(pub)
                                saved += db.execute(
                                    'INSERT OR IGNORE INTO publications '
                                    f"({','.join(fields)}) "
                                    f"VALUES ({','.join('?' for _ in fields)})",
                                    list(pub.values()),
                                ).rowcount

                        pending = outcome['pending_social']
                        if pending is not None:
                            pending_social.append(pending)

                    persist_status(
                        progress=processed,
                        stage='Получаем оригиналы и проверяем содержание',
                    )

                    if saved >= result_limit:
                        break

                if saved >= result_limit:
                    for future in futures:
                        future.cancel()
                    break

                # Освобождённые места заполняем следующими кандидатами.
                while (
                    next_index < len(selected)
                    and len(futures) < max_parallel
                ):
                    candidate = selected[next_index]
                    future = executor.submit(
                        process_candidate, search, candidate
                    )
                    futures[future] = candidate
                    next_index += 1

        # Добавляем карточки соцсетей без оригинала в пределах лимита.
        for pub in pending_social[:max(0, result_limit - saved)]:
            with connect(path) as db:
                fields = list(pub)
                saved += db.execute(
                    'INSERT OR IGNORE INTO publications '
                    f"({','.join(fields)}) "
                    f"VALUES ({','.join('?' for _ in fields)})",
                    list(pub.values()),
                ).rowcount

        if (
            saved == 0
            and processed
            and diagnostics
            and all(d['stage'] == 'fetch' for d in diagnostics)
        ):
            warning(
                'Яндекс нашёл ссылки, но тексты источников недоступны. '
                'Это не означает, что подходящих новостей нет. '
                'Причины и ссылки приведены ниже.'
            )

        if not saved:
            log.info(
                'empty_search id=%s mode=%s candidates=%s',
                search_id, search['mode'], len(candidates),
            )

        update_search(
            path, search_id,
            state='partial' if warnings else 'completed',
            stage='Подборка готова' if saved else 'Подходящих публикаций не найдено',
            finished_at=now(),
            progress=processed,
            warnings=json.dumps(warnings, ensure_ascii=False),
            diagnostics=json.dumps(diagnostics, ensure_ascii=False),
        )

    except Exception as exc:
        warning(
            str(exc) if isinstance(exc, ServiceError)
            else 'Обработка прервана. Уже полученные материалы сохранены; '
            'можно повторить поиск.'
        )
        log.exception('Search %s failed', search_id)
        update_search(
            path, search_id,
            state='failed',
            stage='Обработка прервана',
            warnings=json.dumps(warnings, ensure_ascii=False),
            diagnostics=json.dumps(diagnostics, ensure_ascii=False),
            finished_at=now(),
        )