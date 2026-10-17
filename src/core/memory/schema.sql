-- Ultron Prime schema v1. SQLite is the source of truth; Chroma is a rebuildable index.
-- Every statement is idempotent so the schema can be applied on every start.

CREATE TABLE IF NOT EXISTS chats (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  provider    TEXT,
  model       TEXT,
  archived    INTEGER NOT NULL DEFAULT 0,
  private     INTEGER NOT NULL DEFAULT 0,
  titled      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
  id               TEXT PRIMARY KEY,
  chat_id          TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  turn_id          TEXT,
  role             TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
  content          TEXT NOT NULL,
  tool_calls_json  TEXT,
  receipts_json    TEXT,
  attachments_json TEXT,
  meta_json        TEXT,
  token_count      INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_turn ON messages(turn_id);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content, content='messages', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF content ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
  INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TABLE IF NOT EXISTS turns (
  id               TEXT PRIMARY KEY,
  chat_id          TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  status           TEXT NOT NULL CHECK (status IN ('running', 'done', 'error', 'stopped')),
  started_at       TEXT NOT NULL,
  finished_at      TEXT,
  user_message_id  TEXT,
  assistant_message_id TEXT,
  provider         TEXT,
  model            TEXT,
  effort           TEXT,
  tainted          INTEGER NOT NULL DEFAULT 0,
  usage_json       TEXT,
  remote           INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turns_chat ON turns(chat_id, started_at);

CREATE TABLE IF NOT EXISTS turn_events (
  turn_id      TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  type         TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  PRIMARY KEY (turn_id, seq)
);

CREATE TABLE IF NOT EXISTS request_clusters (
  id                  TEXT PRIMARY KEY,
  chat_id             TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  canonical_text      TEXT NOT NULL,
  embedding_ref       TEXT,
  embedding_model     TEXT NOT NULL,
  ask_count           INTEGER NOT NULL DEFAULT 1,
  first_asked_at      TEXT NOT NULL,
  last_asked_at       TEXT NOT NULL,
  first_message_id    TEXT,
  last_message_id     TEXT,
  answered_message_id TEXT,
  was_refused         INTEGER NOT NULL DEFAULT 0,
  refusal_class       TEXT,
  volatile            INTEGER NOT NULL DEFAULT 0,
  status              TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_clusters_chat ON request_clusters(chat_id, last_asked_at);

CREATE TABLE IF NOT EXISTS egress_log (
  id                    TEXT PRIMARY KEY,
  chat_id               TEXT,
  turn_id               TEXT,
  call_id               TEXT NOT NULL,
  tool                  TEXT NOT NULL,
  outbound_string_hash  TEXT NOT NULL,
  decision              TEXT NOT NULL,
  reason                TEXT,
  created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipts (
  id             TEXT PRIMARY KEY,
  chat_id        TEXT,
  turn_id        TEXT,
  tool           TEXT NOT NULL,
  args_hash      TEXT NOT NULL,
  result_digest  TEXT NOT NULL,
  ok             INTEGER NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
  id             TEXT PRIMARY KEY,
  chat_id        TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  message_id     TEXT,
  original_name  TEXT NOT NULL,
  safe_name      TEXT NOT NULL,
  detected_mime  TEXT NOT NULL,
  size_bytes     INTEGER NOT NULL,
  sha256         TEXT NOT NULL,
  pages          INTEGER,
  width          INTEGER,
  height         INTEGER,
  trust          TEXT NOT NULL DEFAULT 'external' CHECK (trust IN ('external', 'trusted')),
  status         TEXT NOT NULL,
  secret_warning INTEGER NOT NULL DEFAULT 0,
  stored_path    TEXT,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_chat ON attachments(chat_id);

CREATE TABLE IF NOT EXISTS attachment_chunks (
  id             TEXT PRIMARY KEY,
  attachment_id  TEXT NOT NULL REFERENCES attachments(id) ON DELETE CASCADE,
  page           INTEGER,
  span_start     INTEGER NOT NULL,
  span_end       INTEGER NOT NULL,
  text           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_att ON attachment_chunks(attachment_id, page);

CREATE TABLE IF NOT EXISTS files (
  id            TEXT PRIMARY KEY,
  chat_id       TEXT,
  logical_name  TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('upload_copy', 'generated')),
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_chat ON files(chat_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_files_logical ON files(logical_name);

CREATE TABLE IF NOT EXISTS file_versions (
  id                TEXT PRIMARY KEY,
  file_id           TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  chat_id           TEXT,
  version           INTEGER NOT NULL,
  path              TEXT NOT NULL,
  sha256            TEXT NOT NULL,
  mime              TEXT NOT NULL,
  size_bytes        INTEGER NOT NULL,
  created_by        TEXT NOT NULL CHECK (created_by IN ('operator', 'forge', 'scribe', 'sandbox')),
  derived_from_json TEXT NOT NULL DEFAULT '[]',
  sources_json      TEXT NOT NULL DEFAULT '[]',
  external_content  INTEGER NOT NULL DEFAULT 0,
  change_note       TEXT,
  created_at        TEXT NOT NULL,
  UNIQUE (file_id, version)
);

CREATE INDEX IF NOT EXISTS idx_versions_chat ON file_versions(chat_id);

CREATE TABLE IF NOT EXISTS memories (
  id                   TEXT PRIMARY KEY,
  text                 TEXT NOT NULL,
  category             TEXT NOT NULL,
  importance           INTEGER NOT NULL DEFAULT 3,
  source_chat_id       TEXT,
  evidence_message_ids TEXT NOT NULL DEFAULT '[]',
  evidence_span        TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  edited_by_user       INTEGER NOT NULL DEFAULT 0,
  status               TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS memory_proposals (
  id              TEXT PRIMARY KEY,
  candidate_text  TEXT NOT NULL,
  category        TEXT NOT NULL,
  rationale       TEXT NOT NULL,
  evidence        TEXT NOT NULL DEFAULT '[]',
  verdict         TEXT NOT NULL,
  user_decision   TEXT,
  chat_id         TEXT,
  turn_id         TEXT,
  importance      INTEGER NOT NULL DEFAULT 3,
  final_text      TEXT,
  memory_id       TEXT,
  created_at      TEXT NOT NULL,
  decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_verdict ON memory_proposals(verdict, user_decision);

CREATE TABLE IF NOT EXISTS memory_mutes (
  category   TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id           TEXT PRIMARY KEY,
  event        TEXT NOT NULL,
  actor        TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guard_decisions (
  id          TEXT PRIMARY KEY,
  chat_id     TEXT,
  turn_id     TEXT,
  class       TEXT NOT NULL,
  attack_type TEXT,
  confidence  REAL NOT NULL,
  action      TEXT NOT NULL,
  task_type   TEXT,
  source      TEXT NOT NULL DEFAULT 'model',
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drift_scores (
  id          TEXT PRIMARY KEY,
  chat_id     TEXT,
  turn_id     TEXT,
  score       REAL NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
  id            TEXT PRIMARY KEY,
  chat_id       TEXT,
  turn_id       TEXT,
  role          TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms    INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_auth (
  id               INTEGER PRIMARY KEY CHECK (id = 1),
  passphrase_hash  TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,
  csrf_token    TEXT NOT NULL,
  created_at    TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  user_agent    TEXT,
  remote_addr   TEXT,
  is_remote     INTEGER NOT NULL DEFAULT 0,
  revoked       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS login_attempts (
  key           TEXT PRIMARY KEY,
  failures      INTEGER NOT NULL DEFAULT 0,
  locked_until  REAL NOT NULL DEFAULT 0,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings_overrides (
  key         TEXT PRIMARY KEY,
  value_json  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS redteam_runs (
  id           TEXT PRIMARY KEY,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_index_meta (
  collection       TEXT PRIMARY KEY,
  embedding_model  TEXT NOT NULL,
  dimension        INTEGER NOT NULL,
  updated_at       TEXT NOT NULL
);

PRAGMA user_version = 1;
