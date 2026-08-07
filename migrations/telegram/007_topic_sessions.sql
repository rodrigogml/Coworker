CREATE TABLE IF NOT EXISTS telegram_topic_sessions (
    chat_id INTEGER NOT NULL,
    message_thread_id INTEGER NOT NULL,
    codex_thread_id TEXT,
    last_used_at TEXT,
    PRIMARY KEY (chat_id, message_thread_id)
);
