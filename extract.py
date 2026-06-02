"""
import_claude.py — Claude export importer

Reads a Claude conversations.json export and:
  1. Populates a SQLite database (claude.db) with conversations,
     messages, content blocks, attachments, and file references.
  2. Reconstructs final file state for create_file artifacts by
     replaying create_file → str_replace sequences per path, then
     writes them to disk under artifacts/.
  3. Records each artifact's disk path in the DB so the CLI can
     print it and you can cp it directly.

Usage:
    python3 import_claude.py conversations.json [--db claude.db] [--artifacts ./artifacts]

Re-running is safe: uses INSERT OR REPLACE for all records and
overwrites artifact files on disk.
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path



# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import Claude export into SQLite")
    parser.add_argument("export",     help="Path to conversations.json")
    parser.add_argument("--db",       default="claude.db",    help="SQLite DB path")
    parser.add_argument("--artifacts", default="./artifacts", help="Artifact output dir")
    args = parser.parse_args()

    export_path   = Path(args.export)
    db_path       = Path(args.db)
    artifacts_root = Path(args.artifacts)

    if not export_path.exists():
        print(f"Error: {export_path} not found")
        sys.exit(1)

    print(f"Loading {export_path} …")
    conversations = load_export(export_path)
    print(f"Found {len(conversations)} conversations")

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(SCHEMA)
    con.commit()

    total_msgs      = 0
    total_artifacts = 0

    for i, conv in enumerate(conversations, 1):
        name = conv.get("name", "<untitled>")
        print(f"  [{i:2d}/{len(conversations)}] {name[:60]}")
        n_msgs, n_art = import_conversation(conv, cur, artifacts_root)
        total_msgs      += n_msgs
        total_artifacts += n_art
        print(f"         {n_msgs} messages, {n_art} artifacts")

    con.commit()
    con.close()

    print(f"\nDone.")
    print(f"  Conversations : {len(conversations)}")
    print(f"  Messages      : {total_msgs}")
    print(f"  Artifacts     : {total_artifacts}")
    print(f"  Database      : {db_path.resolve()}")
    print(f"  Artifacts dir : {artifacts_root.resolve()}")


# ── Schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS conversations (
    uuid         TEXT PRIMARY KEY,
    name         TEXT,
    summary      TEXT,
    account_uuid TEXT,
    created_at   TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    uuid                TEXT PRIMARY KEY,
    conversation_uuid   TEXT NOT NULL REFERENCES conversations(uuid),
    parent_message_uuid TEXT,
    sender              TEXT,   -- 'human' | 'assistant'
    text                TEXT,
    created_at          TEXT,
    updated_at          TEXT
);

CREATE TABLE IF NOT EXISTS content_blocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_uuid    TEXT NOT NULL REFERENCES messages(uuid),
    type            TEXT,   -- text | thinking | tool_use | tool_result
    start_timestamp TEXT,
    stop_timestamp  TEXT,

    -- type=text
    text            TEXT,
    citations       TEXT,   -- JSON

    -- type=thinking
    thinking        TEXT,
    summaries       TEXT,   -- JSON
    signature       TEXT,
    truncated       INTEGER,
    cut_off         INTEGER,

    -- type=tool_use
    tool_id         TEXT,   -- block.id (for pairing with tool_result)
    tool_name       TEXT,
    tool_input      TEXT,   -- JSON
    integration_name TEXT,
    is_mcp_app      INTEGER,
    mcp_server_url  TEXT,

    -- type=tool_result
    tool_use_id     TEXT,   -- references content_blocks.tool_id
    result_name     TEXT,
    result_content  TEXT,   -- JSON
    is_error        INTEGER
);

CREATE TABLE IF NOT EXISTS attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_uuid    TEXT NOT NULL REFERENCES messages(uuid),
    file_name       TEXT,
    file_type       TEXT,
    file_size       INTEGER,
    extracted_content TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_uuid    TEXT NOT NULL REFERENCES messages(uuid),
    file_uuid       TEXT,
    file_name       TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_uuid   TEXT NOT NULL REFERENCES conversations(uuid),
    -- message where create_file was called (first creation)
    message_uuid        TEXT REFERENCES messages(uuid),
    original_path       TEXT,   -- path Claude wrote to (e.g. /mnt/user-data/outputs/foo.py)
    disk_path           TEXT,   -- where we wrote it locally
    file_name           TEXT,
    created_at          TEXT,
    UNIQUE(conversation_uuid, original_path)
);

CREATE INDEX IF NOT EXISTS idx_messages_conv   ON messages(conversation_uuid);
CREATE INDEX IF NOT EXISTS idx_blocks_msg      ON content_blocks(message_uuid);
CREATE INDEX IF NOT EXISTS idx_blocks_tool_id  ON content_blocks(tool_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_conv  ON artifacts(conversation_uuid);
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def jdump(v):
    return json.dumps(v) if v is not None else None


def slug(text, maxlen=40):
    """Turn a conversation name into a safe directory name."""
    text = (text or "untitled").strip()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"[\s_]+", "_", text)
    text = text.strip("_-")
    return text[:maxlen] or "untitled"


def apply_str_replace(content: str, old_str: str, new_str: str) -> str:
    """Apply a single str_replace patch. Raises if old_str not found exactly once."""
    count = content.count(old_str)
    if count == 0:
        raise ValueError("old_str not found in content")
    if count > 1:
        raise ValueError(f"old_str found {count} times; ambiguous")
    return content.replace(old_str, new_str, 1)


def extract_heredoc(command: str):
    """
    Extract (path, content) from a bash heredoc of the form:
        cat > /some/path << 'EOF'
        ...content...
        EOF
    The delimiter may be quoted ('EOF', "EOF", or bare EOF).
    The closing delimiter may have trailing whitespace and need not be
    at the very end of the string (there may be more commands after).
    Returns None if the command doesn't look like a heredoc file write.
    """
    m = re.search(
        r"""cat\s*>\s*(\S+)\s*<<\s*['"]?(\w+)['"]?\n(.*?)\n\2[ \t]*(?:\n|$)""",
        command,
        re.DOTALL,
    )
    if m:
        return m.group(1), m.group(3)
    return None


