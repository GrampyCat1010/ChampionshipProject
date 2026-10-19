"""Inspect public search results and originals; uses configured credentials only."""
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediapodborka import create_app
from mediapodborka.yandex import web_search
from mediapodborka.content import fetch_html, extract

app = create_app({'TESTING': True})
out = Path('artifacts/adelinchiki')
out.mkdir(exist_ok=True)
queries = ['Аделинчики Саратов', 'Аделинчики Саратов site:vk.com', 'Аделинчики Саратов site:max.ru']
report = []
def inspect(item):
    item = dict(item)
    try:
        html, url = fetch_html(item['url'])
        filename = out / (hashlib.sha256(item['url'].encode()).hexdigest()[:12] + '.html')
        filename.write_bytes(html)
        item.update(resolved=url, html=str(filename), bytes=len(html))
        item['original'] = extract(html, url)
    except Exception as exc:
        item['error'] = str(exc)
    print(json.dumps({k:v for k,v in item.items() if k != 'original'}, ensure_ascii=False), flush=True)
    return item
for query in queries:
    found = web_search(app.config, query)
    print(json.dumps({'query':query,'found':len(found)},ensure_ascii=False),flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        inspected = list(pool.map(inspect, found[:6]))
    report.append({'query':query,'found':found,'inspected':inspected})
    (out/'diagnosis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
