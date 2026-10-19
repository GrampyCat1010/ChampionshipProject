import json
from io import BytesIO

import pytest
from openpyxl import load_workbook
from mediapodborka.yandex import parse_analysis, ServiceError
from mediapodborka.db import connect, initialize
from mediapodborka.jobs import ordered_candidates, run_search
from test_search_upgrade import create, OTHER


@pytest.mark.parametrize('flag', [False, 'false'])
def test_explicit_rejection_never_becomes_review_without_quote(flag):
    raw = json.dumps({'relevance':'irrelevant','reason':'Другая область.', 'evidence':None,
                      'region_reason':None,'verification_reason':None,'region_match':flag,'query_match':flag})
    r = parse_analysis(raw, OTHER, {1:OTHER})
    assert r['relevance'] == 'irrelevant' and r['region_match'] is False
    assert r['evidence'] == ''


def test_string_booleans_and_segment_ids_preserve_supported_positive():
    raw = json.dumps({'relevance':'relevant','reason':'Подтверждено.', 'evidence_id':'1',
                      'query_match':'true','region_match':'true','city_match':'true'})
    r = parse_analysis(raw, OTHER, {1:OTHER})
    assert r['relevance'] == 'relevant' and r['evidence'] == OTHER
    assert all(r[k] is True for k in ('query_match','region_match','city_match'))


def test_uncertain_or_unknown_values_are_not_coerced_to_positive():
    raw = json.dumps({'relevance':'relevant','reason':'Неизвестно.', 'evidence_id':1,'region_match':'maybe'})
    assert parse_analysis(raw, OTHER, {1:OTHER})['relevance'] == 'review'


def test_model_refusal_has_specific_reason_instead_of_format_error():
    with pytest.raises(ServiceError, match='отказалась анализировать'):
        parse_analysis('Я не могу обсуждать эту тему. Давайте поговорим о чём-нибудь ещё.', OTHER)


def test_geographic_search_hits_precede_general_results():
    items = {'wrong':{'url':'https://other.example/a','title':'пожарная безопасность','snippet':''},
             'local':{'url':'https://local.example/b','title':'пожарная безопасность','snippet':'','location_hint':True}}
    result = ordered_candidates(items, {'platforms':['web'],'mode':'by_thesis','query':'пожарная безопасность'},1)
    assert result == [items['local']]


@pytest.mark.parametrize('platform,url', [('vk','https://vk.com/wall-1_123'),('ok','https://ok.ru/group/1/topic/123')])
def test_social_cards_are_exportable_only_after_manual_selection(app, client, headers, search_payload, monkeypatch, platform, url):
    monkeypatch.setattr('mediapodborka.jobs.web_search',lambda *args:[{'url':url,'title':'Пожарная безопасность','snippet':OTHER}])
    monkeypatch.setattr('mediapodborka.jobs.fetch_publication',lambda _: (_ for _ in ()).throw(ValueError('Вход необходим')))
    sid = create(client,headers,search_payload,platforms=[platform])
    run_search(app.config,sid)
    p = client.get('/api/searches/'+sid).json['publications'][0]
    assert p['relevance']=='review' and p['fetch_status']=='unavailable' and not p['included']
    assert client.get('/api/searches/'+sid+'/export').status_code==400
    client.patch('/api/searches/'+sid+'/publications/'+p['id'],headers=headers,json={'included':True})
    exported = client.get('/api/searches/'+sid+'/export')
    cells = list(load_workbook(BytesIO(exported.data),read_only=True)['Публикации'].values)[1]
    assert cells[7]=='Неоднозначное' and cells[9]=='Не получен'
    assert cells[5].startswith('Сниппет поиска:')


def test_existing_social_leads_migrate_once_without_fabricating_text(app,client,headers,search_payload):
    sid=create(client,headers,search_payload,limit=2)
    diag=[{'url':'https://ok.ru/group/1/topic/123','title':'Пожарная безопасность','snippet':OTHER,
           'reason':'Вход необходим','stage':'fetch','index_match':True,'platform':'ok'}]
    with connect(app.config['DATABASE']) as db:
        db.execute("UPDATE searches SET state='partial',diagnostics=? WHERE id=?",(json.dumps(diag),sid))
    initialize(app.config['DATABASE'])
    initialize(app.config['DATABASE'])
    result=client.get('/api/searches/'+sid).json
    assert len(result['publications'])==1
    assert result['publications'][0]['original_text']==''
    assert client.get('/api/searches').json['searches'][0]['results_count']==1


def test_social_leads_do_not_displace_verified_article(app,client,headers,search_payload,monkeypatch):
    items=[{'url':'https://vk.com/wall-1_123','title':'Пожарная безопасность','snippet':OTHER},
           {'url':'https://news.example/a','title':'Пожарная безопасность','snippet':OTHER}]
    monkeypatch.setattr('mediapodborka.jobs.web_search',lambda *args:items)
    def fetch(url):
        if 'vk.com' in url: raise ValueError('Недоступно')
        return {'title':'Пожарная безопасность','original_text':OTHER,'published_at':'2026-10-10','content_hash':'hash','source':'СМИ'}
    monkeypatch.setattr('mediapodborka.jobs.fetch_publication',fetch)
    monkeypatch.setattr('mediapodborka.jobs.analyze',lambda *args:{'relevance':'relevant','reason':'Подходит','evidence':OTHER})
    sid=create(client,headers,search_payload,platforms=['vk','web'],limit=1)
    run_search(app.config,sid)
    pubs=client.get('/api/searches/'+sid).json['publications']
    assert len(pubs)==1 and pubs[0]['platform']=='web'


def test_fresh_search_excludes_undated_social_cards(app,client,headers,search_payload,monkeypatch):
    monkeypatch.setattr('mediapodborka.jobs.web_search',lambda *args:[{'url':'https://ok.ru/group/1/topic/123','title':'Пожарная безопасность','snippet':OTHER}])
    monkeypatch.setattr('mediapodborka.jobs.fetch_publication',lambda _: (_ for _ in ()).throw(ValueError('Недоступно')))
    sid=create(client,headers,search_payload,platforms=['ok'],fresh_only=True)
    run_search(app.config,sid)
    initialize(app.config['DATABASE'])
    assert client.get('/api/searches/'+sid).json['publications']==[]


def test_wrong_region_social_snippet_is_not_added_or_restored(app,client,headers,search_payload,monkeypatch):
    monkeypatch.setattr('mediapodborka.jobs.web_search',lambda *args:[{'url':'https://vk.com/wall-1_123','title':'Пожарная безопасность','snippet':'Пожарная безопасность в Астрахани.'}])
    monkeypatch.setattr('mediapodborka.jobs.fetch_publication',lambda _: (_ for _ in ()).throw(ValueError('Недоступно')))
    def analyze(config,search,pub):
        assert pub['content_source']=='search_snippet'
        return {'relevance':'irrelevant','reason':'Другая область.'}
    monkeypatch.setattr('mediapodborka.jobs.analyze',analyze)
    sid=create(client,headers,search_payload,platforms=['vk'])
    run_search(app.config,sid)
    initialize(app.config['DATABASE'])
    result=client.get('/api/searches/'+sid).json
    assert result['publications']==[]
    assert not any(d.get('index_match') for d in result['search']['diagnostics'])
