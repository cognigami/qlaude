# Claude Conversation Browser

A small toolkit for importing your Claude conversation export into a SQLite
database and browsing it from the terminal.

## Files

| File | Purpose |
|------|---------|
| `probe_schema.py` | Inspect the structure of a raw export file |
| `probe_tools.py` | Deep-dive into tool use / artifact content in an export |
| `extract.py` | Import the export into SQLite and extract artifacts to disk |
| `browse_claude.py` | Terminal UI for browsing conversations and artifacts |

---

## Quick start

```bash
# 1. Import your export
python3 extract.py conversations.json

# 2. Browse
python3 browse_claude.py
```

Both scripts default to `claude.db` and `./artifacts` in the current directory.
Run them from the same directory as your export file for the simplest setup.

---

## Import

```bash
python3 extract.py conversations.json [--db claude.db] [--artifacts ./artifacts]
```

Reads `conversations.json` (the file Anthropic emails you as a ZIP attachment
when you request an export from Settings → Account → Export Data).

**What it does:**

- Populates `claude.db` with conversations, messages, content blocks,
  attachments, and file references.
- Reconstructs the **final state** of each file artifact by replaying
  `create_file` and `str_replace` tool calls in order. Files written via
  bash heredocs (`cat > file << 'EOF'`) are also captured.
- Writes artifact files to `artifacts/<conv-uuid-prefix>/<conv-slug>/filename`.
- Records each artifact's local disk path in the DB so the browser can
  show it and you can `cp` it directly from the shell.
- Skips conversations where no message has any content (these are empty
  shells that Anthropic sometimes includes in exports).
- Safe to re-run: uses `INSERT OR REPLACE` throughout and overwrites
  artifact files on disk.

**Artifact layout:**

```
artifacts/
  03cba4fd/                        ← first 8 chars of conversation UUID
    Dealing-range-builder/
      dr_builder.py
      test_dr_builder.py
  019e1e0f/
    Weather-Overground/
      main.go
      go.mod
```

**Known limitation:** if a file was modified by a bash command (e.g.
`go mod tidy`, `sed -i`) between a `create_file` and a subsequent
`str_replace`, the in-memory state can drift and the patch may not apply.
The importer prints a warning when this happens:

```
[warn] str_replace on /path/to/file failed: old_str not found in content
```

The artifact is still written with whatever content was reconstructed up
to that point.

---

## Browse

```bash
python3 browse_claude.py [--db claude.db]
```

Requires no dependencies beyond the Python standard library. Works in any
terminal that supports curses (tested on Alacritty). Mouse wheel scrolling
is supported.

### Conversation list

| Key | Action |
|-----|--------|
| `j` / `k` or arrows | Move selection |
| `Enter` | Open conversation |
| `1`–`9` | Jump directly to nth conversation |
| `s` | Cycle sort order: date → name → messages → artifacts |
| `/` | Search conversation titles; `n` / `N` next / prev match |
| `Ctrl-U` / `Ctrl-D` | Scroll half-page |
| `q` | Quit |

### Conversation view

The thread is rendered as a continuous scrollable document. Each block
(human turn, assistant turn, tool call, thinking) has a coloured left
gutter bar:

| Colour | Sender |
|--------|--------|
| Cyan | Human |
| Green | Assistant |
| Yellow | Tool / artifact |
| Magenta | Thinking |

`create_file` and bash heredoc artifacts are rendered as framed code boxes
showing the first 30 lines. The artifact's local disk path is shown at the
bottom-right of the box.

| Key | Action |
|-----|--------|
| `j` / `k` or arrows | Scroll one line |
| `Ctrl-U` / `Ctrl-D` | Scroll half-page |
| Mouse wheel | Scroll |
| `m` / `M` | Jump to next / prev block |
| `t` | Toggle thinking blocks visible in the viewport |
| `a` | Toggle artifact panel |
| `1`–`9` (artifact panel open) | Open nth artifact in `$PAGER` |
| `c` | Copy first artifact path to clipboard |
| `e` | Export conversation to Markdown |
| `/` | Search; `n` / `N` next / prev match |
| `q` / `Esc` | Back to conversation list |

**Exporting to Markdown** writes a file named
`conversation_<uuid-prefix>.md` in the current directory containing the
plain text of all messages (tool calls omitted).

**Copying artifact paths:** `c` copies the local disk path to the system
clipboard (via `pbcopy`, `xclip`, `xsel`, or `wl-copy`, whichever is
available). If none are found it prints the path to the status bar instead
so you can triple-click and copy manually. Once you have the path:

```bash
cp /path/to/artifacts/03cba4fd/Dealing-range-builder/dr_builder.py ~/myproject/
```

---

## Probes

The probe scripts were used to reverse-engineer the export schema before
writing the importer. You won't normally need them, but they're useful if
Anthropic changes the export format or you want to inspect a new export
before importing.

```bash
# General schema overview
python3 probe_schema.py conversations.json

# Tool use / artifact deep-dive
python3 probe_tools.py conversations.json
```

Both are read-only and write nothing to disk.

---

## Database schema

```
conversations   uuid, name, summary, account_uuid, created_at, updated_at

messages        uuid, conversation_uuid, parent_message_uuid,
                sender, text, created_at, updated_at

content_blocks  id, message_uuid, type,
                start_timestamp, stop_timestamp,
                text, citations,                     -- type=text
                thinking, summaries, signature,       -- type=thinking
                truncated, cut_off,
                tool_id, tool_name, tool_input,       -- type=tool_use
                integration_name, is_mcp_app,
                mcp_server_url,
                tool_use_id, result_name,             -- type=tool_result
                result_content, is_error

attachments     id, message_uuid, file_name, file_type,
                file_size, extracted_content

files           id, message_uuid, file_uuid, file_name

artifacts       id, conversation_uuid, message_uuid,
                original_path, disk_path, file_name, created_at
```

You can query the database directly with any SQLite client. Example:

```bash
# List all artifacts across all conversations
sqlite3 claude.db "
  SELECT c.name, a.file_name, a.disk_path
  FROM artifacts a
  JOIN conversations c ON c.uuid = a.conversation_uuid
  ORDER BY a.created_at DESC;
"

# Full text search (basic)
sqlite3 claude.db "
  SELECT c.name, m.sender, substr(m.text, 1, 120)
  FROM messages m
  JOIN conversations c ON c.uuid = m.conversation_uuid
  WHERE m.text LIKE '%dealing range%'
  ORDER BY m.created_at;
"
```
