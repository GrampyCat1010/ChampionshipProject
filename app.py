import os
from mediapodborka import create_app

app = create_app()

if __name__ == '__main__':
    from waitress import serve
    host, port = os.getenv('HOST', '127.0.0.1'), int(os.getenv('PORT', '5000'))
    print(f'Mediapodborka: http://{host}:{port}', flush=True)
    serve(app, host=host, port=port, threads=6)
