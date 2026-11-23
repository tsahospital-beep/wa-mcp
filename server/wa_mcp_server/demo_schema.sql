-- Reader tables from the bridge schema, bundled for standalone Python installations.
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE conversations (
  jid TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', is_group INTEGER NOT NULL DEFAULT 0,
  last_activity INTEGER NOT NULL DEFAULT 0);

CREATE TABLE chat_messages (
  conv TEXT NOT NULL, msg_id TEXT NOT NULL, author TEXT NOT NULL DEFAULT '', outgoing INTEGER NOT NULL DEFAULT 0,
  sent_at INTEGER NOT NULL, body TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'text',
  attachment_name TEXT, mime TEXT, byte_size INTEGER,
  direct_path TEXT, cdn_url TEXT, media_key BLOB, plain_sha256 BLOB, enc_sha256 BLOB, local_path TEXT,
  reply_to_id TEXT, reply_to_author TEXT, reply_preview TEXT,
  mentioned TEXT,
  read_at INTEGER, edited_at INTEGER, deleted_at INTEGER, deleted_unverified INTEGER, deleted_by TEXT,
  PRIMARY KEY (conv, msg_id));

CREATE TABLE derived_text (
  conv TEXT NOT NULL, msg_id TEXT NOT NULL, source TEXT NOT NULL,
  lang TEXT, confidence REAL, content TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY (conv, msg_id, source));

CREATE TABLE call_log (
  call_id TEXT PRIMARY KEY, peer TEXT NOT NULL, group_jid TEXT, outgoing INTEGER NOT NULL, video INTEGER NOT NULL,
  started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT NOT NULL DEFAULT 'unknown', creator TEXT NOT NULL DEFAULT '', group_call INTEGER NOT NULL DEFAULT 0);

CREATE TABLE scheduled_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, target TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', media_path TEXT,
  reply_to_id TEXT, mentions TEXT, next_run INTEGER NOT NULL, recurrence TEXT NOT NULL DEFAULT 'once',
  status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  created_at INTEGER NOT NULL, last_sent_at INTEGER, anchor_day INTEGER, kind TEXT, human INTEGER, agent_revision TEXT NOT NULL DEFAULT '', quiet_queued INTEGER NOT NULL DEFAULT 0, source_chat TEXT NOT NULL DEFAULT '', source_message_id TEXT NOT NULL DEFAULT '');

CREATE TABLE reactions (
  conv TEXT NOT NULL, msg_id TEXT NOT NULL, reactor TEXT NOT NULL, emoji TEXT NOT NULL,
  reacted_at INTEGER NOT NULL, added_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (conv, msg_id, reactor));

CREATE TABLE wa_contacts (
  jid TEXT PRIMARY KEY, first_name TEXT NOT NULL DEFAULT '', full_name TEXT NOT NULL DEFAULT '',
  push_name TEXT NOT NULL DEFAULT '', business_name TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL DEFAULT 0);
