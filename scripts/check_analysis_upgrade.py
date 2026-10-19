"""Two real YandexGPT requests with synthetic public-safe texts, separate cache DB."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mediapodborka import create_app
from mediapodborka.yandex import analyze

app = create_app({'TESTING': True, 'DATABASE': str(Path('artifacts/analysis-upgrade.sqlite3').resolve())})
reference = 'В Красноярске 18 октября прошли учебные занятия по пожарной безопасности в школе № 1. Ученики отработали эвакуацию, сотрудники проверили огнетушители. Пострадавших нет.'
publication = {'title': 'Проверка на заводе Норильска', 'original_text': 'В Норильске 19 октября пожарная безопасность стала темой проверки на металлургическом заводе. Инспекторы проверили системы оповещения и провели занятия с рабочими. Проверка была плановой. Пострадавших нет.'}
report = {}
for mode in ('by_thesis', 'compare'):
    result = analyze(app.config, {'mode': mode, 'query': 'пожарная безопасность' if mode == 'by_thesis' else reference,
        'query_type': 'topic' if mode == 'by_thesis' else 'article', 'region': 'Красноярский край', 'city': 'Норильск',
        'reference': {'original_text': reference} if mode == 'compare' else {}}, publication)
    report[mode] = result
Path('artifacts/analysis-upgrade.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
