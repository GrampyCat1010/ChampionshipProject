PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    platform TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT 'Красноярский край',
    created_at TEXT NOT NULL,
    UNIQUE(owner, url)
);
CREATE TABLE IF NOT EXISTS searches (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    query TEXT NOT NULL,
    query_type TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    region TEXT NOT NULL,
    mode TEXT NOT NULL,
    platforms TEXT NOT NULL,
    sources TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'В очереди',
    progress INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    warnings TEXT NOT NULL DEFAULT '[]',
    coverage TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    platform TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    original_text TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    fetch_status TEXT NOT NULL DEFAULT 'unavailable',
    fetch_note TEXT NOT NULL DEFAULT '',
    relevance TEXT NOT NULL DEFAULT 'review',
    reason TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    region_reason TEXT NOT NULL DEFAULT '',
    verification TEXT NOT NULL DEFAULT 'insufficient',
    included INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    UNIQUE(search_id, url)
);
CREATE INDEX IF NOT EXISTS idx_search_owner ON searches(owner, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pub_search ON publications(search_id);
CREATE TABLE IF NOT EXISTS analysis_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL
);
