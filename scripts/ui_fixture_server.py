"""Isolated browser acceptance server. Synthetic publications, no external API calls."""
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediapodborka import create_app
from mediapodborka import jobs

app = create_app({'TESTING': True, 'DATABASE': str(Path(tempfile.mkdtemp(prefix='media-ui-')) / 'ui.sqlite'),
                  'SECRET_KEY': 'isolated-browser-test', 'YANDEX_CLOUD_API_KEY': '', 'YANDEX_CLOUD_FOLDER': ''})
TEXT = 'В Норильске пожарная безопасность стала темой проверки на заводе. Специалисты проверили оборудование и провели обучение сотрудников. Это синтетическая публикация для проверки интерфейса.'
LABELS = {'emotion': 'нейтральная', 'emotion_reason': 'Факты изложены без эмоциональных оценок.', 'style': 'официально-деловой', 'style_reason': 'Использованы деловые формулировки.'}
jobs.web_search = lambda config, query, page=0: [] if page else [
    {'url': f'https://example.com/testing/{i}', 'title': f'Тестовая публикация {i + 1}', 'snippet': ''} for i in range(65)]
jobs.fetch_publication = lambda url: {'original_text': TEXT, 'title': 'Проверка пожарной безопасности — тест ' + url.rsplit('/', 1)[-1],
    'published_at': datetime.now(timezone.utc).isoformat(), 'source': 'Тестовая редакция', 'content_hash': url}
jobs.summarize_reference = lambda config, text: {'summary': 'В Красноярске прошли учения в школе.', 'terms': ['пожарная безопасность'], **LABELS}
def analysis(config, search, pub):
    index = int(pub['url'].rsplit('/', 1)[-1])
    return {'relevance': ['relevant', 'indirect', 'review'][index % 3], 'reason': 'Тема подтверждается текстом тестовой публикации.',
        'evidence': TEXT[:100], 'region_reason': 'Норильск — город выбранного региона.', 'verification': 'insufficient', **LABELS,
        'difference': 'В эталоне учения в школе Красноярска, в найденном тексте проверка на заводе Норильска.' if search['mode'] == 'compare' else '',
        'reference_evidence': search.get('reference', {}).get('original_text', '')[:100]}
jobs.analyze = analysis

if __name__ == '__main__':
    from waitress import serve
    print('ISOLATED UI TEST SERVER http://127.0.0.1:5001 — synthetic data only', flush=True)
    serve(app, host='127.0.0.1', port=5001, threads=4)
