import pytest
from mediapodborka.content import extract, platform_for
from mediapodborka.matching import phrase_match
from mediapodborka.jobs import run_search
from test_search_upgrade import create, pipeline, OTHER

TEXT = 'В Красноярске ремонтируют дороги и устанавливают новые камеры. Работы ведутся в нескольких районах города. Подрядчик обновит дорожное покрытие и установит оборудование.'


def test_aspnet_form_keeps_article_and_publication_time():
    html = f'''<html><head><title>Ремонт дорог</title></head><body><form id="aspnetForm">
      <input value="hidden state"><header><nav>Меню</nav></header><article>
      <li itemprop="datePublished" content="2026-10-13T13:40:10+07:00">Дата</li>
      <div id="samcontentonly" itemprop="articleBody"><p>{TEXT}</p></div>
      </article></form></body></html>'''
    result = extract(html, 'https://example.com/news')
    assert result['original_text'] == TEXT
    assert result['published_at'] == '2026-10-13T13:40:10+07:00'


def test_main_news_body_beats_unrelated_cards_and_jsonld():
    other = 'Получить материнский капитал. ' * 15
    html = f'''<title>Ремонт дорог</title><meta property="article:published_time" content="2026-10-13T10:00:00+03:00">
        <script type="application/ld+json">{{"@type":"Article","articleBody":"{other}"}}</script>
        <article><p>{other}</p></article>
        <div class="journal-detail-page__main-content typography"><p>{TEXT}</p></div>'''
    result = extract(html, 'https://example.com/news')
    assert result['original_text'] == TEXT
    assert 'материнский' not in result['original_text']


def test_short_matching_selector_does_not_hide_full_body():
    result = extract(f'<div class="article-body">Реклама</div><article>{TEXT}</article>', 'https://example.com/news')
    assert result['original_text'] == TEXT


def test_login_form_still_not_article():
    with pytest.raises(ValueError):
        extract('<form><label>Войти</label><input name="password"><button>Войти</button></form>', 'https://example.com/login')


def test_road_inflections_without_derivations():
    assert phrase_match('дороги', 'Ремонт дорог в городе') == 'дорог'
    assert phrase_match('Дороги Красноярск', 'На дорогах Красноярск – Енисейск') == 'дорогах Красноярск'
    assert not phrase_match('дороги', 'Дорожная карта проекта')
    assert not phrase_match('адресант', 'Адреса, адрес, адресат')


def test_vkvideo_is_not_a_news_site():
    assert platform_for('https://m.vkvideo.ru/video-1_2') == 'vk'


def test_rejections_are_saved_and_owned(app, client, headers, search_payload, monkeypatch):
    pipeline(monkeypatch, [OTHER, 'Правила безопасности без исходной фразы.'], dates=['2025-01-01', '2026-10-13'])
    sid = create(client, headers, search_payload)
    run_search(app.config, sid)
    result = client.get('/api/searches/' + sid).json
    assert result['publications'] == []
    assert [r['stage'] for r in result['search']['diagnostics']] == ['date', 'exact_match']
    assert all(r['url'] and r['reason'] for r in result['search']['diagnostics'])
    assert app.test_client().get('/api/searches/' + sid).status_code == 404


def test_all_fetch_failures_do_not_claim_no_news(app, client, headers, search_payload, monkeypatch):
    pipeline(monkeypatch, [OTHER])
    def fail(url):
        raise TimeoutError('timed out')
    monkeypatch.setattr('mediapodborka.jobs.fetch_publication', fail)
    sid = create(client, headers, search_payload)
    run_search(app.config, sid)
    result = client.get('/api/searches/' + sid).json
    assert result['search']['state'] == 'partial'
    assert result['search']['diagnostics'][0]['stage'] == 'fetch'
    assert any('не означает' in w for w in result['search']['warnings'])
