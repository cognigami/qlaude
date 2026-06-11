#!/usr/bin/env python3
"""
probe_tools.py — Dig into tool_use and tool_result content blocks

Reports:
  - All tool names seen and how often
  - Shape of tool_use.input per tool name
  - Shape / content of tool_result.content per tool name
  - Whether tool_result content looks like it contains code/artifacts
  - Sample raw values (truncated) so you can see what's actually in there

Usage:
    python3 probe_tools.py conversations.json
"""

import json
import sys
import collections
from pathlib import Path


SNIP = 300  # max chars to show for any sample value


def snip(s, n=SNIP):
    s = str(s).replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + f"…[+{len(s)-n}]"


def load_export(path):
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    lines = [json.loads(l) for l in raw.splitlines() if l.strip()]
    return lines


def all_content_blocks(conversations):
    for conv in conversations:
        msgs = conv.get("chat_messages") or conv.get("messages") or []
        for msg in msgs:
            for block in msg.get("content") or []:
                yield conv, msg, block


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 probe_tools.py <export_file>")
        sys.exit(1)

    path = Path(sys.argv[1])
    conversations = load_export(path)

    # Collect tool_use and tool_result blocks, paired by tool_use id
    tool_uses    = {}   # id → block
    tool_results = collections.defaultdict(list)  # tool_use_id → [blocks]

    tool_use_names   = collections.Counter()
    tool_result_names = collections.Counter()

    for conv, msg, block in all_content_blocks(conversations):
        btype = block.get("type")
        if btype == "tool_use":
            tid = block.get("id")
            tool_uses[tid] = block
            tool_use_names[block.get("name", "<no-name>")] += 1
        elif btype == "tool_result":
            tid = block.get("tool_use_id")
            tool_results[tid].append(block)
            tool_result_names[block.get("name", "<no-name>")] += 1

    print(f"\n{'='*60}")
    print("Tool Use / Tool Result Deep Probe")
    print(f"{'='*60}")
    print(f"\ntool_use blocks  : {sum(tool_use_names.values())}")
    print(f"tool_result blocks: {sum(tool_result_names.values())}")

    # ── tool names ──
    print(f"\n--- Tool names (tool_use.name) ---")
    for name, count in tool_use_names.most_common():
        print(f"  {count:4d}  {name}")

    # ── per-tool deep dive ──
    print(f"\n--- Per-tool detail ---")

    by_name = collections.defaultdict(list)  # name → list of (tool_use_block, [tool_result_blocks])
    for tid, use_block in tool_uses.items():
        results = tool_results.get(tid, [])
        by_name[use_block.get("name", "<no-name>")].append((use_block, results))

    for tool_name, pairs in sorted(by_name.items()):
        print(f"\n  [{tool_name}]  ×{len(pairs)}")

        # Summarise input keys
        input_keys = collections.Counter()
        for use_block, _ in pairs:
            inp = use_block.get("input") or {}
            for k in inp:
                input_keys[k] += 1
        if input_keys:
            print(f"    tool_use.input keys: {dict(input_keys)}")

        # Show one sample input (truncated)
        sample_input = pairs[0][0].get("input")
        if sample_input:
            print(f"    sample input: {snip(json.dumps(sample_input))}")

        # Summarise result content
        result_content_types = collections.Counter()
        has_text_content = []

        for _, result_blocks in pairs:
            for rb in result_blocks:
                content = rb.get("content")
                if content is None:
                    result_content_types["<no content field>"] += 1
                elif isinstance(content, str):
                    result_content_types["str"] += 1
                    has_text_content.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            result_content_types[f"list[dict.type={item.get('type','?')}]"] += 1
                            if item.get("type") == "text":
                                has_text_content.append(item.get("text", ""))
                        else:
                            result_content_types[f"list[{type(item).__name__}]"] += 1
                else:
                    result_content_types[type(content).__name__] += 1

        if result_content_types:
            print(f"    tool_result.content types: {dict(result_content_types)}")

        # Show sample result text (truncated)
        if has_text_content:
            print(f"    sample result text: {snip(has_text_content[0])}")

        # Flag anything that looks like code
        code_hits = [t for t in has_text_content if "```" in t or "def " in t or "function " in t or "<html" in t.lower()]
        if code_hits:
            print(f"    *** {len(code_hits)} result(s) appear to contain code/markup ***")
            print(f"    code sample: {snip(code_hits[0], 400)}")

    # ── unpaired tool_results (no matching tool_use) ──
    unmatched = [tid for tid in tool_results if tid not in tool_uses]
    if unmatched:
        print(f"\n--- Unmatched tool_results (no tool_use found): {len(unmatched)} ---")
        for tid in unmatched[:5]:
            rb = tool_results[tid][0]
            print(f"  tool_use_id={tid}  name={rb.get('name')}  content={snip(str(rb.get('content')))}")

    print(f"\n{'='*60}")
    print("Probe complete.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
