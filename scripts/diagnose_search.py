"""Bounded live diagnosis: original stage errors, never credentials."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediapodborka import create_app
from mediapodborka.jobs import load_search, build_queries, candidate_allowed
from mediapodborka.yandex import search_terms, web_search
from mediapodborka.content import fetch_html, extract
from mediapodborka.matching import phrase_match

app = create_app({'TESTING': True})
search = load_search(app.config, sys.argv[1])
if len(sys.argv) > 2:
    search['query'] = sys.argv[2]
    search['city'] = sys.argv[3] if len(sys.argv) > 3 else ''
queries = build_queries(search, search_terms(app.config, search))
candidates = {}
for query, label in queries:
    if label != 'Новостные сайты':
        continue
    found = web_search(app.config, query)
    print(json.dumps({'query': query, 'found': len(found)}, ensure_ascii=False), flush=True)
    for candidate in found:
        if candidate_allowed(search, candidate['url']):
            candidates.setdefault(candidate['url'], candidate)

def inspect(candidate):
    url = candidate['url']
    result = {'url': url, 'title': candidate['title']}
    try:
        html, resolved = fetch_html(url)
        result.update(bytes=len(html), resolved=resolved)
        import hashlib
        out = Path('artifacts/diagnostic-html')
        out.mkdir(exist_ok=True)
        filename = out / (hashlib.sha256(url.encode()).hexdigest()[:12] + '.html')
        filename.write_bytes(html)
        result['saved_html'] = str(filename)
        original = extract(html, resolved)
        result.update(length=len(original['original_text']), date=original['published_at'],
                      exact=phrase_match(search['query'], original['original_text']), preview=original['original_text'][:180])
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc))
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result

with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(inspect, list(candidates.values())[:12]))
Path('artifacts/search-diagnosis.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
