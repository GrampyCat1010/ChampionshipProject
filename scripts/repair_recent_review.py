"""Re-evaluate the two identified broken searches using their saved originals."""
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mediapodborka import create_app
from mediapodborka.yandex import analyze, ServiceError
from mediapodborka.jobs import load_search

app=create_app({'TESTING':True,'DATABASE':str(Path('artifacts/common-cards-live.sqlite3').resolve())})
db_path=str(Path('instance/mediapodborka.sqlite3').resolve())
changes=[]
for sid in ('0c1cb47cff9a4c42be6209a1fea7096e','930d9b5eb6884209a1c7441e37369ebd'):
    search=load_search({'DATABASE':db_path},sid)
    with sqlite3.connect(db_path) as db:
        db.row_factory=sqlite3.Row
        pubs=[dict(p) for p in db.execute("SELECT * FROM publications WHERE search_id=? AND relevance='review' AND fetch_status='received'",(sid,))]
    for pub in pubs:
        try: result=analyze(app.config,search,pub)
        except ServiceError as exc:
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE publications SET reason=? WHERE id=? AND relevance='review'",(str(exc),pub['id']))
            changes.append({'search':sid,'title':pub['title'],'status':'review','reason':str(exc)})
            print(json.dumps(changes[-1],ensure_ascii=False),flush=True)
            continue
        if result['relevance']!='irrelevant' and not pub['published_at']:
            result['reason']='Дата публикации не подтверждена. '+result['reason']
        fields=('relevance','reason','evidence','region_reason','verification','emotion','emotion_reason','style','style_reason')
        with sqlite3.connect(db_path) as db:
            db.execute('UPDATE publications SET '+','.join(f'{f}=?' for f in fields)+",included=CASE WHEN ?='irrelevant' THEN 0 ELSE included END WHERE id=? AND relevance='review'",
                       [*[result.get(f,'') for f in fields],result['relevance'],pub['id']])
        changes.append({'search':sid,'title':pub['title'],'status':result['relevance'],'reason':result['reason']})
        print(json.dumps(changes[-1],ensure_ascii=False),flush=True)
Path('artifacts/repaired-review-statuses.json').write_text(json.dumps(changes,ensure_ascii=False,indent=2),encoding='utf-8')
