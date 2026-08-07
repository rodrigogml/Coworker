CREATE TABLE IF NOT EXISTS telegram_group_members (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (chat_id, user_id)
);

CREATE INDEX IF NOT EXISTS telegram_group_members_active
    ON telegram_group_members(chat_id, revoked_at);
