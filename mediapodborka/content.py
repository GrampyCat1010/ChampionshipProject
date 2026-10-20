"""Получение оригиналов публикаций: фиксация DNS, ограничение размера, осторожное извлечение текста."""
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote

from bs4 import BeautifulSoup
import certifi

# Максимальный размер страницы - 2 МБ
MAX_BYTES = 2_000_000
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MediaPodborka/1.0'
)
# Домены соцсетей, по которым определяем площадку
DOMAINS = {
    'vk': ('vk.com', 'vk.ru', 'vkvideo.ru'),
    'ok': ('ok.ru',),
    'max': ('max.ru',),
}


def platform_for(url):
    """Определяет площадку по адресу: vk / ok / max, иначе web."""
    host = (urlsplit(url).hostname or '').lower()
    for platform, domains in DOMAINS.items():
        if any(host == d or host.endswith('.' + d) for d in domains):
            return platform
    return 'web'


def normalize_url(url):
    """Проверяет ссылку и приводит к каноническому виду.
    Бросает ValueError, если ссылка не публичная или некорректная."""
    try:
        parts = urlsplit(url.strip())
        # Только http(s), без логина/пароля и нестандартных портов
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError
        if parts.port not in (None, 80, 443):
            raise ValueError

        host = parts.hostname.lower().encode('idna').decode('ascii')
        if host == 'localhost' or '.' not in host or '%' in host or ':' in host:
            raise ValueError

        # Если хост - IP-адрес, он обязан быть публичным
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError
        except ValueError as exc:
            if re.fullmatch(r'[0-9.]+', host):
                raise ValueError('Разрешены только публичные адреса.') from exc

        # У соцсетей убираем www. и m.; vk.ru приводим к vk.com
        host = re.sub(r'^(www\.|m\.)', '', host) if platform_for(url) != 'web' else host
        if host == 'vk.ru':
            host = 'vk.com'

        # Выбрасываем рекламные метки
        query = [
            (k, v) for k, v in parse_qsl(parts.query)
            if not k.lower().startswith('utm_') and k not in ('fbclid', 'gclid', 'ref', 'from')
        ]
        path = parts.path.rstrip('/') or '/'

        # ВКонтакте оборачивает прямые ссылки на посты в ?w=wall-123_456
        wall = dict(query).get('w', '')
        if host == 'vk.com' and re.fullmatch(r'wall-?\d+_\d+', wall):
            path, query = '/' + wall, []

        return urlunsplit((parts.scheme, host, path, urlencode(sorted(query)), ''))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Укажите публичную ссылку http:// или https:// без логина и нестандартного порта.') from exc


def source_matches(url, source_url):
    """Проверяет, что адрес принадлежит выбранному источнику (тот же сайт и путь)."""
    u, s = urlsplit(normalize_url(url)), urlsplit(normalize_url(source_url))
    if u.hostname != s.hostname:
        return False
    p, base = unquote(u.path).rstrip('/'), unquote(s.path).rstrip('/')
    # Источник - весь сайт
    if not base:
        return True
    if p == base or p.startswith(base + '/'):
        return True
    # Для групп VK вида /club123 посты лежат по адресу /wall-123_456
    group = re.fullmatch(r'/(?:club|public)(\d+)', base)
    return bool(group and re.match(r'/wall-' + group[1] + r'_\d+$', p))


def public_addresses(host, port):
    """Возвращает IP-адреса хоста; если хоть один внутренний - бросает ошибку (защита от SSRF)."""
    addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('Внутренние сетевые адреса недоступны для сбора.')
    return addresses


