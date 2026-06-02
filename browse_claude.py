#!/usr/bin/env python3
"""
browse_claude.py — Claude conversation browser

A terminal UI for browsing imported Claude conversations.

Usage:
    python3 browse_claude.py [--db claude.db]

Navigation (global):
    Ctrl-U / Ctrl-D     Scroll up / down half-page
    Mouse wheel         Scroll
    /                   On-screen search (n / N for next / prev match)
    Esc / q             Back / quit

Conversation list:
    j / k / arrows      Move selection
    Enter / number      Open conversation
    s                   Cycle sort (date / name / messages / artifacts)

Conversation view:
    m / M               Jump to next / prev block
    t                   Toggle thinking blocks in viewport
    a                   Show artifact list
    e                   Export conversation to Markdown
    p                   Show/hide artifact panel inline

Artifact panel:
    Enter               Page artifact in $PAGER
    c                   Copy disk path to clipboard (or print to status bar)
"""

import argparse
import curses
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data layer ────────────────────────────────────────────────────────────────

def get_db(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def load_conversations(con, sort="date"):
    order = {
        "date":      "c.updated_at DESC",
        "name":      "c.name ASC",
        "messages":  "msg_count DESC",
        "artifacts": "art_count DESC",
    }.get(sort, "c.updated_at DESC")

    return con.execute(f"""
        SELECT
            c.uuid, c.name, c.summary, c.created_at, c.updated_at,
            COUNT(DISTINCT m.uuid)  AS msg_count,
            COUNT(DISTINCT a.id)    AS art_count
        FROM conversations c
        LEFT JOIN messages  m ON m.conversation_uuid = c.uuid
        LEFT JOIN artifacts a ON a.conversation_uuid = c.uuid
        GROUP BY c.uuid
        ORDER BY {order}
    """).fetchall()


def load_messages(con, conv_uuid):
    return con.execute("""
        SELECT uuid, sender, text, created_at, updated_at, parent_message_uuid
        FROM messages
        WHERE conversation_uuid = ?
        ORDER BY created_at ASC
    """, (conv_uuid,)).fetchall()


def load_blocks(con, message_uuid):
    return con.execute("""
        SELECT * FROM content_blocks
        WHERE message_uuid = ?
        ORDER BY id ASC
    """, (message_uuid,)).fetchall()


def load_artifacts(con, conv_uuid):
    return con.execute("""
        SELECT * FROM artifacts
        WHERE conversation_uuid = ?
        ORDER BY created_at ASC
    """, (conv_uuid,)).fetchall()


# ── Rendering helpers ─────────────────────────────────────────────────────────

GUTTER = 2          # width of left gutter bar
GUTTER_PAD = 1      # space after gutter
INDENT = GUTTER + GUTTER_PAD

SORT_CYCLE = ["date", "name", "messages", "artifacts"]

SENDER_COLORS = {}  # populated after curses.init


@dataclass
class RenderedLine:
    """One terminal line in the rendered conversation."""
    text: str                    # content to display (already wrapped)
    block_idx: int               # which logical block this belongs to
    sender: str                  # 'human' | 'assistant' | 'tool' | 'thinking'
    is_code: bool = False
    is_block_header: bool = False
    artifact_path: Optional[str] = None   # set on code-box header lines


@dataclass
class Block:
    sender: str         # human | assistant | thinking | tool
    lines: list         # list of RenderedLine
    is_thinking: bool = False
    collapsed: bool = False   # thinking blocks start collapsed


def wrap(text: str, width: int) -> list[str]:
    if not text:
        return [""]
    result = []
    for para in text.splitlines():
        if para.strip() == "":
            result.append("")
        else:
            result.extend(textwrap.wrap(para, width) or [""])
    return result


def ext_from_path(path: str) -> str:
    if not path:
        return ""
    return Path(path).suffix.lstrip(".")


def copy_to_clipboard(text: str) -> bool:
    """Try to copy text to system clipboard. Returns True on success."""
    for cmd in (["pbcopy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=2)
                return True
            except Exception:
                pass
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    return False


def render_conversation(messages, blocks_by_msg, width, thinking_expanded: set) -> list[Block]:
    """
    Turn messages + content blocks into a flat list of Block objects,
    each containing RenderedLine objects ready for display.
    """
    content_width = width - INDENT - 1
    rendered_blocks: list[Block] = []

    for msg in messages:
        sender = msg["sender"]  # 'human' | 'assistant'
        blocks = blocks_by_msg.get(msg["uuid"], [])

        # If there are no content blocks, fall back to the flat message.text field.
        # Some messages (especially older exports) store everything there and have
        # an empty content array.
        if not blocks and msg["text"] and msg["text"].strip():
            db = Block(sender=sender, lines=[])
            for line in wrap(msg["text"].strip(), content_width):
                db.lines.append(RenderedLine(line, len(rendered_blocks), sender))
            if db.lines:
                rendered_blocks.append(db)
            continue

        # Group content blocks into display blocks
        # Each tool_use+tool_result pair is one block; text and thinking are their own
        i = 0
        while i < len(blocks):
            b = blocks[i]
            btype = b["type"]

            if btype == "thinking":
                is_expanded = len(rendered_blocks) in thinking_expanded
                tb = Block(sender="thinking", lines=[], is_thinking=True,
                           collapsed=not is_expanded)
                # Header
                summary_text = ""
                try:
                    summaries = json.loads(b["summaries"] or "[]")
                    if summaries:
                        summary_text = summaries[0].get("summary", "")[:80]
                except Exception:
                    pass
                label = f"◇ Thinking{': ' + summary_text if summary_text else ''}"
                tb.lines.append(RenderedLine(label, len(rendered_blocks),
                                             "thinking", is_block_header=True))
                if not tb.collapsed:
                    thinking_text = b["thinking"] or ""
                    for line in wrap(thinking_text, content_width):
                        tb.lines.append(RenderedLine(line, len(rendered_blocks), "thinking"))
                rendered_blocks.append(tb)
                i += 1

            elif btype == "text":
                text_content = b["text"] or ""
                if not text_content.strip():
                    i += 1
                    continue

                db = Block(sender=sender, lines=[])
                # Parse text for markdown code fences
                segments = re.split(r"(```[\w]*\n.*?```)", text_content, flags=re.DOTALL)
                for seg in segments:
                    if seg.startswith("```"):
                        lang_match = re.match(r"```(\w*)\n", seg)
                        lang = lang_match.group(1) if lang_match else ""
                        code = re.sub(r"```\w*\n", "", seg).rstrip("`").rstrip()
                        # code box header
                        box_label = f"┌─ {lang or 'code'} {'─' * max(0, content_width - len(lang) - 5)}┐"
                        db.lines.append(RenderedLine(box_label, len(rendered_blocks),
                                                     sender, is_code=True, is_block_header=True))
                        for cl in code.splitlines():
                            # truncate long lines
                            cl_disp = ("│ " + cl)[:content_width - 1]
                            db.lines.append(RenderedLine(cl_disp, len(rendered_blocks),
                                                         sender, is_code=True))
                        db.lines.append(RenderedLine("└" + "─" * (content_width - 1),
                                                     len(rendered_blocks), sender, is_code=True))
                    elif seg.strip():
                        for line in wrap(seg.strip(), content_width):
                            db.lines.append(RenderedLine(line, len(rendered_blocks), sender))
                if db.lines:
                    rendered_blocks.append(db)
                i += 1

            elif btype == "tool_use":
                tool_name = b["tool_name"] or ""
                inp = {}
                try:
                    inp = json.loads(b["tool_input"] or "{}")
                except Exception:
                    pass

                # Find matching tool_result (next block with same tool_use_id)
                result_block = None
                if i + 1 < len(blocks) and blocks[i + 1]["type"] == "tool_result":
                    result_block = blocks[i + 1]
                    i += 1  # consume it

                # Only render file-producing tools prominently
                if tool_name == "create_file":
                    path     = inp.get("path", "")
                    content  = inp.get("file_text", "")
                    ext      = ext_from_path(path)
                    filename = Path(path).name if path else "file"
                    db = Block(sender="tool", lines=[])
                    box_top = f"┌─ {filename} "
                    box_top += "─" * max(0, content_width - len(box_top) - 1) + "┐"
                    db.lines.append(RenderedLine(box_top, len(rendered_blocks),
                                                 "tool", is_code=True, is_block_header=True))
                    for cl in (content or "").splitlines()[:30]:
                        cl_disp = ("│ " + cl)[:content_width - 1]
                        db.lines.append(RenderedLine(cl_disp, len(rendered_blocks),
                                                     "tool", is_code=True))
                    if content and content.count("\n") > 30:
                        more = content.count("\n") - 30
                        db.lines.append(RenderedLine(f"│  … +{more} lines",
                                                     len(rendered_blocks), "tool", is_code=True))
                    db.lines.append(RenderedLine("└" + "─" * (content_width - 1),
                                                 len(rendered_blocks), "tool", is_code=True,
                                                 artifact_path=path))
                    rendered_blocks.append(db)

                elif tool_name == "bash_tool":
                    cmd = inp.get("command", "")[:120]
                    desc = inp.get("description", "")
                    db = Block(sender="tool", lines=[])
                    label = f"$ {cmd}"
                    db.lines.append(RenderedLine(f"┌─ bash: {desc[:content_width-12]} ─┐",
                                                 len(rendered_blocks), "tool",
                                                 is_code=True, is_block_header=True))
                    for cl in cmd.splitlines()[:5]:
                        db.lines.append(RenderedLine("│ " + cl[:content_width - 3],
                                                     len(rendered_blocks), "tool", is_code=True))
                    # show stdout if interesting
                    if result_block:
                        try:
                            rc_list = result_block["result_content"]
                            rc_items = json.loads(rc_list or "[]")
                            for item in rc_items:
                                if item.get("type") == "text":
                                    try:
                                        rc = json.loads(item["text"])
                                        stdout = (rc.get("stdout") or "").strip()
                                        stderr = (rc.get("stderr") or "").strip()
                                        if stdout:
                                            for sl in stdout.splitlines()[:5]:
                                                db.lines.append(RenderedLine(
                                                    "│ " + sl[:content_width - 3],
                                                    len(rendered_blocks), "tool", is_code=True))
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    db.lines.append(RenderedLine("└" + "─" * (content_width - 1),
                                                 len(rendered_blocks), "tool", is_code=True))
                    rendered_blocks.append(db)

                # Other tools: minimal one-liner
                elif tool_name not in ("view", "str_replace"):
                    db = Block(sender="tool", lines=[])
                    db.lines.append(RenderedLine(f"⚙ {tool_name}",
                                                 len(rendered_blocks), "tool"))
                    rendered_blocks.append(db)

                i += 1
            else:
                i += 1

    return rendered_blocks


# ── Color pairs ───────────────────────────────────────────────────────────────

CP_NORMAL    = 1
CP_HUMAN     = 2
CP_ASSISTANT = 3
CP_TOOL      = 4
CP_THINKING  = 5
CP_HEADER    = 6
CP_CODE      = 7
CP_SELECTED  = 8
CP_STATUS    = 9
CP_SEARCH    = 10
CP_DIM       = 11


def init_colors():
    curses.start_color()
    curses.use_default_colors()
    bg = -1
    curses.init_pair(CP_NORMAL,    -1,                  bg)
    curses.init_pair(CP_HUMAN,     curses.COLOR_CYAN,   bg)
    curses.init_pair(CP_ASSISTANT, curses.COLOR_GREEN,  bg)
    curses.init_pair(CP_TOOL,      curses.COLOR_YELLOW, bg)
    curses.init_pair(CP_THINKING,  curses.COLOR_MAGENTA, bg)
    curses.init_pair(CP_HEADER,    curses.COLOR_WHITE,  bg)
    curses.init_pair(CP_CODE,      curses.COLOR_CYAN,   bg)
    curses.init_pair(CP_SELECTED,  curses.COLOR_BLACK,  curses.COLOR_WHITE)
    curses.init_pair(CP_STATUS,    curses.COLOR_BLACK,  curses.COLOR_BLUE)
    curses.init_pair(CP_SEARCH,    curses.COLOR_BLACK,  curses.COLOR_YELLOW)
    curses.init_pair(CP_DIM,       curses.COLOR_WHITE,  bg)


SENDER_CP = {
    "human":     CP_HUMAN,
    "assistant": CP_ASSISTANT,
    "tool":      CP_TOOL,
    "thinking":  CP_THINKING,
}

GUTTER_CHARS = {
    "human":     "▌",
    "assistant": "▌",
    "tool":      "▌",
    "thinking":  "▸",
}


# ── Screen primitives ─────────────────────────────────────────────────────────

def draw_status(win, text: str, color_pair=CP_STATUS):
    h, w = win.getmaxyx()
    text = text[:w - 1].ljust(w - 1)
    try:
        win.addstr(h - 1, 0, text, curses.color_pair(color_pair) | curses.A_BOLD)
    except curses.error:
        pass


def draw_line(win, y: int, x: int, text: str, attr=0, max_width=None):
    h, w = win.getmaxyx()
    if y < 0 or y >= h - 1:
        return
    if max_width is None:
        max_width = w - x
    text = text[:max_width]
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


# ── Search state ──────────────────────────────────────────────────────────────

@dataclass
class SearchState:
    query: str = ""
    active: bool = False
    matches: list = field(default_factory=list)   # list of line indices
    current: int = 0

    def find(self, lines: list[str]):
        if not self.query:
            self.matches = []
            return
        q = self.query.lower()
        self.matches = [i for i, l in enumerate(lines) if q in l.lower()]
        self.current = 0

    def next_match(self):
        if self.matches:
            self.current = (self.current + 1) % len(self.matches)

    def prev_match(self):
        if self.matches:
            self.current = (self.current - 1) % len(self.matches)

    def current_line(self):
        if self.matches:
            return self.matches[self.current]
        return None


# ── Conversation list screen ──────────────────────────────────────────────────

def screen_conv_list(stdscr, con):
    sort_idx  = 0
    selected  = 0
    scroll    = 0
    search    = SearchState()
    convs     = load_conversations(con, SORT_CYCLE[sort_idx])

    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        list_h = h - 3   # header + status
        visible = convs   # could be filtered by search later

        # Header
        title = f" Claude Conversations  [{SORT_CYCLE[sort_idx]}]  {len(visible)} conversations "
        draw_line(stdscr, 0, 0, title.ljust(w), curses.color_pair(CP_STATUS) | curses.A_BOLD)

        col_w = w - 4
        draw_line(stdscr, 1, 2,
                  f"{'#':<4}{'Title':<{col_w - 32}}{'Msgs':>6}  {'Art':>4}  {'Updated':>10}",
                  curses.color_pair(CP_DIM))

        for i, conv in enumerate(visible):
            if i < scroll or i >= scroll + list_h - 1:
                continue
            y     = i - scroll + 2
            name  = (conv["name"] or "(untitled)")[:col_w - 32]
            msgs  = conv["msg_count"] or 0
            arts  = conv["art_count"] or 0
            date  = (conv["updated_at"] or "")[:10]
            line  = f"{i+1:<4}{name:<{col_w - 32}}{msgs:>6}  {arts:>4}  {date:>10}"

            attr = curses.color_pair(CP_SELECTED) if i == selected else curses.color_pair(CP_NORMAL)
            if search.active and search.query and search.query.lower() in name.lower():
                attr |= curses.A_BOLD
            draw_line(stdscr, y, 0, line.ljust(w - 1), attr)

        # Status bar
        if search.active:
            draw_status(stdscr,
                f" /{search.query}  {len(search.matches)} matches  n/N next/prev  Esc cancel")
        else:
            draw_status(stdscr,
                " j/k move  Enter open  s sort  / search  q quit")

        stdscr.refresh()

        key = stdscr.getch()

        # Mouse
        if key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                if bstate & curses.BUTTON4_PRESSED:
                    scroll = max(0, scroll - 3)
                elif bstate & curses.BUTTON5_PRESSED:
                    scroll = min(max(0, len(visible) - list_h), scroll + 3)
                elif bstate & curses.BUTTON1_CLICKED:
                    clicked = scroll + (my - 2)
                    if 0 <= clicked < len(visible):
                        selected = clicked
            except curses.error:
                pass
            continue

        # Search mode
        if search.active:
            if key == 27:   # Esc
                search.active = False
                search.query  = ""
                search.matches = []
            elif key in (curses.KEY_BACKSPACE, 127):
                search.query = search.query[:-1]
                search.find([c["name"] or "" for c in visible])
            elif key == ord("n"):
                search.next_match()
                if search.current_line() is not None:
                    selected = search.matches[search.current]
            elif key == ord("N"):
                search.prev_match()
                if search.current_line() is not None:
                    selected = search.matches[search.current]
            elif 32 <= key < 256:
                search.query += chr(key)
                search.find([c["name"] or "" for c in visible])
                if search.matches:
                    selected = search.matches[0]
            # keep selected in view
            if selected < scroll:
                scroll = selected
            elif selected >= scroll + list_h - 1:
                scroll = selected - list_h + 2
            continue

        # Normal mode
        if key in (ord("q"), 27):
            return None  # quit

        elif key in (ord("j"), curses.KEY_DOWN):
            selected = min(len(visible) - 1, selected + 1)
        elif key in (ord("k"), curses.KEY_UP):
            selected = max(0, selected - 1)

        elif key == 21:  # Ctrl-U
            selected = max(0, selected - list_h // 2)
            scroll   = max(0, scroll   - list_h // 2)
        elif key == 4:   # Ctrl-D
            selected = min(len(visible) - 1, selected + list_h // 2)
            scroll   = min(max(0, len(visible) - list_h), scroll + list_h // 2)

        elif key == ord("s"):
            sort_idx = (sort_idx + 1) % len(SORT_CYCLE)
            convs    = load_conversations(con, SORT_CYCLE[sort_idx])
            visible  = convs
            selected = 0; scroll = 0

        elif key == ord("/"):
            search.active = True
            search.query  = ""

        elif key in (curses.KEY_ENTER, 10, 13):
            if visible:
                return visible[selected]

        elif 49 <= key <= 57:   # digits 1-9
            idx = key - 49
            if idx < len(visible):
                return visible[idx]

        # Scroll to keep selected visible
        if selected < scroll:
            scroll = selected
        elif selected >= scroll + list_h - 1:
            scroll = selected - list_h + 2


# ── Conversation screen ───────────────────────────────────────────────────────

def export_markdown(conv, messages, blocks_by_msg, path: Path):
    lines = [f"# {conv['name'] or 'Conversation'}", ""]
    for msg in messages:
        sender = msg["sender"].capitalize()
        lines.append(f"## {sender}")
        lines.append("")
        for b in blocks_by_msg.get(msg["uuid"], []):
            if b["type"] == "text" and b["text"]:
                lines.append(b["text"])
                lines.append("")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def screen_conversation(stdscr, con, conv):
    conv_uuid = conv["uuid"]
    messages  = load_messages(con, conv_uuid)
    artifacts = load_artifacts(con, conv_uuid)

    blocks_by_msg = {}
    for msg in messages:
        blocks_by_msg[msg["uuid"]] = load_blocks(con, msg["uuid"])

    thinking_expanded: set = set()   # block indices currently expanded
    scroll    = 0
    search    = SearchState()
    show_artifacts = False
    status_msg = ""

    def rebuild():
        return render_conversation(messages, blocks_by_msg,
                                   stdscr.getmaxyx()[1], thinking_expanded)

    rendered = rebuild()

    # Flat list of all lines for searching / scrolling
    def flat_lines(rendered):
        return [rl.text for block in rendered for rl in block.lines]

    # Block index of each flat line
    def line_block_indices(rendered):
        return [rl.block_idx for block in rendered for rl in block.lines]

    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

    while True:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        art_panel_h = min(len(artifacts) + 3, h // 3) if show_artifacts else 0
        conv_h = h - 2 - art_panel_h   # usable lines for conversation

        lines       = flat_lines(rendered)
        block_idx   = line_block_indices(rendered)
        total_lines = len(lines)

        # Clamp scroll
        scroll = max(0, min(scroll, max(0, total_lines - conv_h)))

        # Header
        name  = (conv["name"] or "Conversation")[:w - 20]
        hdr   = f" {name}  [{len(messages)} msgs, {len(artifacts)} artifacts] "
        draw_line(stdscr, 0, 0, hdr.ljust(w), curses.color_pair(CP_STATUS) | curses.A_BOLD)

        # Render visible lines
        for row in range(conv_h):
            li = scroll + row
            if li >= total_lines:
                break
            y  = row + 1

            # Find which block this line belongs to
            bidx   = block_idx[li]
            block  = rendered[bidx] if bidx < len(rendered) else None
            sender = block.sender if block else "assistant"
            rl     = None
            # find the RenderedLine object
            flat_i = 0
            for bl in rendered:
                for rline in bl.lines:
                    if flat_i == li:
                        rl = rline
                        break
                    flat_i += 1
                if rl:
                    break

            cp = SENDER_CP.get(sender, CP_NORMAL)

            # Gutter
            gchar = GUTTER_CHARS.get(sender, "▌")
            try:
                stdscr.addstr(y, 0, gchar, curses.color_pair(cp) | curses.A_BOLD)
            except curses.error:
                pass

            # Content
            text = (rl.text if rl else lines[li])
            attr = curses.color_pair(cp)
            if rl and rl.is_code:
                attr = curses.color_pair(CP_CODE)
            if rl and rl.is_block_header:
                attr |= curses.A_BOLD

            # Search highlight
            if search.active and search.query and search.query.lower() in text.lower():
                attr = curses.color_pair(CP_SEARCH) | curses.A_BOLD

            draw_line(stdscr, y, INDENT, text, attr, max_width=w - INDENT - 1)

            # Artifact path at right of bottom code box line
            if rl and rl.artifact_path:
                ap = f" {rl.artifact_path} "[-min(30, w // 3):]
                draw_line(stdscr, y, w - len(ap) - 1, ap,
                          curses.color_pair(CP_TOOL) | curses.A_DIM)

        # Artifact panel
        if show_artifacts and art_panel_h > 0:
            panel_y = h - 1 - art_panel_h
            draw_line(stdscr, panel_y, 0,
                      f" Artifacts ({len(artifacts)}) ".center(w, "─"),
                      curses.color_pair(CP_STATUS))
            for i, art in enumerate(artifacts):
                if i >= art_panel_h - 2:
                    break
                fn   = art["file_name"] or ""
                dp   = art["disk_path"] or ""
                line = f"  {i+1}. {fn:<30} {dp}"[:w - 1]
                draw_line(stdscr, panel_y + 1 + i, 0, line.ljust(w - 1),
                          curses.color_pair(CP_NORMAL))

        # Status bar
        if status_msg:
            draw_status(stdscr, f" {status_msg}")
            status_msg = ""
        elif search.active:
            draw_status(stdscr,
                f" /{search.query}  {len(search.matches)} match(es)  n/N next/prev  Esc cancel")
        else:
            draw_status(stdscr,
                " m/M block  t think  a artifacts  e export  / search  q back")

        stdscr.refresh()

        key = stdscr.getch()

        # Mouse wheel scroll
        if key == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
                if bstate & curses.BUTTON4_PRESSED:
                    scroll = max(0, scroll - 3)
                elif bstate & curses.BUTTON5_PRESSED:
                    scroll = min(max(0, total_lines - conv_h), scroll + 3)
            except curses.error:
                pass
            continue

        # Search mode
        if search.active:
            if key == 27:
                search.active = False; search.query = ""; search.matches = []
            elif key in (curses.KEY_BACKSPACE, 127):
                search.query = search.query[:-1]
                search.find(lines)
            elif key == ord("n"):
                search.next_match()
                if search.current_line() is not None:
                    scroll = max(0, search.current_line() - conv_h // 3)
            elif key == ord("N"):
                search.prev_match()
                if search.current_line() is not None:
                    scroll = max(0, search.current_line() - conv_h // 3)
            elif 32 <= key < 256:
                search.query += chr(key)
                search.find(lines)
                if search.current_line() is not None:
                    scroll = max(0, search.current_line() - conv_h // 3)
            continue

        # Navigation
        if key in (ord("q"), 27):
            return

        elif key == 21:   # Ctrl-U
            scroll = max(0, scroll - conv_h // 2)
        elif key == 4:    # Ctrl-D
            scroll = min(max(0, total_lines - conv_h), scroll + conv_h // 2)
        elif key == curses.KEY_UP:
            scroll = max(0, scroll - 1)
        elif key == curses.KEY_DOWN:
            scroll = min(max(0, total_lines - conv_h), scroll + 1)

        elif key == ord("m"):
            # Next block boundary — find the block_idx at current scroll top,
            # then jump to the first line of the next block
            if total_lines > 0:
                cur_bidx = block_idx[min(scroll, total_lines - 1)]
                for li in range(scroll + 1, total_lines):
                    if block_idx[li] > cur_bidx:
                        scroll = li
                        break

        elif key == ord("M"):
            # Prev block boundary
            if total_lines > 0:
                cur_bidx = block_idx[min(scroll, total_lines - 1)]
                target_bidx = cur_bidx - 1
                if target_bidx >= 0:
                    for li in range(total_lines - 1, -1, -1):
                        if block_idx[li] == target_bidx:
                            scroll = li
                            break

        elif key == ord("t"):
            # Toggle thinking blocks that are currently in the viewport
            # Anchor: remember the block_idx at the top of the viewport
            if total_lines > 0:
                anchor_bidx = block_idx[min(scroll, total_lines - 1)]
                anchor_line_in_block = scroll - next(
                    (i for i, bi in enumerate(block_idx) if bi == anchor_bidx), scroll)

                # Find thinking blocks visible in viewport
                visible_thinking = set()
                for li in range(scroll, min(scroll + conv_h, total_lines)):
                    bidx = block_idx[li]
                    if bidx < len(rendered) and rendered[bidx].is_thinking:
                        visible_thinking.add(bidx)

                if visible_thinking:
                    # Toggle: if any are collapsed, expand all; else collapse all
                    any_collapsed = any(
                        rendered[bi].collapsed for bi in visible_thinking)
                    for bi in visible_thinking:
                        if any_collapsed:
                            thinking_expanded.add(bi)
                        else:
                            thinking_expanded.discard(bi)

                    # Rebuild and restore scroll position to same logical position
                    rendered = rebuild()
                    lines     = flat_lines(rendered)
                    block_idx = line_block_indices(rendered)
                    total_lines = len(lines)

                    # Find the first line of anchor block in new rendering
                    new_anchor = next(
                        (i for i, bi in enumerate(block_idx) if bi == anchor_bidx), 0)
                    scroll = max(0, new_anchor + anchor_line_in_block)
                    scroll = min(scroll, max(0, total_lines - conv_h))

        elif key == ord("a"):
            show_artifacts = not show_artifacts

        elif key == ord("/"):
            search.active = True
            search.query  = ""

        elif key == ord("e"):
            # Export to markdown
            out = Path(f"conversation_{conv_uuid[:8]}.md")
            export_markdown(conv, messages, blocks_by_msg, out)
            status_msg = f"Exported to {out.resolve()}"

        elif key == ord("c"):
            # Copy path of first artifact (or show list if multiple)
            if artifacts:
                dp = artifacts[0]["disk_path"] or ""
                if copy_to_clipboard(dp):
                    status_msg = f"Copied: {dp}"
                else:
                    status_msg = f"Path: {dp}"

        # Digit: open nth artifact in pager
        elif 49 <= key <= 57 and show_artifacts:
            idx = key - 49
            if idx < len(artifacts):
                dp = artifacts[idx]["disk_path"]
                if dp and Path(dp).exists():
                    curses.endwin()
                    pager = os.environ.get("PAGER", "less")
                    subprocess.run([pager, dp])
                    stdscr = curses.initscr()
                    init_colors()
                    curses.curs_set(0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(stdscr, db_path: str):
    init_colors()
    curses.curs_set(0)

    con = get_db(db_path)

    while True:
        conv = screen_conv_list(stdscr, con)
        if conv is None:
            break
        screen_conversation(stdscr, con, conv)

    con.close()


def run():
    parser = argparse.ArgumentParser(description="Browse Claude conversations")
    parser.add_argument("--db", default="claude.db", help="Path to claude.db")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"Database not found: {args.db}")
        print("Run import_claude.py first.")
        sys.exit(1)

    curses.wrapper(main, args.db)


if __name__ == "__main__":
    run()
