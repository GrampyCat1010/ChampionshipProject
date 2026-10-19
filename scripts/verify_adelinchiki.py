"""Real API/original regression for the user's query, in an isolated database."""
import json
import sys
import time
import httpx
from bs4 import BeautifulSoup
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediapodborka import create_app
from mediapodborka.jobs import run_search

running = '--running' in sys.argv
if running:
    client = httpx.Client(base_url='http://127.0.0.1:5000', timeout=20)
    page = client.get('/')
    page.raise_for_status()
    headers = {'X-CSRF-Token': BeautifulSoup(page.text, 'html.parser').select_one('meta[name="csrf-token"]')['content']}
else:
    app = create_app({'TESTING': True, 'DATABASE': str(Path('artifacts/adelinchiki/live.sqlite3').resolve()),
                      'MAX_CANDIDATES':18, 'HARD_CANDIDATE_LIMIT':18, 'SEARCH_PAGES':1})
    app.extensions['jobs'].submit = lambda _: True
    client = app.test_client()
    client.get('/')
    with client.session_transaction() as session:
        headers = {'X-CSRF-Token':session['csrf']}
payload = {'query':'Аделинчики Саратов', 'mode':'by_thesis','scope':'extended','region':'Россия',
           'city':'', 'date_from':'2026-07-22','date_to':'2026-10-19','platforms':['vk','max','web'],'limit':5}
response = client.post('/api/searches', json=payload, headers=headers)
assert response.status_code == 202
sid = (response.json() if running else response.json)['id']
print('Started ' + sid, flush=True)
if not running:
    run_search(app.config, sid)
deadline = time.monotonic() + 600
while True:
    response = client.get('/api/searches/' + sid)
    result = response.json() if running else response.json
    if result['search']['state'] not in ('queued','running') or time.monotonic() >= deadline:
        break
    time.sleep(3)
prefix = 'running-result' if running else 'live-result'
Path('artifacts/adelinchiki/' + prefix + '.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'state':result['search']['state'], 'coverage':result['search']['coverage'],
 'publications':[{k:p[k] for k in ('title','url','published_at','relevance','reason')} for p in result['publications']],
 'index_leads':[d for d in result['search']['diagnostics'] if d.get('index_match')],
 'diagnostics':[{k:d[k] for k in ('url','stage','reason')} for d in result['search']['diagnostics']]},ensure_ascii=False,indent=2),flush=True)
assert any('now64.ru' in p['url'] and p['relevance']=='relevant' for p in result['publications'])
assert any(d.get('index_match') and d.get('platform')=='vk' for d in result['search']['diagnostics'])
assert any('site:max.ru' in c.get('query','') and c['status']=='ok' for c in result['search']['coverage'])
export = client.get('/api/searches/' + sid + '/export')
assert export.status_code == 200
Path('artifacts/adelinchiki/' + prefix + '.xlsx').write_bytes(export.content if running else export.data)
print('PASS: relevant original, visible VK leads, MAX queried, Excel exported',flush=True)