# ── File-state reconstructor ──────────────────────────────────────────────────

class FileStateTracker:
    """
    Replays create_file and str_replace tool calls in message order to
    produce the final content of each file path within a conversation.

    Also tracks bash_tool heredoc writes as a fallback.
    """

    def __init__(self):
        # path → {"content": str, "message_uuid": str, "created_at": str}
        self._files = {}

    def on_tool_use(self, block, message_uuid, created_at):
        name  = block.get("name")
        inp   = block.get("input") or {}

        if name == "create_file":
            path    = inp.get("path", "")
            content = inp.get("file_text", "")
            if path:
                self._files[path] = {
                    "content":      content,
                    "message_uuid": message_uuid,
                    "created_at":   created_at,
                }

        elif name == "bash_tool":
            command = inp.get("command", "")
            result  = extract_heredoc(command)
            if result:
                path, content = result
                # Only record if we haven't seen a create_file for this path,
                # or if this bash write came after the last create_file.
                self._files[path] = {
                    "content":      content,
                    "message_uuid": message_uuid,
                    "created_at":   created_at,
                }

        elif name == "str_replace":
            path    = inp.get("path", "")
            old_str = inp.get("old_str", "")
            new_str = inp.get("new_str", "")
            if path and path in self._files:
                try:
                    patched = apply_str_replace(
                        self._files[path]["content"], old_str, new_str
                    )
                    self._files[path]["content"] = patched
                    # Keep original message_uuid (creation point) for DB record
                except ValueError as e:
                    print(
                        f"  [warn] str_replace on {path} failed: {e} "
                        f"(message {message_uuid})"
                    )
            elif path and path not in self._files:
                print(
                    f"  [warn] str_replace on {path} but no prior create_file "
                    f"seen in this conversation — skipping patch"
                )

    def final_files(self):
        """Yield (path, content, message_uuid, created_at) for each tracked file."""
        yield from (
            (path, info["content"], info["message_uuid"], info["created_at"])
            for path, info in self._files.items()
        )


# ── Importer ──────────────────────────────────────────────────────────────────

def load_export(path: Path):
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except json.JSONDecodeError:
        pass
    # JSONL fallback
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def has_content(conv) -> bool:
    """Return True if at least one message has non-empty text or content blocks."""
    for msg in conv.get("chat_messages") or conv.get("messages") or []:
        if (msg.get("text") or "").strip():
            return True
        if msg.get("content"):
            return True
    return False


