#!/usr/bin/env python3
"""
probe_schema.py — Claude export schema validator

Reads a Claude conversations export (JSON or JSONL) and reports:
  - Top-level structure (array vs object, keys present)
  - Conversation-level fields and types
  - Message-level fields and types
  - Content block types and shapes
  - Attachment / file shapes
  - Any unexpected or missing fields

Produces a human-readable report to stdout. No data is written to disk.

Usage:
    python3 probe_schema.py conversations.json
    python3 probe_schema.py conversations.jsonl
"""

import json
import sys
import collections
from pathlib import Path


# ── helpers ──────────────────────────────────────────────────────────────────

def type_label(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        return f"dict{{{', '.join(value.keys())}}}"
    return type(value).__name__


def summarise_keys(objects, label, max_show=5):
    """Given a list of dicts, print which keys appear, their types, and coverage."""
    key_types   = collections.defaultdict(set)   # key → set of type labels seen
    key_counts  = collections.Counter()           # key → how many objects have it
    total = len(objects)

    for obj in objects:
        if not isinstance(obj, dict):
            print(f"  [!] Expected dict, got {type(obj).__name__}")
            continue
        for k, v in obj.items():
            key_counts[k] += 1
            key_types[k].add(type_label(v))

    print(f"\n  {label} ({total} items):")
    for k in sorted(key_counts):
        coverage = key_counts[k]
        types    = ", ".join(sorted(key_types[k]))
        flag = "" if coverage == total else f"  ← only {coverage}/{total}"
        print(f"    {k:30s} {types}{flag}")


def probe_content_blocks(all_messages):
    """Dig into message['content'] arrays and report block types."""
    block_type_shapes = collections.defaultdict(list)  # type → list of key-sets

    for msg in all_messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                block_type_shapes["<non-dict>"].append(set())
                continue
            btype = block.get("type", "<no-type>")
            block_type_shapes[btype].append(set(block.keys()))

    print(f"\n  Content block types across all messages:")
    if not block_type_shapes:
        print("    (none found — content field absent or always empty)")
        return

    for btype, key_sets in sorted(block_type_shapes.items()):
        # union of all keys seen for this block type
        all_keys = sorted(set().union(*key_sets))
        print(f"    [{btype}]  ×{len(key_sets)}  keys: {', '.join(all_keys)}")


def probe_attachments(all_messages):
    """Report shapes of attachments and files arrays."""
    att_key_counts  = collections.Counter()
    att_key_types   = collections.defaultdict(set)
    att_total       = 0
    file_key_counts = collections.Counter()
    file_key_types  = collections.defaultdict(set)
    file_total      = 0

    for msg in all_messages:
        for att in msg.get("attachments") or []:
            att_total += 1
            if isinstance(att, dict):
                for k, v in att.items():
                    att_key_counts[k] += 1
                    att_key_types[k].add(type_label(v))

        for f in msg.get("files") or []:
            file_total += 1
            if isinstance(f, dict):
                for k, v in f.items():
                    file_key_counts[k] += 1
                    file_key_types[k].add(type_label(v))

    def _print_shape(label, total, key_counts, key_types):
        print(f"\n  {label} ({total} items total):")
        if not key_counts:
            print("    (none found)")
            return
        for k in sorted(key_counts):
            types = ", ".join(sorted(key_types[k]))
            flag  = "" if key_counts[k] == total else f"  ← {key_counts[k]}/{total}"
            print(f"    {k:30s} {types}{flag}")

    _print_shape("attachments[]", att_total, att_key_counts,  att_key_types)
    _print_shape("files[]",       file_total, file_key_counts, file_key_types)


def sample_values(conversations, field_path, n=3):
    """Print a few example values for a dotted field path like 'chat_messages.sender'."""
    parts = field_path.split(".")
    seen  = []

    def extract(obj, parts):
        if not parts:
            return [obj]
        key = parts[0]
        rest = parts[1:]
        if isinstance(obj, dict) and key in obj:
            return extract(obj[key], rest)
        if isinstance(obj, list):
            results = []
            for item in obj:
                results.extend(extract(item, rest))
            return results
        return []

    for conv in conversations:
        seen.extend(extract(conv, parts))
        if len(seen) >= n * 3:
            break

    unique = list(dict.fromkeys(str(v) for v in seen))[:n]
    print(f"    sample {field_path}: {unique}")


# ── main ─────────────────────────────────────────────────────────────────────

def load_export(path: Path):
    raw = path.read_text(encoding="utf-8")

    # Try JSON array first
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data, "json-array"
        if isinstance(data, dict):
            return [data], "json-object"
    except json.JSONDecodeError:
        pass

    # Fall back to JSONL
    conversations = []
    for i, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            conversations.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"  [!] Line {i} failed to parse: {e}")
    if conversations:
        return conversations, "jsonl"

    raise ValueError("Could not parse file as JSON array, JSON object, or JSONL.")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 probe_schema.py <export_file>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"Claude Export Schema Probe")
    print(f"File: {path}  ({path.stat().st_size / 1024:.1f} KB)")
    print(f"{'='*60}")

    conversations, fmt = load_export(path)
    print(f"\nFormat detected : {fmt}")
    print(f"Conversations   : {len(conversations)}")

    # ── top-level conversation keys ──
    summarise_keys(conversations, "Conversation-level keys")

    sample_values(conversations, "uuid")
    sample_values(conversations, "name")
    sample_values(conversations, "created_at")

    # ── message-level keys ──
    all_messages = []
    for conv in conversations:
        msgs = conv.get("chat_messages") or conv.get("messages") or []
        all_messages.extend(msgs)

    print(f"\n  Total messages across all conversations: {len(all_messages)}")

    summarise_keys(all_messages, "Message-level keys")

    sample_values(conversations, "chat_messages.sender")
    sample_values(conversations, "chat_messages.text")

    # ── content blocks ──
    probe_content_blocks(all_messages)

    # ── attachments / files ──
    probe_attachments(all_messages)

    # ── account field ──
    account_shapes = [c.get("account") for c in conversations if "account" in c]
    if account_shapes:
        print(f"\n  account field shapes ({len(account_shapes)} convs have it):")
        for shape in list(dict.fromkeys(
                str(sorted(a.keys())) if isinstance(a, dict) else str(a)
                for a in account_shapes))[:5]:
            print(f"    {shape}")

    print(f"\n{'='*60}")
    print("Probe complete. No data was written to disk.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
