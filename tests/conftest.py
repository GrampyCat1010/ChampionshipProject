import pytest
from mediapodborka import create_app


@pytest.fixture
def app(tmp_path):
    app = create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'test.sqlite'),
                      'SECRET_KEY': 'test-only-key', 'YANDEX_CLOUD_API_KEY': '',
                      'YANDEX_SEARCH_API_KEY': '', 'YANDEX_CLOUD_FOLDER': ''})
    app.extensions['jobs'].submit = lambda _: True
    yield app
    app.extensions['jobs'].pool.shutdown(wait=True)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def headers(client):
    client.get('/')
    with client.session_transaction() as s:
        return {'X-CSRF-Token': s['csrf']}


@pytest.fixture
def search_payload():
    return {'query': 'Реконструкция набережной Енисея', 'query_type': 'topic', 'region': 'Красноярский край',
            'date_from': '2026-10-01', 'date_to': '2026-10-19', 'mode': 'extended',
            'platforms': ['vk', 'ok', 'max', 'web'], 'source_ids': []}
