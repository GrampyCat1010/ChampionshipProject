"""Optional paid integration check: at most 4 originals, one search page, separate DB."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediapodborka import create_app
from mediapodborka.jobs import run_search


def main():
    artifacts = Path(__file__).resolve().parents[1] / 'artifacts'
    artifacts.mkdir(exist_ok=True)
    app = create_app({'TESTING': True, 'DATABASE': str(artifacts / 'live-smoke.sqlite3'), 'MAX_CANDIDATES': 4, 'HARD_CANDIDATE_LIMIT': 4, 'SEARCH_PAGES': 1})
    app.extensions['jobs'].submit = lambda _: True
    client = app.test_client()
    client.get('/')
    with client.session_transaction() as session:
        token = session['csrf']
    payload = {'query': 'набережная', 'query_type': 'topic', 'region': 'Красноярский край',
               'date_from': '2025-01-01', 'date_to': '2025-12-31', 'mode': 'by_thesis', 'scope': 'extended', 'limit': 4, 'platforms': ['web'], 'source_ids': []}
    response = client.post('/api/searches', json=payload, headers={'X-CSRF-Token': token})
    assert response.status_code == 202, response.json
    sid = response.json['id']
    print('Live pipeline started (max 4 candidates).', flush=True)
    run_search(app.config, sid)
    result = client.get('/api/searches/' + sid).json
    report = {'state': result['search']['state'], 'warnings': result['search']['warnings'],
              'publications': [{k: p[k] for k in ('title', 'url', 'fetch_status', 'fetch_note', 'published_at', 'relevance', 'reason', 'included')} for p in result['publications']]}
    (artifacts / 'live-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    selected = [p for p in result['publications'] if p['included']]
    if selected:
        exported = client.get('/api/searches/' + sid + '/export')
        assert exported.status_code == 200
        (artifacts / 'live-smoke.xlsx').write_bytes(exported.data)
        print(f'Exported {len(selected)} publications.', flush=True)


if __name__ == '__main__':
    main()
