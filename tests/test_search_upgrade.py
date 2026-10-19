import json
import logging
from pathlib import Path

import pytest

from mediapodborka.content import extract, parse_date
from mediapodborka.db import connect, initialize
from mediapodborka.filters import fresh_at
from mediapodborka.jobs import run_search, build_queries
from mediapodborka.locations import validate_location
from mediapodborka.matching import phrase_match
from mediapodborka.yandex import ANALYSIS_PROMPT, analyze, search_terms, ServiceError, summarize_reference

ANCHOR = '2026-10-19T12:00:00+00:00'
REFERENCE = 'В Красноярске пожарная безопасность стала темой учений в школе № 1. Учения прошли 18 октября, администрация сообщила об эвакуации учеников и проверке оборудования.'
OTHER = 'В Норильске пожарная безопасность стала темой проверки на заводе. Проверка состоялась 19 октября, специалисты проверили оборудование и провели обучение сотрудников.'
LABELS = {'emotion': 'нейтральная', 'emotion_reason': 'Факты изложены без оценочных эпитетов.', 'style': 'официально-деловой', 'style_reason': 'Используются точные формулировки и сведения о проверке.'}


def create(client, headers, base, **change):
    payload = {**base, 'mode': 'by_thesis', 'query': 'пожарная безопасность', **change}
    response = client.post('/api/searches', headers=headers, json=payload)
    assert response.status_code == 202, response.json
    return response.json['id']


def pipeline(monkeypatch, texts, dates=None, statuses=None):
    candidates = [{'url': f'https://news.example.com/post/{i}', 'title': f'Проверка {i}', 'snippet': ''} for i in range(len(texts))]
    monkeypatch.setattr('mediapodborka.jobs.web_search', lambda *args: candidates)
    def fetch(url):
        i = int(url.rsplit('/', 1)[1])
        return {'original_text': texts[i], 'title': f'Проверка {i}', 'source': 'Редакция',
                'published_at': dates[i] if dates else '2026-10-19T10:00:00+00:00', 'content_hash': str(i)}
    monkeypatch.setattr('mediapodborka.jobs.fetch_publication', fetch)
    monkeypatch.setattr('mediapodborka.jobs.analyze', lambda config, search, pub: {
        'relevance': statuses[int(pub['url'].rsplit('/', 1)[1])] if statuses else 'relevant',
        'reason': 'Публикация относится к запросу.', 'evidence': pub['original_text'][:100],
        'region_reason': 'В тексте назван город региона.', 'verification': 'insufficient', **LABELS})


@pytest.mark.parametrize('query,text,expected', [
    ('пожарная безопасность', 'Важна пожарная безопасность.', True),
    ('пожарная безопасность', 'О пожарной безопасности говорят сегодня.', True),
    ('пожарная безопасность', 'Правила безопасности', False),
    ('пожарная безопасность', 'Безопасность движения', False),
    ('пожарная безопасность', 'Пожарная машина обеспечивает безопасность', False),
    ('пожарная безопасность', 'Пожарная. Безопасность движения', False),
    ('адресант', 'Ответ отправлен адресанту.', True),
    ('адресант', 'Сообщение адресанта.', True),
    ('адресант', 'Адреса, адрес, адресат.', False),
    ('адресант', 'Лжеадресант и суперадресант', False),
    ('неизвестноеслово', 'неизвестноесловом', False),
    ('пожарная безопасность', 'ПОЖАРНАЯ\nБЕЗОПАСНОСТЬ', True),
])
def test_exact_lexeme_and_phrase(query, text, expected):
    assert bool(phrase_match(query, text)) == expected


@pytest.mark.parametrize('value,expected', [
    ('2026-10-18T12:00:00Z', True),
    ('2026-10-18T11:59:59Z', False),
    ('2026-10-19T16:00:00+04:00', True),
    ('2026-10-19T12:00:01+00:00', False),
    ('2026-10-19', False), ('2026-10-19T10:00:00', False), (None, False),
])
def test_exact_fresh_boundary(value, expected):
    assert fresh_at(value, ANCHOR) == expected


@pytest.mark.parametrize('mode', ['by_thesis', 'compare'])
def test_fresh_filter_in_both_modes(app, client, headers, search_payload, monkeypatch, mode):
    pipeline(monkeypatch, [OTHER] * 5, ['2026-10-18T12:00:00Z', '2026-10-18T11:59:59Z', '2026-10-19', None, '2026-10-19T12:00:01Z'])
    monkeypatch.setattr('mediapodborka.jobs.summarize_reference', lambda *args: {'summary': 'Учения в школе.', 'terms': ['пожарная безопасность'], **LABELS})
    sid = create(client, headers, search_payload, mode=mode, query=REFERENCE if mode == 'compare' else 'пожарная безопасность', fresh_only=True)
    with connect(app.config['DATABASE']) as db:
        db.execute('UPDATE searches SET created_at=? WHERE id=?', (ANCHOR, sid))
    run_search(app.config, sid)
    data = client.get('/api/searches/' + sid).json
    assert len(data['publications']) == 1
    assert data['publications'][0]['fresh'] is True


