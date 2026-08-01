ALTER TABLE messages ADD COLUMN reply_to_message_id INTEGER;
ALTER TABLE messages ADD COLUMN thread_id TEXT;
ALTER TABLE messages ADD COLUMN turn_id TEXT;
ALTER TABLE messages ADD COLUMN media_group_id TEXT;
ALTER TABLE messages ADD COLUMN content_type TEXT;
ALTER TABLE messages ADD COLUMN job_id INTEGER REFERENCES jobs(id);

ALTER TABLE attachments ADD COLUMN file_unique_id TEXT;
ALTER TABLE attachments ADD COLUMN detected_mime TEXT;
ALTER TABLE attachments ADD COLUMN logical_type TEXT;
ALTER TABLE attachments ADD COLUMN origin TEXT NOT NULL DEFAULT 'current';
ALTER TABLE attachments ADD COLUMN message_record_id INTEGER REFERENCES messages(id);

ALTER TABLE jobs ADD COLUMN workspace_path TEXT;
ALTER TABLE jobs ADD COLUMN request_message_id INTEGER REFERENCES messages(id);
ALTER TABLE jobs ADD COLUMN response_message_id INTEGER REFERENCES messages(id);
ALTER TABLE jobs ADD COLUMN thread_id TEXT;
ALTER TABLE jobs ADD COLUMN turn_id TEXT;
ALTER TABLE jobs ADD COLUMN media_group_id TEXT;

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    message_record_id INTEGER REFERENCES messages(id),
    direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    local_path TEXT NOT NULL,
    relative_path TEXT,
    requested_kind TEXT,
    effective_kind TEXT,
    caption TEXT,
    mime_type TEXT,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    upload_state TEXT NOT NULL CHECK (
        upload_state IN ('prepared', 'uploading', 'sent', 'failed', 'unknown')
    ),
    telegram_message_id INTEGER,
    telegram_file_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE job_updates (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    update_id INTEGER NOT NULL REFERENCES telegram_updates(update_id),
    message_record_id INTEGER REFERENCES messages(id),
    PRIMARY KEY(job_id, update_id)
);

CREATE INDEX messages_telegram_id ON messages(chat_id, message_id);
CREATE INDEX messages_thread_turn ON messages(thread_id, turn_id);
CREATE INDEX jobs_media_group ON jobs(chat_id, media_group_id);
CREATE INDEX artifacts_job_state ON artifacts(job_id, upload_state);
