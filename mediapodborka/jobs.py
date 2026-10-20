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
    """Главная фоновая задача: ищет в Яндексе, скачивает оригиналы, анализирует и сохраняет результаты."""
    path = config['DATABASE']
    warnings, coverage, diagnostics = [], [], []
    processed = 0

    def warning(message):
        """Добавляет предупреждение без повторов."""
        if message not in warnings:
            warnings.append(message)

    def record(candidate, stage, reason):
        """Записывает, почему ссылка не попала в подборку (для блока «Почему не вошли»)."""
        diagnostics.append({
            'url': candidate['url'],
            'title': candidate.get('title', '')[:500],
            'stage': stage,
            'reason': reason[:1200],
        })
        update_search(path, search_id, diagnostics=json.dumps(diagnostics, ensure_ascii=False))

    try:
        # «Захватываем» поиск один раз: повтор воркера не должен дублировать историю и платную работу
        with connect(path) as db:
            claimed = db.execute(
                "UPDATE searches SET state='running' WHERE id=? AND state='queued'", (search_id,)
            ).rowcount
        if not claimed:
            return

        search = load_search(config, search_id)
        update_search(path, search_id, state='running', stage='Ищем публикации в Яндексе')

        # ---- Режим сравнения: сначала получаем эталон и готовим его краткую версию ----
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

        # ---- Формулировки запросов ----
        try:
            terms = search_terms(config, search)
        except ServiceError as exc:
            warning(str(exc))
            # Запасной вариант: сам запрос, обрезанный до 150 символов
            terms = [re.sub(r'\s+', ' ', search['query'])[:150]]
        queries = build_queries(search, terms)

        candidates = {}
        succeeded = 0
        result_limit = search['result_limit']
        limit = min(500, max(config['MAX_CANDIDATES'], result_limit * 3))
        # Явный жёсткий бюджет для интеграционных проверок и развёртываний
        limit = min(limit, config.get('HARD_CANDIDATE_LIMIT', 500))

        # Опрашиваем все выбранные площадки, прежде чем перейти на следующую страницу выдачи
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
                            # Запоминаем, какие запросы с географией нашли эту ссылку
                            if label == 'Поиск с географией':
                                existing['location_hint'] = True
                                existing['location_priority'] = max(1, existing.get('location_priority', 0))
                            elif label.endswith('география и период'):
                                existing['location_priority'] = 2
                            elif label == 'Новости по теме и географии':
                                existing['location_priority'] = 3
                    # Пустая страница - дальше по этому запросу не идём
                    if not results:
                        exhausted.add(query)
                except ServiceError as exc:
                    warning(str(exc))
                    coverage.append({'source': label, 'page': page + 1, 'status': 'error', 'found': 0})
                    exhausted.add(query)
                finally:
                    update_search(
                        path, search_id,
                        coverage=json.dumps(coverage, ensure_ascii=False),
                        warnings=json.dumps(warnings, ensure_ascii=False),
                    )
            if len(candidates) >= limit:
                break

        # Ни один запрос не сработал - поиск не выполнен
        if not succeeded:
            update_search(path, search_id, state='failed', stage='Поиск не выполнен', finished_at=now())
            return

        selected = ordered_candidates(candidates, search, limit)
        if len(candidates) > limit:
            warning(f'Найдено {len(candidates)} ссылок; обработка ограничена {limit} кандидатами с распределением между площадками.')
        update_search(path, search_id, total=len(candidates), stage='Получаем оригиналы и проверяем содержание')

        seen_originals = set()   # уже обработанные итоговые адреса
        saved = 0                # сколько публикаций сохранено
        pending_social = []      # посты соцсетей, оригинал которых недоступен

        # ---- Основной цикл: обрабатываем кандидатов по одному ----
        for index, candidate in enumerate(selected):
            if saved >= result_limit:
                break
            update_search(path, search_id, progress=index + 1)
            processed = index + 1
            url = candidate['url']

            # Заготовка записи о публикации
            pub = {
                'id': uuid.uuid4().hex, 'search_id': search_id, 'url': url,
                'title': candidate['title'][:500] or url,
                'source': urlsplit(url).hostname, 'platform': platform_for(url),
                'published_at': None, 'fetched_at': now(),
                'original_text': '', 'snippet': candidate['snippet'][:3000],
                'fetch_status': 'unavailable', 'fetch_note': '',
                'relevance': 'review', 'reason': '', 'evidence': '', 'region_reason': '',
                'verification': 'insufficient', 'included': 0, 'content_hash': '',
            }

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
                # В режиме сравнения сам эталон в результаты не берём
                if search['mode'] == 'compare' and resolved == search['reference'].get('url'):
                    record(candidate, 'same_reference', 'Это сам эталон или его точная копия.')
                    continue

            except Exception as exc:
                # Оригинал получить не удалось
                pub['fetch_note'] = str(exc) if isinstance(exc, ValueError) else 'Источник не ответил или ограничил получение текста.'
                pub['reason'] = 'Оригинал не получен. Сниппет поиска не подтверждает содержание публикации.'
                warning('Не все оригиналы удалось получить. Доступность текста указана в карточках; неподходящие ссылки пропущены.')

                # Человеческое объяснение причины
                if isinstance(exc, ssl.SSLError):
                    reason = 'Не удалось установить защищённое соединение с сайтом (TLS). Содержание не проверено.'
                elif isinstance(exc, (TimeoutError, socket.timeout)):
                    reason = 'Сайт не ответил вовремя. Содержание не проверено.'
                elif isinstance(exc, ValueError):
                    reason = str(exc)
                else:
                    reason = 'Не удалось загрузить оригинал (' + type(exc).__name__ + '). Содержание не проверено.'
                record(candidate, 'fetch', reason)

                # Проиндексированные посты соцсетей кладём в общий список как карточки на проверку.
                # Добавляем их после оригиналов, чтобы они не съели лимит результатов.
                if platform_for(url) != 'web' and search['mode'] == 'by_thesis' and query_match(search['query'], candidate['title'] + '\n' + candidate['snippet']):
                    if not search['fresh_only']:
                        try:
                            preliminary = analyze(config, search, {
                                'original_text': candidate['title'] + '\n' + candidate['snippet'],
                                'title': candidate['title'],
                                'content_source': 'search_snippet',
                            })
                            if preliminary['relevance'] == 'irrelevant':
                                diagnostics[-1].update(stage='analysis', reason='По поисковому описанию: ' + preliminary['reason'])
                                update_search(path, search_id, diagnostics=json.dumps(diagnostics, ensure_ascii=False))
                                continue
                        except ServiceError:
                            pass  # Оставляем только явно непроверенную карточку
                    diagnostics[-1].update(index_match=True, snippet=candidate['snippet'][:3000], platform=platform_for(url))
                    update_search(path, search_id, diagnostics=json.dumps(diagnostics, ensure_ascii=False))
                    if not search['fresh_only']:
                        pub.update(
                            fetch_note=reason,
                            reason='Совпадение в поисковом описании. Оригинал и дата не подтверждены; откройте источник для проверки.',
                        )
                        pending_social.append(pub)
                elif platform_for(url) != 'web' and search['mode'] == 'compare' and not search['fresh_only']:
                    snippet_text = (candidate['title'] + '\n' + candidate['snippet']).strip()
                    if len(snippet_text) >= 30:
                        try:
                            preliminary = analyze(config, search, {
                                'original_text': snippet_text,
                                'title': candidate['title'],
                                'content_source': 'search_snippet',
                            })
                            # Если модель подтвердила, что сниппет очень схож с эталоном (relevant)
                            if preliminary.get('relevance') == 'relevant':
                                # 1. Убираем пост из списка ошибок ("ошибка не отдало текст")
                                diagnostics.pop()
                                update_search(path, search_id, diagnostics=json.dumps(diagnostics, ensure_ascii=False))

                                # 2. Переносим все поля анализа в карточку и ставим статус "прошёл" (included = 1)
                                for field in ('relevance', 'reason', 'evidence', 'region_reason', 'verification',
                                              'emotion', 'emotion_reason', 'style', 'style_reason',
                                              'difference', 'reference_evidence'):
                                    if field in preliminary:
                                        pub[field] = preliminary[field]
                                pub['fetch_note'] = reason
                                pub['included'] = 1

                                # 3. Сохраняем сразу в БД ко всем прошедшим сайтам
                                with connect(path) as db:
                                    fields = list(pub)
                                    saved += db.execute(
                                        f"INSERT OR IGNORE INTO publications ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                                        list(pub.values()),
                                    ).rowcount
                            else:
                                # Если не подошёл — оставляем в отброшенных с причиной от модели
                                diagnostics[-1].update(stage='analysis', reason='По поисковому описанию: ' + preliminary.get('reason', ''))
                                update_search(path, search_id, diagnostics=json.dumps(diagnostics, ensure_ascii=False))
                        except ServiceError:
                            pass
                continue


            # Оригинал получен - проверяем и анализируем
            if pub['original_text']:
                date = pub['published_at']

                # Дата вне выбранного периода
                if date and not (search['date_from'] <= date[:10] <= search['date_to']):
                    record(candidate, 'date', f'Дата публикации {date} вне выбранного периода.')
                    continue

                # Режим «Свежее» (24 часа)
                if search['fresh_only'] and not fresh_at(date, search['created_at']):
                    record(candidate, 'freshness', 'Публикация старше 24 часов либо точное время с часовым поясом неизвестно.')
                    continue

                # Для поиска по тезису: в тексте должны быть все ключевые слова
                if search['mode'] == 'by_thesis':
                    pub['exact_match'] = query_match(search['query'], pub['title'] + '\n' + pub['original_text'])
                    if not pub['exact_match']:
                        record(candidate, 'exact_match', f'В оригинале нет всех ключевых слов или указанной в кавычках фразы из запроса «{search["query"]}».')
                        continue

                # Анализ моделью
                try:
                    result = analyze(config, search, pub)
                    for field in ('relevance', 'reason', 'evidence', 'region_reason', 'verification',
                                  'emotion', 'emotion_reason', 'style', 'style_reason',
                                  'difference', 'reference_evidence'):
                        if field in result:
                            pub[field] = result[field]
                    if pub['relevance'] == 'irrelevant':
                        record(candidate, 'analysis', pub['reason'])
                        continue
                    if not date:
                        pub['reason'] = 'Дата публикации не подтверждена. ' + pub['reason']
                    # Релевантность описывает содержание. Неизвестная дата - отдельное ограничение:
                    # оставляем её видимой и требуем ручного выбора.
                    pub['included'] = int(pub['relevance'] == 'relevant' and bool(date))
                except ServiceError as exc:
                    pub['reason'] = str(exc)
                    warning(str(exc))
                    if search['mode'] == 'compare':
                        record(candidate, 'analysis_error', str(exc))
                        continue

                if pub.get('exact_match'):
                    pub['reason'] += f' Найденные слова: «{pub["exact_match"]}».'

            # Сохраняем публикацию в БД
            with connect(path) as db:
                fields = list(pub)
                saved += db.execute(
                    f"INSERT OR IGNORE INTO publications ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                    list(pub.values()),
                ).rowcount
            update_search(path, search_id, progress=index + 1, warnings=json.dumps(warnings, ensure_ascii=False))

        # Добавляем посты соцсетей без оригинала, пока не достигнут лимит
        for pub in pending_social[:max(0, result_limit - saved)]:
            with connect(path) as db:
                fields = list(pub)
                saved += db.execute(
                    f"INSERT OR IGNORE INTO publications ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                    list(pub.values()),
                ).rowcount

        # Все кандидаты отвалились на этапе скачивания
        if saved == 0 and processed and all(d['stage'] == 'fetch' for d in diagnostics):
            warning('Яндекс нашёл ссылки, но тексты источников недоступны. Это не означает, что подходящих новостей нет. Причины и ссылки приведены ниже.')
        if not saved:
            log.info('empty_search id=%s mode=%s candidates=%s', search_id, search['mode'], len(candidates))

        # Финальное состояние поиска
        update_search(
            path, search_id,
            state='partial' if warnings else 'completed',
            stage='Подборка готова' if saved else 'Подходящих публикаций не найдено',
            finished_at=now(),
            progress=processed,
            warnings=json.dumps(warnings, ensure_ascii=False),
        )

    except Exception as exc:
        # Любая непредвиденная ошибка: помечаем поиск как прерванный, ранее сохранённое остаётся
        warning(str(exc) if isinstance(exc, ServiceError) else 'Обработка прервана. Уже полученные материалы сохранены; можно повторить поиск.')
        log.warning('Search %s failed: %s', search_id, type(exc).__name__)
        update_search(
            path, search_id,
            state='failed',
            stage='Обработка прервана',
            warnings=json.dumps(warnings, ensure_ascii=False),
            finished_at=now(),
        )

        