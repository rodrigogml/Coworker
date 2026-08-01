CREATE TABLE IF NOT EXISTS authorized_users (
    user_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'user')),
    display_name TEXT NOT NULL,
    username TEXT,
    paired_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_owner
    ON authorized_users(role) WHERE role = 'owner' AND revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS pairing_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pin_salt BLOB NOT NULL,
    pin_digest BLOB NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    max_attempts INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    candidate_user_id INTEGER,
    candidate_chat_id INTEGER,
    candidate_name TEXT,
    candidate_username TEXT,
    approval_code TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS telegram_updates (
    update_id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL,
    processed_at TEXT,
    status TEXT NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS codex_sessions (
    chat_id INTEGER PRIMARY KEY,
    thread_id TEXT,
    created_at TEXT,
    last_used_at TEXT,
    active INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    text TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY(update_id) REFERENCES telegram_updates(update_id)
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER NOT NULL,
    telegram_file_id TEXT NOT NULL,
    original_name TEXT,
    local_path TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(update_id) REFERENCES telegram_updates(update_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    pid INTEGER,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    FOREIGN KEY(update_id) REFERENCES telegram_updates(update_id)
);
