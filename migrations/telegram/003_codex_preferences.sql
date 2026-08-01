CREATE TABLE IF NOT EXISTS codex_preferences (
    chat_id INTEGER PRIMARY KEY,
    model TEXT,
    reasoning_effort TEXT CHECK (
        reasoning_effort IS NULL OR
        reasoning_effort IN ('minimal','low','medium','high','xhigh','max','ultra')
    ),
    speed TEXT CHECK (speed IS NULL OR speed IN ('standard','fast')),
    verbosity TEXT CHECK (
        verbosity IS NULL OR verbosity IN ('low','medium','high')
    ),
    updated_at TEXT NOT NULL
);
