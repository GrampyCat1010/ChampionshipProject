import sqlite3
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute('PRAGMA journal_mode = WAL')
        db.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))
        migrate(db)


def migrate(db):
    """Additive, idempotent upgrade of existing workspaces, all DML in one transaction."""
    db.execute('BEGIN IMMEDIATE')
    additions = {
        'searches': {
            'scope': "TEXT NOT NULL DEFAULT 'extended'", 'city': "TEXT NOT NULL DEFAULT ''",
            'fresh_only': 'INTEGER NOT NULL DEFAULT 0', 'result_limit': 'INTEGER NOT NULL DEFAULT 20',
            'reference': "TEXT NOT NULL DEFAULT '{}'", 'region_defaulted': 'INTEGER NOT NULL DEFAULT 0',
            'diagnostics': "TEXT NOT NULL DEFAULT '[]'",
        },
        'publications': {
            'exact_match': "TEXT NOT NULL DEFAULT ''", 'emotion': "TEXT NOT NULL DEFAULT 'не определена'",
            'emotion_reason': "TEXT NOT NULL DEFAULT 'Анализ эмоциональной окраски не выполнен.'",
            'style': "TEXT NOT NULL DEFAULT 'не определён'",
            'style_reason': "TEXT NOT NULL DEFAULT 'Анализ стиля не выполнен.'",
            'difference': "TEXT NOT NULL DEFAULT ''", 'reference_evidence': "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, fields in additions.items():
        columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
        for field, declaration in fields.items():
            if field not in columns:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {field} {declaration}')
    # Old modes described source scope. Preserve this independent choice.
    db.execute("UPDATE searches SET scope=mode, mode=CASE WHEN query_type='article' THEN 'compare' ELSE 'by_thesis' END WHERE mode IN ('extended','sources')")
    db.execute('''CREATE TABLE IF NOT EXISTS search_history (
        id TEXT PRIMARY KEY REFERENCES searches(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL, mode TEXT NOT NULL, query TEXT NOT NULL,
        region TEXT NOT NULL, city TEXT NOT NULL, fresh_only INTEGER NOT NULL,
        "limit" INTEGER NOT NULL, results_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
    )''')
    db.execute('CREATE INDEX IF NOT EXISTS idx_history_user_created ON search_history(user_id, created_at DESC)')
    db.execute('''INSERT OR IGNORE INTO search_history
        SELECT s.id,s.owner,s.mode,s.query,s.region,s.city,s.fresh_only,s.result_limit,
        (SELECT COUNT(*) FROM publications p WHERE p.search_id=s.id),s.created_at FROM searches s''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS history_search_insert AFTER INSERT ON searches BEGIN
        INSERT INTO search_history VALUES (NEW.id,NEW.owner,NEW.mode,NEW.query,NEW.region,NEW.city,
            NEW.fresh_only,NEW.result_limit,0,NEW.created_at); END''')
    for operation, ref, delta in [('INSERT', 'NEW', '+1'), ('DELETE', 'OLD', '-1')]:
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS history_publication_{operation.lower()}
            AFTER {operation} ON publications BEGIN
            UPDATE search_history SET results_count=results_count{delta} WHERE id={ref}.search_id; END''')
    # Existing completed searches should use the same common-card presentation.
    # Idempotent: preserve current selections, dates and original text verbatim.
    for search in db.execute("SELECT id,diagnostics,result_limit,finished_at,created_at FROM searches WHERE mode='by_thesis' AND fresh_only=0 AND state NOT IN ('queued','running') AND diagnostics!='[]'").fetchall():
        count = db.execute('SELECT COUNT(*) FROM publications WHERE search_id=?', (search['id'],)).fetchone()[0]
        for item in json.loads(search['diagnostics']):
            if count >= search['result_limit']:
                break
            if not item.get('index_match') or item.get('platform') not in ('vk','ok','max'):
                continue
            count += db.execute('''INSERT OR IGNORE INTO publications
                (id,search_id,url,title,source,platform,fetched_at,snippet,fetch_note,reason)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (uuid.uuid5(uuid.NAMESPACE_URL, search['id'] + item['url']).hex, search['id'],item['url'],
                 item.get('title') or item['url'], {'vk':'ВКонтакте','ok':'Одноклассники','max':'MAX'}[item['platform']],
                 item['platform'],search['finished_at'] or search['created_at'],item.get('snippet',''),item.get('reason',''),
                 'Совпадение в поисковом описании. Оригинал и дата не подтверждены; откройте источник для проверки.')).rowcount
    db.execute('PRAGMA user_version = 2')


def update_search(path, search_id, **values):
    fields = ', '.join(f'{key} = ?' for key in values)
    with connect(path) as db:
        db.execute(f'UPDATE searches SET {fields} WHERE id = ?', [*values.values(), search_id])
