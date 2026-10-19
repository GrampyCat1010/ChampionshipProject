"""Fetch public originals with DNS pinning, bounded reads and conservative extraction."""
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

MAX_BYTES = 2_000_000
DOMAINS = {'vk': ('vk.com', 'vk.ru'), 'ok': ('ok.ru',), 'max': ('max.ru',)}


def platform_for(url):
    host = (urlsplit(url).hostname or '').lower()
    for platform, domains in DOMAINS.items():
        if any(host == d or host.endswith('.' + d) for d in domains):
            return platform
    return 'web'


def normalize_url(url):
    try:
        parts = urlsplit(url.strip())
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError
        if parts.port not in (None, 80, 443):
            raise ValueError
        host = parts.hostname.lower().encode('idna').decode('ascii')
        if host == 'localhost' or '.' not in host or '%' in host or ':' in host:
            raise ValueError
        try:
            if not ipaddress.ip_address(host).is_global:
                raise ValueError
        except ValueError as exc:
            if re.fullmatch(r'[0-9.]+', host):
                raise ValueError('Разрешены только публичные адреса.') from exc
        host = re.sub(r'^(www\.|m\.)', '', host) if platform_for(url) != 'web' else host
        if host == 'vk.ru':
            host = 'vk.com'
        query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith('utm_') and k not in ('fbclid', 'gclid', 'ref', 'from')]
        path = parts.path.rstrip('/') or '/'
        # VK wraps direct post links in ?w=wall-123_456.
        wall = dict(query).get('w', '')
        if host == 'vk.com' and re.fullmatch(r'wall-?\d+_\d+', wall):
            path, query = '/' + wall, []
        return urlunsplit((parts.scheme, host, path, urlencode(sorted(query)), ''))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Укажите публичную ссылку http:// или https:// без логина и нестандартного порта.') from exc


def source_matches(url, source_url):
    u, s = urlsplit(normalize_url(url)), urlsplit(normalize_url(source_url))
    if u.hostname != s.hostname:
        return False
    p, base = unquote(u.path).rstrip('/'), unquote(s.path).rstrip('/')
    if not base:
        return True
    if p == base or p.startswith(base + '/'):
        return True
    group = re.fullmatch(r'/(?:club|public)(\d+)', base)
    return bool(group and re.match(r'/wall-' + group[1] + r'_\d+$', p))


def public_addresses(host, port):
    addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('Внутренние сетевые адреса недоступны для сбора.')
    return addresses


def fetch_html(url):
    normalize_url(url)
    current = url
    for _ in range(5):
        parts = urlsplit(current)
        port = 443 if parts.scheme == 'https' else 80
        # Connect to the validated IP directly; TLS still verifies the original hostname.
        addresses = public_addresses(parts.hostname, port)
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=15)
        sock = socket.create_connection((addresses[0], port), timeout=15)
        try:
            if parts.scheme == 'https':
                context = ssl.create_default_context(cafile=certifi.where())
                sock = context.wrap_socket(sock, server_hostname=parts.hostname)
            conn.sock = sock
            target = urlunsplit(('', '', parts.path or '/', parts.query, ''))
            conn.request('GET', target, headers={'User-Agent': 'MediaPodborka/1.0 (+public-media-monitoring)', 'Accept': 'text/html', 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location:
                    raise ValueError('Переадресация без адреса.')
                redirected = urljoin(current, location)
                normalize_url(redirected)
                # Preserve the server's trailing slash while fetching. Only the
                # saved identity is canonicalized; otherwise slash redirects loop.
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
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace('Z', '+00:00')).date().isoformat()
    except (ValueError, TypeError):
        return None


def extract(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    def meta(*names):
        for name in names:
            el = soup.find('meta', attrs={'property': name}) or soup.find('meta', attrs={'name': name})
            if el and el.get('content'):
                return el['content'].strip()
        return ''
    title = meta('og:title') or (soup.title.get_text(' ', strip=True) if soup.title else '')
    published = parse_date(meta('article:published_time', 'datePublished', 'pubdate'))
    bodies = []
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
    time_el = soup.find('time', datetime=True)
    if time_el:
        published = published or parse_date(time_el['datetime'])
    date_el = soup.select_one('[itemprop="datePublished"]')
    if date_el:
        published = published or parse_date(date_el.get('content') or date_el.get('datetime') or date_el.get_text(strip=True))
    # Newslab's publication date is a dedicated metadata element, not an
    # arbitrary date inside the body (which could refer to an event).
    local_date = soup.select_one('.di3-meta__date')
    if not published and local_date:
        try:
            published = datetime.strptime(local_date.get_text(strip=True), '%d.%m.%Y').date().isoformat()
        except ValueError:
            pass
    for el in soup.select('script, style, nav, footer, header, noscript, form, aside'):
        el.decompose()
    selectors = ('[itemprop="articleBody"]', '.wall_post_text', '.media-text_cnt', '.post-text', '.js-mediator-article', '.article__body', '.article-body', '.article-content', '.entry-content', '.news-text', '.news__text', '.wcol.article', 'article', '.news-detail')
    article = next((node for selector in selectors if (node := soup.select_one(selector)) is not None), None)
    text = max(bodies, key=len) if bodies else article.get_text('\n', strip=True) if article else ''
    # Never promote an og:description, a search snippet, or a generic login page to an original.
    if len(text.strip()) < 100:
        raise ValueError('Полный текст публикации не выделен. Возможны авторизация или динамическая страница.')
    if re.search(r'captcha|access denied|проверка браузера|подтвердите.{0,20}не робот', title, re.I):
        raise ValueError('Источник показывает страницу проверки доступа.')
    text = re.sub(r'\n{3,}', '\n\n', text).strip()[:60000]
    return {'title': title[:500] or text[:100], 'original_text': text, 'published_at': published,
            'source': meta('og:site_name')[:200] or urlsplit(url).hostname,
            'content_hash': hashlib.sha256(re.sub(r'\s+', ' ', text).encode()).hexdigest()}


def fetch_publication(url):
    html, final_url = fetch_html(url)
    # A redirect to a homepage or login is not the requested original.
    before, after = urlsplit(url), urlsplit(final_url)
    if before.path not in ('', '/') and (after.path in ('', '/') or re.search(r'/(login|auth|signin)(/|$)', after.path)):
        raise ValueError('Ссылка перенаправляет на главную страницу или вход.')
    result = extract(html, final_url)
    result['resolved_url'] = final_url
    return result