def test_no_semantic_substitution_in_pipeline(app, client, headers, search_payload, monkeypatch, caplog):
    pipeline(monkeypatch, ['Правила безопасности и безопасность движения.'])
    sid = create(client, headers, search_payload)
    with caplog.at_level(logging.INFO):
        run_search(app.config, sid)
    data = client.get('/api/searches/' + sid).json
    assert data['publications'] == [] and data['search']['state'] == 'completed'
    assert any('empty_search' in record.message and record.levelno == logging.INFO for record in caplog.records)
    assert client.get('/api/searches').json['searches'][0]['results_count'] == 0


@pytest.mark.parametrize('region,city', [('Красноярский край', 'Норильск'), ('Красноярский край', 'Ачинск'), ('Красноярский край', 'Канск'), ('Новосибирская область', 'Новосибирск'), ('Москва', 'Москва')])
def test_valid_city(region, city):
    assert validate_location(region, city)[1] == city


@pytest.mark.parametrize('city', ['Новосибирск', 'Неизвестный город'])
def test_invalid_city_blocked(client, headers, search_payload, city):
    response = client.post('/api/searches', headers=headers, json={**search_payload, 'city': city})
    assert response.status_code == 400 and 'город' in response.json['error'].lower()
    assert client.get('/api/searches').json['searches'] == []


@pytest.mark.parametrize('change', [{'limit': 0}, {'limit': 101}, {'limit': True}, {'limit': 10.5}, {'limit': '50'}, {'fresh_only': 'yes'}, {'mode': 'invalid'}])
def test_new_parameter_validation(client, headers, search_payload, change):
    assert client.post('/api/searches', headers=headers, json={**search_payload, **change}).status_code == 400


def test_history_repeat_limit_and_owner(app, client, headers, search_payload, monkeypatch):
    pipeline(monkeypatch, [OTHER] * 5)
    sid = create(client, headers, search_payload, limit=3, region='', city='Норильск')
    run_search(app.config, sid)
    run_search(app.config, sid)  # Duplicate delivery is a no-op.
    data = client.get('/api/searches/' + sid).json
    assert len(data['publications']) == 3
    assert data['search']['region_defaulted'] == 1
    history = client.get('/api/searches').json['searches']
    assert len(history) == 1 and history[0]['results_count'] == 3 and history[0]['limit'] == 3
    repeated = client.post('/api/searches/' + sid + '/repeat', headers=headers, json={})
    assert repeated.status_code == 202
    new = client.get('/api/searches/' + repeated.json['id']).json['search']
    for field in ['mode', 'query', 'region', 'city', 'fresh_only', 'limit', 'scope', 'platforms', 'date_from', 'date_to']:
        assert new[field] == data['search'][field]
    other = app.test_client()
    other.get('/')
    with other.session_transaction() as session:
        token = session['csrf']
    assert other.get('/api/searches').json['searches'] == []
    assert other.post('/api/searches/' + sid + '/repeat', headers={'X-CSRF-Token': token}, json={}).status_code == 404


def test_large_result_grouping_and_labels(app, client, headers, search_payload, monkeypatch):
    pipeline(monkeypatch, [OTHER] * 70, statuses=['relevant', 'indirect', 'review'] * 23 + ['relevant'])
    sid = create(client, headers, search_payload, limit=60)
    run_search(app.config, sid)
    data = client.get('/api/searches/' + sid).json
    assert len(data['publications']) == 60 and data['grouped'] is True
    assert data['groups'][0]['count'] == 60
    assert set(data['groups'][0]['publication_ids']) == {p['id'] for p in data['publications']}
    assert all(p['exact_match'] and p['emotion_reason'] and p['style_reason'] for p in data['publications'])
    assert {p['relevance'] for p in data['publications']} == {'relevant', 'indirect', 'review'}


