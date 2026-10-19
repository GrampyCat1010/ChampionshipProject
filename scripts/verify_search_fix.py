"""Bounded end-to-end live regression using real search, originals and YandexGPT."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediapodborka import create_app
from mediapodborka.jobs import run_search

app = create_app({'TESTING': True, 'DATABASE': str(Path('artifacts/search-fix-live.sqlite3').resolve()),
                  'MAX_CANDIDATES': 12, 'HARD_CANDIDATE_LIMIT': 12, 'SEARCH_PAGES': 1})
app.extensions['jobs'].submit = lambda _: True
client = app.test_client()
client.get('/')
with client.session_transaction() as session:
    headers = {'X-CSRF-Token': session['csrf']}
query = sys.argv[1] if len(sys.argv) > 1 else 'дороги'
city = sys.argv[2] if len(sys.argv) > 2 else ''
payload = {'mode': 'by_thesis', 'scope': 'extended', 'query': query, 'region': 'Красноярский край', 'city': city,
           'date_from': '2026-07-22', 'date_to': '2026-10-19', 'platforms': ['web'], 'limit': 3}
response = client.post('/api/searches', json=payload, headers=headers)
assert response.status_code == 202, response.json
sid = response.json['id']
print('Live search started: ' + query, flush=True)
run_search(app.config, sid)
result = client.get('/api/searches/' + sid).json
report = {'id':sid, 'query':query,'city':city,'state':result['search']['state'], 'processed':result['search']['progress'],
          'publications': [{k:p[k] for k in ('url','title','relevance','published_at','exact_match','reason','emotion','style')} for p in result['publications']],
          'diagnostics': result['search']['diagnostics']}
Path('artifacts/search-fix-' + sid + '.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
assert result['publications'], 'No verified results in bounded live run'
if any(p['included'] for p in result['publications']):
    exported = client.get('/api/searches/' + sid + '/export')
    assert exported.status_code == 200
    Path('artifacts/search-fix-live.xlsx').write_bytes(exported.data)
    print('Excel verified',flush=True)