def fetch_html(url):
    """Скачивает страницу вручную, следуя не более чем 5 переадресациям.
    Возвращает (байты страницы, итоговый адрес)."""
    normalize_url(url)
    current = url
    for _ in range(5):
        parts = urlsplit(current)
        port = 443 if parts.scheme == 'https' else 80

        # Подключаемся сразу к проверенному IP; TLS всё равно проверяет исходное имя хоста
        addresses = public_addresses(parts.hostname, port)
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=15)
        sock = socket.create_connection((addresses[0], port), timeout=15)
        try:
            if parts.scheme == 'https':
                context = ssl.create_default_context(cafile=certifi.where())
                sock = context.wrap_socket(sock, server_hostname=parts.hostname)
            conn.sock = sock

            target = urlunsplit(('', '', parts.path or '/', parts.query, ''))
            conn.request('GET', target, headers={
                'User-Agent': USER_AGENT,
                'Accept': 'text/html',
                'Accept-Encoding': 'identity',
                'Accept-Language': 'ru,en;q=0.8',
            })
            response = conn.getresponse()

            # Переадресация: идём по новому адресу
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Переадресация без адреса.')
                redirected = urljoin(current, location)
                normalize_url(redirected)
                # Завершающий слэш сервера сохраняем при загрузке. Каноническим делается
                # только сохраняемый адрес, иначе редиректы на слэш зациклятся.
                current = redirected
                continue

            if response.status != 200:
                raise ValueError(f'Источник вернул HTTP {response.status}.')
            content_type = response.getheader('Content-Type', '')
            if 'html' not in content_type.lower():
                raise ValueError('Источник не вернул HTML-публикацию.')

            content = response.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise ValueError('Страница превышает ограничение 2 МБ.')
            return content, current
        finally:
            conn.close()
            sock.close()
    raise ValueError('Слишком много переадресаций.')


def parse_date(value):
    """Разбирает дату ISO; для даты без времени возвращает только день."""
    if not isinstance(value, str):
        return None
    try:
        value = value.strip().replace('Z', '+00:00')
        parsed = datetime.fromisoformat(value)
        return parsed.date().isoformat() if len(value) == 10 else parsed.isoformat()
    except (ValueError, TypeError):
        return None


