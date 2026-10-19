"""Real regression for the recent all-review search; separate database."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mediapodborka import create_app
from mediapodborka.jobs import run_search

app=create_app({'TESTING':True,'DATABASE':str(Path('artifacts/common-cards-live.sqlite3').resolve()),
                'MAX_CANDIDATES':24,'HARD_CANDIDATE_LIMIT':24,'SEARCH_PAGES':1})
app.extensions['jobs'].submit=lambda _: True
client=app.test_client();client.get('/')
with client.session_transaction() as session: headers={'X-CSRF-Token':session['csrf']}
payload={'query':'пожарная безопасность','mode':'by_thesis','scope':'extended','region':'Красноярский край',
         'city':'Красноярск','date_from':'2026-09-20','date_to':'2026-10-19','platforms':['vk','ok','max','web'],'limit':10}
r=client.post('/api/searches',headers=headers,json=payload)
assert r.status_code==202,r.json
sid=r.json['id'];print('Started '+sid,flush=True)
run_search(app.config,sid)
result=client.get('/api/searches/'+sid).json
Path('artifacts/common-cards-live.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'state':result['search']['state'],'publications':[{k:p[k] for k in ('title','platform','relevance','reason','published_at','fetch_status')} for p in result['publications']]},ensure_ascii=False,indent=2),flush=True)
assert any(p['relevance']=='relevant' and p['fetch_status']=='received' for p in result['publications']), 'No verified relevant originals'
assert any(p['platform'] in ('vk','ok') and p['fetch_status']=='unavailable' for p in result['publications']), 'No social cards'
print('PASS: verified relevant news and unavailable social cards share one collection',flush=True)
