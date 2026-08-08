ALTER TABLE codex_preferences ADD COLUMN progress_mode TEXT NOT NULL DEFAULT 'off'
    CHECK (progress_mode IN ('off','compact','detailed'));
