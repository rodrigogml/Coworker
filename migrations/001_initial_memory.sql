CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (
        kind IN ('fact', 'preference', 'decision', 'inference', 'reference', 'routine')
    ),
    subject TEXT NOT NULL CHECK (length(trim(subject)) > 0),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    source TEXT NOT NULL CHECK (length(trim(source)) > 0),
    scope TEXT NOT NULL DEFAULT 'global' CHECK (length(trim(scope)) > 0),
    sensitivity TEXT NOT NULL DEFAULT 'normal' CHECK (
        sensitivity IN ('normal', 'personal', 'confidential')
    ),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (
        confidence >= 0.0 AND confidence <= 1.0
    ),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'superseded', 'archived')
    ),
    credential_ref TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    supersedes_id TEXT REFERENCES memories(id) ON DELETE SET NULL
);

CREATE TABLE memory_tags (
    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag TEXT NOT NULL COLLATE NOCASE CHECK (length(trim(tag)) > 0),
    PRIMARY KEY (memory_id, tag)
);

CREATE INDEX idx_memories_status ON memories(status);
CREATE INDEX idx_memories_kind ON memories(kind);
CREATE INDEX idx_memories_scope ON memories(scope);
CREATE INDEX idx_memories_updated_at ON memories(updated_at);
CREATE INDEX idx_memories_expires_at ON memories(expires_at);
CREATE INDEX idx_memory_tags_tag ON memory_tags(tag);