def extract(html, url):
    """Достаёт из HTML заголовок, дату и основной текст публикации."""
    soup = BeautifulSoup(html, 'html.parser')

    def meta(*names):
        """Первое найденное значение из meta-тегов с указанными именами."""
        for name in names:
            el = soup.find('meta', attrs={'property': name}) or soup.find('meta', attrs={'name': name})
            if el and el.get('content'):
                return el['content'].strip()
        return ''

    title = meta('og:title') or (soup.title.get_text(' ', strip=True) if soup.title else '')
    published = parse_date(meta('article:published_time', 'datePublished', 'pubdate'))
    bodies = []

    # Разбираем микроразметку JSON-LD (дата и текст статьи)
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes[:50]:
            if not isinstance(node, dict):
                continue
            for item in [node, *node.get('@graph', [])] if isinstance(node.get('@graph', []), list) else [node]:
                if not isinstance(item, dict):
                    continue
                types = item.get('@type', [])
                if isinstance(types, str):
                    types = [types]
                if any(t in ('NewsArticle', 'Article', 'BlogPosting', 'SocialMediaPosting') for t in types):
                    published = published or parse_date(item.get('datePublished'))
                    if isinstance(item.get('articleBody'), str):
                        bodies.append(item['articleBody'])

    # Дата из тега <time>
    time_el = soup.find('time', datetime=True)
    if time_el:
        published = published or parse_date(time_el['datetime'])

    # Дата из schema.org itemprop
    date_el = soup.select_one('[itemprop="datePublished"]')
    if date_el:
        published = published or parse_date(
            date_el.get('content') or date_el.get('datetime') or date_el.get_text(strip=True)
        )

    # Дата публикации Newslab лежит в отдельном элементе метаданных,
    # а не в произвольной дате внутри текста (которая может относиться к событию).
    local_date = soup.select_one('.di3-meta__date')
    if not published and local_date:
        try:
            published = datetime.strptime(local_date.get_text(strip=True), '%d.%m.%Y').date().isoformat()
        except ValueError:
            pass

    # ASP.NET-сайты оборачивают весь документ в форму (например, dela.ru).
    # Удаление формы уничтожит статью, поэтому убираем элементы управления, а не обёртки.
    for el in soup.select('script, style, nav, footer, noscript, aside, input, button, select, textarea'):
        el.decompose()

    # Явные контейнеры текста издателя важнее общих карточек <article>.
    # На странице может быть много статей: первая может оказаться рекомендацией.
    selectors = (
        '.journal-detail-page__main-content', '#samcontentonly', '[itemprop="articleBody"]',
        '.wall_post_text', '.media-text_cnt', '.post-text', '.js-mediator-article',
        '.article__body', '.article-body', '.article-content', '.entry-content',
        '.news-text', '.news__text', '.wcol.article', '.news-detail',
    )
    article = None
    for selector in selectors:
        nodes = [node for node in soup.select(selector) if len(node.get_text(' ', strip=True)) >= 100]
        if nodes:
            article = max(nodes, key=lambda node: len(node.get_text(' ', strip=True)))
            break
    if article is None:
        nodes = [node for node in soup.select('main article, article') if len(node.get_text(' ', strip=True)) >= 100]
        if nodes:
            article = max(nodes, key=lambda node: len(node.get_text(' ', strip=True)))

    # Настоящий текст статьи важнее посторонних рекомендаций из JSON-LD
    text = article.get_text('\n', strip=True) if article else max(bodies, key=len) if bodies else ''

    # og:description, сниппет поиска или страница входа не должны становиться «оригиналом»
    platform = platform_for(url)
    if platform == 'max':
        # Страница канала описывает канал, а не публикацию.
        # Только прямая ссылка /канал/пост содержит нужный контейнер сообщения.
        path = urlsplit(url).path.strip('/').split('/')
        message = soup.select_one('.card .info .description') if len(path) == 2 else None
        if not message:
            raise ValueError('Это страница канала MAX без текста отдельного поста. Нужна прямая ссылка на публикацию.')
        text = message.get_text('\n', strip=True)
        title = text.split('\n', 1)[0][:200]

    # Слишком короткий текст - значит, полный текст не получен
    if len(text.strip()) < (30 if platform != 'web' else 100):
        if platform == 'vk':
            raise ValueError('VK не отдал текст поста в публичном HTML (динамическая страница или требуется вход). Ссылка из индекса сохранена для ручного открытия.')
        raise ValueError('Полный текст публикации не выделен. Возможны авторизация или динамическая страница.')

    # Страницы капчи / проверки браузера
    if re.search(r'captcha|access denied|проверка браузера|подтвердите.{0,20}не робот', title, re.I):
        raise ValueError('Источник показывает страницу проверки доступа.')

    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if len(text) > 60000:
        raise ValueError('Текст превышает 60 000 символов. Вставьте нужный фрагмент; оригинал не будет молча обрезан.')

    return {
        'title': title[:500] or text[:100],
        'original_text': text,
        'published_at': published,
        'source': meta('og:site_name')[:200] or urlsplit(url).hostname,
        # Хэш нужен, чтобы считать одинаковые тексты размещениями
        'content_hash': hashlib.sha256(re.sub(r'\s+', ' ', text).encode()).hexdigest(),
    }


def fetch_publication(url):
    """Скачивает и разбирает публикацию. Возвращает словарь с текстом, датой и итоговым адресом."""
    html, final_url = fetch_html(url)

    # Редирект на главную или страницу входа - это не запрошенный оригинал
    before, after = urlsplit(url), urlsplit(final_url)
    if before.path not in ('', '/') and (after.path in ('', '/') or re.search(r'/(login|auth|signin)(/|$)', after.path)):
        raise ValueError('Ссылка перенаправляет на главную страницу или вход.')

    result = extract(html, final_url)
    result['resolved_url'] = final_url
    return result