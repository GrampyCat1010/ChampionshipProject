"""Run one small request per provider. Never print credentials or raw errors."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediapodborka import create_app
from mediapodborka.yandex import generate, web_search, ServiceError


def main():
    app = create_app({'TESTING': True})
    errors = 0
    for name, action in [
        ('YandexGPT', lambda: generate(app.config, 'Ответь одним словом: работает.', {'test': 'Проверка подключения'}, tokens=30)),
        ('Yandex Search API', lambda: web_search(app.config, 'Красноярск набережная Енисея')),
    ]:
        try:
            result = action()
            print(f'{name}: OK; ' + (f'{len(result)} search results' if isinstance(result, list) else 'response received'), flush=True)
        except ServiceError as exc:
            errors += 1
            print(f'{name}: {exc}', flush=True)
    return errors


if __name__ == '__main__':
    raise SystemExit(main())
