"""Exercise the running local server, including its worker and Excel endpoint."""
import json
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from openpyxl import load_workbook
from io import BytesIO

with httpx.Client(base_url='http://127.0.0.1:5000', timeout=15) as client:
    page = client.get('/')
    page.raise_for_status()
    csrf = BeautifulSoup(page.text, 'html.parser').select_one('meta[name="csrf-token"]')['content']
    response = client.post('/api/searches', headers={'X-CSRF-Token': csrf}, json={
        'mode': 'by_thesis', 'scope': 'extended', 'query': 'дороги',
        'region': 'Красноярский край', 'city': 'Красноярск',
        'date_from': '2026-07-22', 'date_to': '2026-10-19', 'platforms': ['web'], 'limit': 3,
    })
    assert response.status_code == 202, response.text
    sid = response.json()['id']
    print('Running-server search: ' + sid, flush=True)
    deadline = time.monotonic() + 600
    previous = None
    while time.monotonic() < deadline:
        response = client.get('/api/searches/' + sid)
        response.raise_for_status()
        result = response.json()
        search = result['search']
        state = (search['state'], search['progress'], len(result['publications']))
        if state != previous:
            print(state, flush=True)
            previous = state
        if search['state'] not in ('queued', 'running'):
            break
        time.sleep(3)
    Path('artifacts/running-search-fix.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    assert search['state'] in ('partial', 'completed'), search['warnings']
    assert len(result['publications']) == 3, search['diagnostics']
    exported = client.get('/api/searches/' + sid + '/export')
    exported.raise_for_status()
    workbook = load_workbook(BytesIO(exported.content), read_only=True)
    print('Excel worksheets: ' + ', '.join(workbook.sheetnames), flush=True)
    Path('artifacts/running-search-fix.xlsx').write_bytes(exported.content)
    print('PASS: running server, real Yandex search, originals, analysis and Excel', flush=True)
