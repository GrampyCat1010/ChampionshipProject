import json
import pytest

from mediapodborka.content import extract
from mediapodborka.jobs import build_queries, ordered_candidates, run_search
from mediapodborka.matching import query_match
from mediapodborka.yandex import analyze, search_terms
from test_search_upgrade import create, LABELS

ARTICLE = ('Команда «Аделинчики» детского технопарка Саратовского областного института '
           'вышла в финал Первенства России по спортивному программированию. '
           'В октябре ребята поедут выступать в Красноярск.')


def test_keywords_can_be_separated_but_names_cannot_be_substituted():
    assert query_match('Аделинчики Саратов', ARTICLE)
    assert query_match('Команда Аделинчики - Первенство России', ARTICLE)
    assert not query_match('Аделинчики Саратов', 'Магазин Апельсинчики в Саратове')
    assert not query_match('Аделинчики Саратов', 'Команда из Саратова выступила в финале')
    assert not query_match('"Аделинчики Саратов"', ARTICLE)
    assert query_match('«Первенство России» Аделинчики', ARTICLE)


def test_discovery_has_plain_query_and_each_social_domain():
    s = {'query':'Аделинчики Саратов','mode':'by_thesis','scope':'extended','region':'Саратовская область',
         'city':'Саратов','platforms':['web','vk','max'],'date_from':'2026-09-20','date_to':'2026-10-19'}
    queries = [q.strip() for q, _ in build_queries(s, search_terms({}, s))]
    assert queries[0] == 'Аделинчики Саратов'
    assert 'Аделинчики Саратов site:vk.ru' in queries
    assert 'Аделинчики Саратов site:vk.com' in queries
    assert 'Аделинчики Саратов site:max.ru' in queries
    assert 'date:' not in queries[0]
    assert any('date:' in q for q in queries)
    assert '"Аделинчики" Саратов' in queries
    assert '"Аделинчики Саратов"' not in queries


def test_platforms_are_not_starved_by_first_domain():
    candidates = {str(i): {'url':f'https://vk.com/wall-1_{i}', 'title':'Аделинчики','snippet':''} for i in range(25)}
    candidates['web'] = {'url':'https://example.com/news','title':'Аделинчики','snippet':''}
    candidates['max'] = {'url':'https://max.ru/channel/post','title':'Аделинчики','snippet':''}
    selected = ordered_candidates(candidates, {'query':'Аделинчики Саратов','mode':'by_thesis','platforms':['vk','web','max']}, 3)
    assert candidates['web'] in selected and candidates['max'] in selected


def test_max_post_body_not_channel_description():
    html = f'<title>Канал в MAX</title><div class="card"><div class="info"><div class="description"><span>{ARTICLE}</span></div></div></div>'
    result = extract(html, 'https://max.ru/school/post123')
    assert result['original_text'] == ARTICLE
    assert result['published_at'] is None
    with pytest.raises(ValueError, match='страница канала'):
        extract(html, 'https://max.ru/school')


@pytest.mark.parametrize('query_match_value,city,expected', [(True,True,'relevant'), (False,True,'irrelevant'), (None,True,'review'), (True,None,'review'), (True,False,'irrelevant'), (False,None,'irrelevant')])
def test_whole_query_and_geography_are_checked_in_context(app, monkeypatch, query_match_value, city, expected):
    response = {'query_match':query_match_value,'relevance':'relevant','reason':'Команда представляет Саратов.',
                'evidence_id':1,'region_match':True,'city_match':city,'region_reason':'Саратовский институт.',**LABELS}
    monkeypatch.setattr('mediapodborka.yandex.generate', lambda *args: json.dumps(response))
    result = analyze(app.config, {'mode':'by_thesis','query':'Аделинчики Саратов','query_type':'topic','region':'Россия','city':'Саратов'},
                     {'title':'Команда в финале','original_text':ARTICLE})
    assert result['relevance'] == expected


def test_verified_article_and_unavailable_vk_share_list(app, client, headers, search_payload, monkeypatch):
    vk = {'url':'https://vk.ru/wall-1_123','title':'Аделинчики в финале','snippet':ARTICLE}
    web = {'url':'https://news.example.com/team','title':'Команда в финале','snippet':ARTICLE}
    junk = {'url':'https://max.ru/cars/post','title':'Автомобили Саратов','snippet':'Автомобили в Саратове'}
    monkeypatch.setattr('mediapodborka.jobs.web_search', lambda *args: [vk, web, junk])
    def fetch(url):
        if 'vk.com' in url:
            raise ValueError('Требуется JavaScript')
        return {'original_text':ARTICLE if 'example' in url else 'Автомобили в Саратове. ' * 10,
                'title':'Публикация','source':'Источник','published_at':'2026-10-10','content_hash':url}
    monkeypatch.setattr('mediapodborka.jobs.fetch_publication', fetch)
    monkeypatch.setattr('mediapodborka.jobs.analyze', lambda *args: {'relevance':'relevant','reason':'Команда из Саратова','evidence':ARTICLE,**LABELS})
    sid = create(client, headers, search_payload, query='Аделинчики Саратов', region='Россия', platforms=['vk','max','web'])
    run_search(app.config, sid)
    result = client.get('/api/searches/' + sid).json
    assert len(result['publications']) == 2
    verified = next(p for p in result['publications'] if p['fetch_status'] == 'received')
    social = next(p for p in result['publications'] if p['fetch_status'] == 'unavailable')
    assert verified['relevance'] == 'relevant'
    assert social['relevance'] == 'review' and social['included'] == 0 and social['original_text'] == ''
    assert social['snippet'] == ARTICLE
    leads = [d for d in result['search']['diagnostics'] if d.get('index_match')]
    assert len(leads) == 1 and leads[0]['platform'] == 'vk'
    assert any(d['stage'] == 'exact_match' and 'max.ru' in d['url'] for d in result['search']['diagnostics'])


def test_platform_queries_run_even_when_first_query_fills_budget(app, client, headers, search_payload, monkeypatch):
    app.config['HARD_CANDIDATE_LIMIT'] = 1
    queries = []
    def search(config, query, page):
        queries.append(query)
        return [{'url':'https://news.example.com/team','title':'Аделинчики','snippet':ARTICLE}]
    monkeypatch.setattr('mediapodborka.jobs.web_search', search)
    monkeypatch.setattr('mediapodborka.jobs.fetch_publication', lambda _: (_ for _ in ()).throw(ValueError('Unavailable')))
    sid = create(client, headers, search_payload, query='Аделинчики Саратов', region='Россия', platforms=['web','vk','max'])
    run_search(app.config, sid)
    assert any('site:max.ru' in q for q in queries) and any('site:vk.ru' in q for q in queries)
