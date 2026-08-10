CREATE TABLE IF NOT EXISTS audio_preferences (
    chat_id INTEGER PRIMARY KEY,
    audio_enabled INTEGER NOT NULL DEFAULT 0 CHECK (audio_enabled IN (0,1)),
    voice TEXT,
    language TEXT,
    speed REAL,
    updated_at TEXT NOT NULL
);