def import_conversation(conv, cur, artifacts_root: Path):
    conv_uuid    = conv["uuid"]
    conv_name    = conv.get("name", "")
    conv_slug    = slug(conv_name)
    artifact_dir = artifacts_root / conv_uuid[:8] / conv_slug

    messages = conv.get("chat_messages") or conv.get("messages") or []

    # Skip conversations where no message has any content — these are export
    # artifacts from Anthropic with shells but no bodies.
    if not has_content(conv):
        return 0, 0

    # -- conversations table --
    cur.execute("""
        INSERT OR REPLACE INTO conversations
            (uuid, name, summary, account_uuid, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        conv_uuid,
        conv_name,
        conv.get("summary"),
        (conv.get("account") or {}).get("uuid"),
        conv.get("created_at"),
        conv.get("updated_at"),
    ))

    # Sort messages by created_at so str_replace patches apply in order
    messages = sorted(messages, key=lambda m: m.get("created_at", ""))

    tracker = FileStateTracker()

    for msg in messages:
        msg_uuid = msg["uuid"]

        # -- messages table --
        cur.execute("""
            INSERT OR REPLACE INTO messages
                (uuid, conversation_uuid, parent_message_uuid,
                 sender, text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            msg_uuid,
            conv_uuid,
            msg.get("parent_message_uuid"),
            msg.get("sender"),
            msg.get("text"),
            msg.get("created_at"),
            msg.get("updated_at"),
        ))

        # -- content blocks --
        for block in msg.get("content") or []:
            btype = block.get("type")

            if btype == "text":
                cur.execute("""
                    INSERT INTO content_blocks
                        (message_uuid, type, start_timestamp, stop_timestamp,
                         text, citations)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    msg_uuid, btype,
                    block.get("start_timestamp"), block.get("stop_timestamp"),
                    block.get("text"),
                    jdump(block.get("citations")),
                ))

            elif btype == "thinking":
                cur.execute("""
                    INSERT INTO content_blocks
                        (message_uuid, type, start_timestamp, stop_timestamp,
                         thinking, summaries, signature, truncated, cut_off)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    msg_uuid, btype,
                    block.get("start_timestamp"), block.get("stop_timestamp"),
                    block.get("thinking"),
                    jdump(block.get("summaries")),
                    block.get("signature"),
                    int(bool(block.get("truncated"))),
                    int(bool(block.get("cut_off"))),
                ))

            elif btype == "tool_use":
                cur.execute("""
                    INSERT INTO content_blocks
                        (message_uuid, type, start_timestamp, stop_timestamp,
                         tool_id, tool_name, tool_input,
                         integration_name, is_mcp_app, mcp_server_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    msg_uuid, btype,
                    block.get("start_timestamp"), block.get("stop_timestamp"),
                    block.get("id"),
                    block.get("name"),
                    jdump(block.get("input")),
                    block.get("integration_name"),
                    int(bool(block.get("is_mcp_app"))),
                    block.get("mcp_server_url"),
                ))
                tracker.on_tool_use(block, msg_uuid, msg.get("created_at", ""))

            elif btype == "tool_result":
                cur.execute("""
                    INSERT INTO content_blocks
                        (message_uuid, type, start_timestamp, stop_timestamp,
                         tool_use_id, result_name, result_content, is_error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    msg_uuid, btype,
                    block.get("start_timestamp"), block.get("stop_timestamp"),
                    block.get("tool_use_id"),
                    block.get("name"),
                    jdump(block.get("content")),
                    int(bool(block.get("is_error"))),
                ))

            else:
                # Unknown block type — store as tool_use with raw JSON so nothing is lost
                cur.execute("""
                    INSERT INTO content_blocks
                        (message_uuid, type, tool_input)
                    VALUES (?, ?, ?)
                """, (msg_uuid, btype or "<unknown>", jdump(block)))

        # -- attachments --
        for att in msg.get("attachments") or []:
            cur.execute("""
                INSERT INTO attachments
                    (message_uuid, file_name, file_type, file_size, extracted_content)
                VALUES (?, ?, ?, ?, ?)
            """, (
                msg_uuid,
                att.get("file_name"),
                att.get("file_type"),
                att.get("file_size"),
                att.get("extracted_content"),
            ))

        # -- files --
        for f in msg.get("files") or []:
            cur.execute("""
                INSERT INTO files (message_uuid, file_uuid, file_name)
                VALUES (?, ?, ?)
            """, (
                msg_uuid,
                f.get("file_uuid"),
                f.get("file_name"),
            ))

    # -- write artifacts to disk and record in DB --
    artifact_count = 0
    for orig_path, content, msg_uuid, created_at in tracker.final_files():
        file_name = Path(orig_path).name
        artifact_dir.mkdir(parents=True, exist_ok=True)
        disk_path = artifact_dir / file_name

        disk_path.write_text(content, encoding="utf-8")
        artifact_count += 1

        cur.execute("""
            INSERT OR REPLACE INTO artifacts
                (conversation_uuid, message_uuid, original_path,
                 disk_path, file_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            conv_uuid,
            msg_uuid,
            orig_path,
            str(disk_path),
            file_name,
            created_at,
        ))

    return len(messages), artifact_count


if __name__ == "__main__":
    main()
