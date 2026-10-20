import os
from mediapodborka import create_app

# Само приложение (его же подхватывает любой WSGI-сервер)
app = create_app()

if __name__ == '__main__':
    # Запуск через waitress: адрес и порт берутся из переменных окружения
    from waitress import serve
    host, port = os.getenv('HOST', '127.0.0.1'), int(os.getenv('PORT', '5000'))
    print(f'INFO-krai: http://{host}:{port}', flush=True)
    serve(app, host=host, port=port, threads=6)


