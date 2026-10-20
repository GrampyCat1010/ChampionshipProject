import json
import os
import secrets
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request, send_file, session
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from werkzeug.exceptions import HTTPException

from .content import normalize_url, platform_for
from .db import connect, initialize, now
from .jobs import JobRunner
from .locations import DEFAULT_REGION, registry, validate_location
from .filters import fresh_at, result_groups
from .matching import TOKEN

# Корень проекта (папка выше пакета mediapodborka)
ROOT = Path(__file__).resolve().parent.parent

# Названия площадок и состояний поиска для интерфейса и Excel
PLATFORMS = {'vk': 'ВКонтакте', 'ok': 'Одноклассники', 'max': 'MAX', 'web': 'Сайты и СМИ'}
STATES = {
    'queued': 'В очереди',
    'running': 'Выполняется',
    'completed': 'Завершён',
    'partial': 'Частичный результат',
    'failed': 'Ошибка',
    'interrupted': 'Прерван',
}


def create_app(test_config=None):
    """Создаёт и настраивает Flask-приложение."""
    load_dotenv(ROOT / '.env')
    app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))

    # ---- Путь к базе данных ----
    db_path = Path(os.getenv('DATABASE_PATH', 'instance/mediapodborka.sqlite3'))
    if not db_path.is_absolute():
        db_path = ROOT / db_path

    # ---- Секретный ключ сессий: из .env либо генерируется и сохраняется в instance/session.key ----
    secret = os.getenv('SECRET_KEY')
    if not secret:
        secret_path = ROOT / 'instance' / 'session.key'
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        if not secret_path.exists():
            secret_path.write_text(secrets.token_hex(32), encoding='utf-8')
        secret = secret_path.read_text(encoding='utf-8').strip()

    # ---- Настройки приложения ----
    app.config.update(
        SECRET_KEY=secret,
        DATABASE=str(db_path),
        MAX_CONTENT_LENGTH=120_000,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', 'false').lower() == 'true',
        PERMANENT_SESSION_LIFETIME=timedelta(days=180),
        YANDEX_CLOUD_FOLDER=os.getenv('YANDEX_CLOUD_FOLDER', ''),
        YANDEX_CLOUD_API_KEY=os.getenv('YANDEX_CLOUD_API_KEY', ''),
        YANDEX_CLOUD_MODEL=os.getenv('YANDEX_CLOUD_MODEL', 'yandexgpt-5-pro/latest'),
        YANDEX_SEARCH_API_KEY=os.getenv('YANDEX_SEARCH_API_KEY', ''),
        MAX_CANDIDATES=max(1, min(int(os.getenv('MAX_CANDIDATES', '300')), 500)),
        SEARCH_PAGES=max(1, min(int(os.getenv('SEARCH_PAGES', '1')), 3)),
    )
    if test_config:
        app.config.update(test_config)

    initialize(app.config['DATABASE'])

    # После перезапуска сервера незавершённые поиски помечаем как прерванные
    if not app.testing:
        with connect(app.config['DATABASE']) as db:
            db.execute(
                "UPDATE searches SET state='interrupted', stage='Сервер перезапущен. Повторите поиск.', finished_at=? "
                "WHERE state IN ('queued','running')",
                (now(),),
            )

    runner = JobRunner(dict(app.config))
    app.extensions['jobs'] = runner

    # ------------------------------------------------------------------
    # Служебные обработчики (сессия, заголовки безопасности, ошибки)
    # ------------------------------------------------------------------

    @app.before_request
    def session_guard():
        """Единое пространство для всех пользователей и проверка CSRF-токена."""
        session['owner'] = 'shared'  # Один общий владелец для всех браузеров
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
            session.permanent = True
        if request.method in ('POST', 'PATCH', 'PUT', 'DELETE'):
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), session['csrf']):
                abort(403, 'Обновите страницу и повторите действие.')

                

    @app.after_request
    def security_headers(response):
        """Добавляет заголовки безопасности и отключает кэш для API."""
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.path.startswith('/api/') or request.path == '/':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(_):
        # Нарушение уникальности (owner, url) в таблице sources
        return jsonify(error='Такой источник уже добавлен.'), 409

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    # ------------------------------------------------------------------
    # Вспомогательные функции
    # ------------------------------------------------------------------

    def body():
        """Тело запроса: должно быть JSON-объектом."""
        value = request.get_json()
        if not isinstance(value, dict):
            abort(400, 'Ожидается объект JSON.')
        return value

    def text_field(data, name, minimum=1, maximum=200):
        """Берёт строковое поле из JSON и проверяет длину."""
        value = data.get(name)
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
            abort(400, f'Поле «{name}» должно содержать от {minimum} до {maximum} символов.')
        return value.strip()

    def owned_search(search_id):
        """Возвращает поиск, принадлежащий текущей сессии, с развёрнутыми JSON-полями."""
        with connect(app.config['DATABASE']) as db:
            row = db.execute(
                'SELECT * FROM searches WHERE id=? AND owner=?', (search_id, session['owner'])
            ).fetchone()
        if not row:
            abort(404, 'Поиск не найден в этой сессии.')
        result = dict(row)
        result.pop('owner')  # идентификатор владельца наружу не отдаём
        for field in ('platforms', 'sources', 'warnings', 'coverage', 'diagnostics'):
            result[field] = json.loads(result[field])
        result['reference'] = json.loads(result['reference'])
        result['limit'] = result['result_limit']
        result['fresh_only'] = bool(result['fresh_only'])
        return result

    # ------------------------------------------------------------------
    # Страницы и справочные API
    # ------------------------------------------------------------------

    @app.get('/')
    def index():
        return render_template('index.html', csrf=session['csrf'])

    @app.get('/api/status')
    def status():
        """Показывает, подключены ли Яндекс Поиск и YandexGPT, и действующие лимиты."""
        return jsonify(
            search_configured=bool(
                (app.config['YANDEX_SEARCH_API_KEY'] or app.config['YANDEX_CLOUD_API_KEY'])
                and app.config['YANDEX_CLOUD_FOLDER']
            ),
            ai_configured=bool(app.config['YANDEX_CLOUD_API_KEY'] and app.config['YANDEX_CLOUD_FOLDER']),
            max_candidates=app.config['MAX_CANDIDATES'],
            search_pages=app.config['SEARCH_PAGES'],
            max_results=100,
            region=DEFAULT_REGION,
        )

    @app.get('/api/locations')
    def locations():
        """Справочник городов для подсказок в форме."""
        return jsonify(locations=registry())

    # ------------------------------------------------------------------
    # Источники пользователя
    # ------------------------------------------------------------------

    @app.get('/api/sources')
    def sources():
        with connect(app.config['DATABASE']) as db:
            rows = db.execute(
                'SELECT id,name,url,platform,region,created_at FROM sources WHERE owner=? ORDER BY created_at DESC',
                (session['owner'],),
            ).fetchall()
        return jsonify(sources=[dict(row) for row in rows])

    def source_values(data):
        """Проверяет поля источника; возвращает (название, ссылка, площадка, регион)."""
        url = normalize_url(text_field(data, 'url', maximum=220))
        return text_field(data, 'name', maximum=100), url, platform_for(url), text_field(data, 'region', maximum=80)

    @app.post('/api/sources')
    def add_source():
        values = source_values(body())
        source_id = uuid.uuid4().hex
        with connect(app.config['DATABASE']) as db:
            count = db.execute('SELECT COUNT(*) FROM sources WHERE owner=?', (session['owner'],)).fetchone()[0]
            if count >= 100:
                abort(400, 'Достигнут лимит 100 источников.')
            db.execute('INSERT INTO sources VALUES (?,?,?,?,?,?,?)', (source_id, session['owner'], *values, now()))
        return jsonify(id=source_id), 201

    @app.put('/api/sources/<source_id>')
    def edit_source(source_id):
        values = source_values(body())
        with connect(app.config['DATABASE']) as db:
            cursor = db.execute(
                'UPDATE sources SET name=?,url=?,platform=?,region=? WHERE id=? AND owner=?',
                (*values, source_id, session['owner']),
            )
            if not cursor.rowcount:
                abort(404, 'Источник не найден.')
        return jsonify(ok=True)

    @app.delete('/api/sources/<source_id>')
    def delete_source(source_id):
        with connect(app.config['DATABASE']) as db:
            db.execute('DELETE FROM sources WHERE id=? AND owner=?', (source_id, session['owner']))
        return jsonify(ok=True)

    # ------------------------------------------------------------------
    # Запуск поиска
    # ------------------------------------------------------------------

    def start_search(data):
        """Проверяет все условия поиска, сохраняет поиск в БД и ставит его в очередь.
        Возвращает идентификатор поиска."""
        query = text_field(data, 'query', minimum=3, maximum=12000)

        # Регион и город (регион по умолчанию - Красноярский край)
        raw_region = text_field({**data, 'region': data.get('region', '')}, 'region', minimum=0, maximum=80)
        region = raw_region or DEFAULT_REGION
        city = text_field({**data, 'city': data.get('city', '')}, 'city', minimum=0, maximum=100)
        region, city = validate_location(region, city)

        # «Свежее» и количество записей
        fresh_only = data.get('fresh_only', False)
        result_limit = data.get('limit', 20)
        if type(fresh_only) is not bool:
            abort(400, 'Поле «Свежее» должно быть логическим значением.')
        if type(result_limit) is not int or not 1 <= result_limit <= 100:
            abort(400, 'Количество записей должно быть целым числом от 1 до 100.')

        # Период поиска
        date_from, date_to = text_field(data, 'date_from', maximum=10), text_field(data, 'date_to', maximum=10)
        try:
            start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except ValueError:
            abort(400, 'Укажите корректные даты периода.')
        if start > end:
            abort(400, 'Начало периода не может быть позже окончания.')
        date_from, date_to = start.isoformat(), end.isoformat()
        if (end - start).days > 366:
            abort(400, 'Выберите период не более одного года.')

        # Площадки
        platforms = data.get('platforms', [])
        if (not isinstance(platforms, list) or not platforms
                or any(not isinstance(p, str) or p not in PLATFORMS for p in platforms)
                or len(platforms) > 4):
            abort(400, 'Выберите хотя бы одну площадку.')
        platforms = list(dict.fromkeys(platforms))

        # Режим и область поиска
        mode, query_type = data.get('mode', 'by_thesis'), data.get('query_type', 'topic')
        scope = data.get('scope', 'extended')
        # Совместимость со старыми клиентами: раньше mode означал область поиска
        if mode in ('extended', 'sources'):
            scope, mode = mode, 'compare' if query_type == 'article' else 'by_thesis'
        if mode not in ('by_thesis', 'compare') or scope not in ('extended', 'sources') or query_type not in ('topic', 'article'):
            abort(400, 'Некорректный режим поиска.')
        query_type = 'article' if mode == 'compare' else 'topic'

        if mode == 'by_thesis':
            if len(query) > 200 or not TOKEN.search(query):
                abort(400, 'Введите поисковый запрос до 200 символов. Для полного текста новости выберите сравнение.')
        elif query.startswith(('http://', 'https://')):
            normalize_url(query)

        # Выбранные источники
        source_ids = data.get('source_ids', [])
        if not isinstance(source_ids, list) or len(source_ids) > 20 or any(not isinstance(s, str) for s in source_ids):
            abort(400, 'Выберите не больше 20 источников за один запуск.')

        with connect(app.config['DATABASE']) as db:
            source_rows = db.execute(
                'SELECT id,name,url,platform,region FROM sources WHERE owner=?', (session['owner'],)
            ).fetchall()
            selected = [dict(s) for s in source_rows if s['id'] in source_ids and s['platform'] in platforms]
            if scope == 'sources' and not selected:
                abort(400, 'Добавьте и выберите источники для выбранных площадок.')
            if scope == 'sources' and len(selected) != len(set(source_ids)):
                abort(400, 'Некоторые источники недоступны или не соответствуют выбранным площадкам.')

            # Не больше двух одновременных поисков на пользователя
            active = db.execute(
                "SELECT COUNT(*) FROM searches WHERE owner=? AND state IN ('queued','running')",
                (session['owner'],),
            ).fetchone()[0]
            if active >= 2:
                abort(429, 'Дождитесь завершения текущих поисков.')

            # Лимит запусков в час
            since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            recent = db.execute(
                'SELECT COUNT(*) FROM searches WHERE owner=? AND created_at>?', (session['owner'], since)
            ).fetchone()[0]
            if recent >= 999:
                abort(429, 'Лимит 999 поисков в час достигнут. Повторите позже.')

            search_id = uuid.uuid4().hex
            db.execute(
                '''INSERT INTO searches
                (id,owner,query,query_type,date_from,date_to,region,mode,platforms,sources,created_at,
                 scope,city,fresh_only,result_limit,region_defaulted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (search_id, session['owner'], query, query_type, date_from, date_to, region, mode,
                 json.dumps(platforms), json.dumps(selected, ensure_ascii=False), now(), scope, city,
                 int(fresh_only), result_limit, int(not raw_region)),
            )

        # Ставим в очередь; если очередь полна - помечаем поиск как неудавшийся
        if not runner.submit(search_id):
            with connect(app.config['DATABASE']) as db:
                db.execute(
                    "UPDATE searches SET state='failed', stage='Очередь заполнена', warnings=? WHERE id=?",
                    (json.dumps(['Сервис занят. Повторите поиск позже.'], ensure_ascii=False), search_id),
                )
            abort(429, 'Очередь заполнена. Повторите поиск позже.')
        return search_id

    @app.post('/api/searches')
    def create_search():
        return jsonify(id=start_search(body())), 202

    @app.post('/api/searches/<search_id>/repeat')
    def repeat_search(search_id):
        """Повторяет поиск с теми же условиями."""
        search = owned_search(search_id)
        search['source_ids'] = [source['id'] for source in search['sources']]
        return jsonify(id=start_search(search)), 202

    @app.get('/api/searches')
    def history():
        """История поисков текущего пользователя (до 100 последних)."""
        with connect(app.config['DATABASE']) as db:
            rows = db.execute('''SELECT s.id,s.query,s.date_from,s.date_to,s.state,s.created_at,s.mode,
                s.scope,s.region,s.city,s.fresh_only,s.result_limit AS "limit",h.results_count,
                (SELECT COUNT(*) FROM publications p WHERE p.search_id=s.id) AS total,
                (SELECT COUNT(*) FROM publications p WHERE p.search_id=s.id AND p.included=1) AS included
                FROM searches s JOIN search_history h ON h.id=s.id
                WHERE h.user_id=? ORDER BY s.created_at DESC LIMIT 100''', (session['owner'],)).fetchall()
        return jsonify(searches=[dict(row) for row in rows])

    @app.get('/api/searches/<search_id>')
    def get_search(search_id):
        """Поиск вместе со всеми его публикациями (фронтенд опрашивает этот адрес)."""
        search = owned_search(search_id)
        with connect(app.config['DATABASE']) as db:
            rows = db.execute(
                'SELECT * FROM publications WHERE search_id=? ORDER BY published_at DESC, fetched_at', (search_id,)
            ).fetchall()
        pubs = [dict(r) for r in rows]

        # Считаем размещения: одинаковый текст в нескольких источниках = несколько размещений
        hashes = {}
        for pub in pubs:
            if pub['content_hash']:
                hashes[pub['content_hash']] = hashes.get(pub['content_hash'], 0) + 1
        for pub in pubs:
            pub['placements'] = hashes.get(pub['content_hash'], 1)
            pub['fresh'] = fresh_at(pub['published_at'], search['created_at'])

        return jsonify(
            search=search,
            publications=pubs,
            grouped=len(pubs) > 50,
            groups=result_groups(pubs) if len(pubs) > 50 else [],
        )

    @app.patch('/api/searches/<search_id>/publications/<publication_id>')
    def select_publication(search_id, publication_id):
        """Включает/исключает публикацию из выгрузки в Excel."""
        owned_search(search_id)
        included = body().get('included')
        if not isinstance(included, bool):
            abort(400, 'Укажите включение публикации.')
        with connect(app.config['DATABASE']) as db:
            cursor = db.execute(
                'UPDATE publications SET included=? WHERE id=? AND search_id=?',
                (int(included), publication_id, search_id),
            )
            if not cursor.rowcount:
                abort(404, 'Публикация не найдена.')
        return jsonify(ok=True)

    # ------------------------------------------------------------------
    # Выгрузка в Excel
    # ------------------------------------------------------------------

    @app.get('/api/searches/<search_id>/export')
    def export(search_id):
        """Формирует Excel-файл с отмеченными публикациями и листом «Условия поиска»."""
        search = owned_search(search_id)
        with connect(app.config['DATABASE']) as db:
            pubs = [dict(r) for r in db.execute(
                'SELECT * FROM publications WHERE search_id=? AND included=1 ORDER BY published_at DESC,fetched_at',
                (search_id,),
            )]
        if not pubs:
            abort(400, 'Сначала выберите публикации для выгрузки.')

        wb = Workbook()
        ws = wb.active
        ws.title = 'Публикации'

        # Заголовки таблицы
        headers = ['№', 'Источник', 'Площадка', 'Заголовок', 'Дата публикации', 'Выдержка', 'Прямая ссылка',
                   'Отбор', 'Почему найдено', 'Оригинал', 'Связь с регионом', 'Получено', 'Истинность утверждений']
        ws.append(headers)
        relevance = {'relevant': 'Связанные', 'indirect': 'Косвенно', 'review': 'Неоднозначное', 'irrelevant': 'Не подходит'}
        headers.extend(['Найденные слова', 'Эмоция', 'Обоснование эмоции', 'Стиль', 'Обоснование стиля', 'Отличие от эталона'])
        for column, header in enumerate(headers, 1):
            ws.cell(1, column, header)

        def safe(value):
            """Защита от формул Excel: убирает управляющие символы и экранирует = + - @ в начале."""
            if isinstance(value, str):
                value = ''.join(c for c in value if c in '\n\t\r' or ord(c) >= 32)[:32000]
                if value.lstrip().startswith(('=', '+', '-', '@')):
                    value = "'" + value
            return value

        for i, pub in enumerate(pubs, 1):
            ws.append([safe(v) for v in [
                i,
                pub['source'],
                PLATFORMS[pub['platform']],
                pub['title'],
                pub['published_at'] or 'Дата неизвестна',
                pub['evidence'] or pub['original_text'][:800] or ('Сниппет поиска: ' + pub['snippet']),
                pub['url'],
                relevance[pub['relevance']],
                pub['reason'],
                'Получен' if pub['fetch_status'] == 'received' else 'Не получен',
                pub['region_reason'],
                pub['fetched_at'],
                {'matches': 'Совпадает с эталоном', 'distorted': 'Есть расхождения с эталоном', 'unclear': 'Недостаточно данных'}.get(pub['verification'], 'Независимая проверка не проводилась'),
                pub['exact_match'],
                pub['emotion'],
                pub['emotion_reason'],
                pub['style'],
                pub['style_reason'],
                pub['difference'],
            ]])
            # Ссылка в 7-й колонке кликабельна
            ws.cell(i + 1, 7).hyperlink = pub['url']
            ws.cell(i + 1, 7).style = 'Hyperlink'

        ws.freeze_panes = 'A2'
        ws.auto_filter.ref = ws.dimensions
        widths = [7, 28, 20, 55, 20, 70, 50, 24, 55, 20, 45, 28, 40]
        for i, width in enumerate(widths, 1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = width

        # Второй лист: параметры поиска
        params = wb.create_sheet('Условия поиска')
        params.append(['Параметр', 'Значение'])
        strochki = [
            ('Запрос', search['query']),
            ('Тип', 'Исходная новость' if search['query_type'] == 'article' else 'Тема / событие / человек'),
            ('Период', f"{search['date_from']} — {search['date_to']}"),
            ('Регион', search['region']),
            ('Режим', 'Сравнение с оригиналом' if search['mode'] == 'compare' else 'Поиск по тезису'),
            ('Область поиска', 'По моим источникам' if search['scope'] == 'sources' else 'Расширенный'),
            ('Город', search['city']),
            ('Свежее', 'Да' if search['fresh_only'] else 'Нет'),
            ('Количество записей', search['limit']),
            ('Площадки', ', '.join(PLATFORMS[p] for p in search['platforms'])),
            ('Источники', '\n'.join(s['name'] + ': ' + s['url'] for s in search['sources']) or 'Без ограничения списком'),
            ('Выбрано публикаций', len(pubs)),
            ('Состояние', STATES[search['state']]),
            ('Запущен', search['created_at']),
            ('Выгружен', now()),
            ('Ограничения', '\n'.join(search['warnings']) or 'Нет ошибок выполнения.'),
            ('Покрытие', 'Индекс Яндекса; полный охват соцсетей не гарантируется.'),
            ('Выполненные запросы', json.dumps(search['coverage'], ensure_ascii=False)),
        ]
        for stroka in strochki:
            params.append([safe(v) for v in stroka])
        params.column_dimensions['A'].width = 30
        params.column_dimensions['B'].width = 100

        # Оформление: красная шапка, перенос текста, выравнивание по верху
        for sheet in wb:
            for cell in sheet[1]:
                cell.fill = PatternFill('solid', fgColor='BB2835')
                cell.font = Font(color='FFFFFF', bold=True)
            for row in sheet.iter_rows(min_row=2):
                for cell in row:
                    cell.alignment = Alignment(vertical='top', wrap_text=True)

        output = BytesIO()
        wb.save(output)
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'mediapodborka-{search_id[:8]}.xlsx',
        )

    return app