def test_compare_url_reference_preserved(app, client, headers, search_payload, monkeypatch):
    pipeline(monkeypatch, [OTHER])
    original_fetch = __import__('mediapodborka.jobs', fromlist=['fetch_publication']).fetch_publication
    monkeypatch.setattr('mediapodborka.jobs.fetch_publication', lambda url: {'title': 'Эталон', 'original_text': REFERENCE, 'published_at': '2026-10-18', 'resolved_url': url} if url.endswith('/reference') else original_fetch(url))
    monkeypatch.setattr('mediapodborka.jobs.summarize_reference', lambda *args: {'summary': 'Учения прошли в школе.', 'terms': ['пожарная безопасность'], **LABELS})
    sid = create(client, headers, search_payload, mode='compare', query='https://example.com/reference')
    run_search(app.config, sid)
    data = client.get('/api/searches/' + sid).json
    assert data['search']['reference']['original_text'] == REFERENCE
    assert data['search']['reference']['summary'] == 'Учения прошли в школе.'
    assert len(data['publications']) == 1


@pytest.mark.parametrize('relation,quote,city_match,expected', [
    ('different', REFERENCE[:70], True, 'relevant'),
    ('same', REFERENCE[:70], True, 'irrelevant'),
    ('unknown', REFERENCE[:70], True, 'irrelevant'),
    ('different', 'Выдуманная цитата из неизвестного эталона', True, 'irrelevant'),
    ('different', REFERENCE[:70], False, 'irrelevant'),
])
def test_comparison_proof_and_city_gate(app, monkeypatch, relation, quote, city_match, expected):
    response = {'relevance': 'relevant', 'reason': 'Разные события.', 'evidence_id': 1,
                'region_match': True, 'city_match': city_match, 'region_reason': 'Норильск.',
                'same_topic': True, 'event_relation': relation, 'difference': 'В эталоне школа, в найденном тексте завод.',
                'reference_evidence': quote, **LABELS}
    prompts = []
    def generate(config, prompt, data, *args):
        prompts.append(prompt)
        return json.dumps(response)
    monkeypatch.setattr('mediapodborka.yandex.generate', generate)
    result = analyze(app.config, {'mode': 'compare', 'query': REFERENCE, 'query_type': 'article', 'region': 'Регион из формы', 'city': 'Норильск', 'reference': {'original_text': REFERENCE}}, {'original_text': OTHER, 'title': 'Проверка'})
    assert result['relevance'] == expected
    assert 'Красноярского края' not in ANALYSIS_PROMPT
    assert 'Регион из формы' in prompts[0] and 'Норильск' in prompts[0]
    assert '{{region}}' not in prompts[0]


def test_strict_queries_do_not_use_llm(monkeypatch):
    def prohibited(*args):
        raise AssertionError('No LLM rewriting for exact phrase searches')
    monkeypatch.setattr('mediapodborka.yandex.generate', prohibited)
    search = {'mode': 'by_thesis', 'scope': 'extended', 'query': 'пожарная безопасность', 'region': 'Красноярский край', 'city': 'Норильск', 'platforms': ['web'], 'date_from': '2026-10-01', 'date_to': '2026-10-19'}
    terms = search_terms({}, search)
    assert terms == ['пожарная безопасность']
    assert build_queries(search, terms)[0][0].strip() == 'пожарная безопасность'
    assert any('Норильск' in query for query, _ in build_queries(search, terms))


def test_migration_preserves_existing_history(tmp_path):
    path = str(tmp_path / 'old.sqlite')
    with connect(path) as db:
        db.executescript(Path('mediapodborka/schema.sql').read_text(encoding='utf-8'))
        db.execute('''INSERT INTO searches (id,owner,query,query_type,date_from,date_to,region,mode,platforms,sources,created_at)
            VALUES ('old','owner','тема','topic','2026-10-01','2026-10-19','Регион','sources','[]','[]',?)''', (ANCHOR,))
    initialize(path)
    initialize(path)
    with connect(path) as db:
        row = db.execute('SELECT * FROM searches').fetchone()
        assert row['scope'] == 'sources' and row['mode'] == 'by_thesis'
        assert db.execute('SELECT COUNT(*) FROM search_history').fetchone()[0] == 1
        assert db.execute('SELECT user_id FROM search_history').fetchone()[0] == 'owner'
        assert 'idx_history_user_created' in {r['name'] for r in db.execute('PRAGMA index_list(search_history)')}


def test_full_reference_not_silently_truncated():
    with pytest.raises(ValueError, match='60 000'):
        extract('<article>' + 'Слово ' * 11000 + '</article>', 'https://example.com/news')


def test_summary_uses_all_reference_chunks(monkeypatch):
    received = []
    def generate(config, prompt, data, *args):
        received.append(data)
        if 'summaries' in data:
            return json.dumps({'summary': 'Итог.', 'terms': ['событие'], **LABELS})
        return 'Краткий текст.'
    monkeypatch.setattr('mediapodborka.yandex.generate', generate)
    text = 'а' * 30000 + 'Последний факт.'
    summary = summarize_reference({}, text)
    assert ''.join(d['text'] for d in received if 'text' in d) == text
    assert summary['summary'] == 'Итог.'
