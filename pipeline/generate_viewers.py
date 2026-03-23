#!/usr/bin/env python3
"""
Generate interactive HTML trace viewers from a CSV of task responses.

Usage:
    python generate_csv_viewers.py [path/to/tasks.csv]

If no CSV path is provided, defaults to output/sheet_data.csv
(written by run.py after pulling from Google Sheets).

CSV Format (column names are case-insensitive):
    Required columns:
        taskid     – Unique task identifier
        response   – JSON string containing the full task response, with a
                     "turns" array. Each turn has step objects keyed by step-ID.
                     Recognized step types:
                       • TextCollection  – persona_selection, oracle_events
                       • PromptInput     – prompt content
                       • RubricCriteriaBuilder – rubric criteria list
                       • ExternalApp     – agentRuns, verifierRuns, deployData

    Optional columns:
        trajectory_urls  – JSON string with signed S3 URLs for trajectory files.
                           If present, the script merges ExternalApp steps from
                           this into the response before processing.
        email            – Annotator email (shown in viewer nav bar)
        annotator        – Annotator name  (shown in viewer nav bar)

Trajectory Files:
    The script looks for downloaded trajectory JSONs in a "trajectories/"
    subdirectory next to the CSV file, organized as:
        trajectories/<taskid>/prompt-trajectory-XXXX.json
    These are OpenTelemetry span dumps that get parsed into chat messages.
    If the directory doesn't exist, the viewer still generates but without
    the detailed chat UI (only run summaries from the response JSON).

Output:
    • One <taskid>_viewer.html per task (interactive trace viewer)
    • index.html homepage with summary table, charts, and links to all viewers

Features:
    • Chat bubble UI with tool-call modals and collapsible thinking blocks
    • Rubric scorecard slide-out panel per run
    • Oracle events slide-out panel per run
    • Overview page with runs summary, tool breakdown, and markdown-rendered prompt
    • Homepage with box plots, tool usage / verifier pass / persona bar charts
    • Interactive walkthrough tours on first visit
    • Dark mode toggle
    • Signed S3 URLs are automatically redacted from the output HTML
"""

import csv
import json
import os
import re
import sys
import html as html_mod

csv.field_size_limit(sys.maxsize)

# Regex to match signed S3 URLs (with query-string credentials)
_S3_SIGNED_RE = re.compile(
    r'https?://[^\s"\'<>]*\.s3[^\s"\'<>]*[?&]X-Amz-[^\s"\'<>]*'
)

def _redact_s3(val):
    """Replace signed S3 URLs with [REDACTED] in a string."""
    return _S3_SIGNED_RE.sub("[S3 URL REDACTED]", val)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_OUTPUT = os.path.join(SCRIPT_DIR, "output")

# CLI:  generate_viewers.py [csv_path] [--html-dir DIR]
# Positional arg 1 = CSV path, --html-dir = where to write HTML viewers
_csv_arg = None
_html_dir_arg = None
_argv = sys.argv[1:]
while _argv:
    if _argv[0] == "--html-dir" and len(_argv) > 1:
        _html_dir_arg = _argv[1]
        _argv = _argv[2:]
    elif _argv[0] in ("-h", "--help"):
        break
    elif _csv_arg is None and not _argv[0].startswith("-"):
        _csv_arg = _argv[0]
        _argv = _argv[1:]
    else:
        _argv = _argv[1:]

OUT_DIR = _html_dir_arg or _DEFAULT_OUTPUT        # where HTML viewers go
DATA_DIR = os.path.join(SCRIPT_DIR, "output")     # where CSV + trajectories live
CSV_PATH = _csv_arg or os.path.join(DATA_DIR, "sheet_data.csv")
TRAJ_DIR = os.path.join(DATA_DIR, "trajectories")
os.makedirs(OUT_DIR, exist_ok=True)


def _esc(text):
    return html_mod.escape(str(text)) if text else ""


def _js_safe(text):
    """Escape a string for embedding inside a JS template literal."""
    return (str(text)
            .replace("\\", "\\\\")
            .replace("`", "\\`")
            .replace("$", "\\$")
            .replace("</", "<\\/"))


# ═══════════════════════════════════════════════════════════════════
# Trajectory parsing → chat messages
# ═══════════════════════════════════════════════════════════════════

def parse_trajectory_to_messages(spans):
    """
    Parse OpenTelemetry spans into a list of chat messages.
    Each message: {role, text, thinking, tools: [{name, args, result}], ts}
    """
    spans.sort(key=lambda s: s.get("start_time", ""))

    # Index tool spans by their span_id for matching
    tool_span_by_id = {}
    for s in spans:
        name = s.get("name", "")
        if name.startswith("mcp__") or name == "TodoWrite":
            tool_span_by_id[s["context"]["span_id"]] = s

    # Also index tool spans by start_time for fuzzy matching
    tool_spans_by_time = []
    for s in spans:
        name = s.get("name", "")
        if name.startswith("mcp__") or name == "TodoWrite":
            attrs = s.get("attributes", {})
            tool_spans_by_time.append({
                "span": s,
                "name": name,
                "start": s["start_time"],
                "input": attrs.get("gen_ai.prompt", ""),
                "output": attrs.get("gen_ai.completion", ""),
            })

    # Track which tool spans have been used
    used_tool_spans = set()

    # Process assistant turns into messages
    # Group consecutive turns that form one logical "assistant message"
    messages = []
    current_msg = None  # accumulator

    # First message is always user (prompt)
    # Get user prompt from the first turn's gen_ai.prompt
    first_prompt = None
    for s in spans:
        if s.get("name") == "claude.assistant.turn":
            attrs = s.get("attributes", {})
            prompt_raw = attrs.get("gen_ai.prompt", "")
            try:
                prompt = json.loads(prompt_raw) if prompt_raw else {}
            except:
                prompt = {}
            msgs = prompt.get("messages", [])
            if msgs:
                # Find the first user message
                for m in msgs:
                    if isinstance(m, dict) and m.get("role") == "user":
                        content = m.get("content", "")
                        if isinstance(content, list):
                            # Extract text from content blocks
                            text_parts = []
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text_parts.append(block.get("text", ""))
                                elif isinstance(block, str):
                                    text_parts.append(block)
                            content = "\n".join(text_parts)
                        first_prompt = content
                        break
            if first_prompt:
                break

    if first_prompt:
        messages.append({
            "role": "user",
            "text": first_prompt,
            "thinking": None,
            "tools": [],
            "ts": "",
        })

    def _flush_msg():
        nonlocal current_msg
        if current_msg and (current_msg["text"] or current_msg["thinking"] or current_msg["tools"]):
            messages.append(current_msg)
        current_msg = None

    def _ensure_msg(ts=""):
        nonlocal current_msg
        if current_msg is None:
            current_msg = {"role": "assistant", "text": "", "thinking": None, "tools": [], "ts": ts}

    for s in spans:
        if s.get("name") != "claude.assistant.turn":
            continue

        attrs = s.get("attributes", {})
        completion_raw = attrs.get("gen_ai.completion", "")
        try:
            completion = json.loads(completion_raw) if completion_raw else {}
        except:
            completion = {}

        content_blocks = completion.get("content", [])
        if isinstance(content_blocks, str):
            content_blocks = [{"type": "text", "text": content_blocks}]

        ts = s.get("start_time", "")[11:19]  # HH:MM:SS

        for block in content_blocks:
            btype = block.get("type", "text")

            if btype == "thinking":
                thinking_text = block.get("thinking", "")
                if thinking_text.strip():
                    # If current_msg already has tools, this thinking block
                    # signals a new logical turn (after tool results came back).
                    # Flush the previous message so the thinking isn't lost.
                    if current_msg and current_msg["tools"]:
                        _flush_msg()
                    _ensure_msg(ts)
                    current_msg["thinking"] = thinking_text

            elif btype == "text":
                text = block.get("text", "")
                if text.strip():
                    _ensure_msg(ts)
                    if current_msg["text"]:
                        current_msg["text"] += "\n\n" + text
                    else:
                        current_msg["text"] = text

            elif btype == "tool_use":
                _ensure_msg(ts)
                tool_name = block.get("name", "unknown_tool")
                tool_input = block.get("input", {})
                tool_id = block.get("id", "")

                # Find matching tool span result
                tool_result = "(no output)"
                tool_status = ""
                tool_duration = ""

                # Match by tool name and time proximity
                best_match = None
                for tsi, ts_item in enumerate(tool_spans_by_time):
                    if tsi in used_tool_spans:
                        continue
                    if tool_name in ts_item["name"] or (tool_name == "TodoWrite" and ts_item["name"] == "TodoWrite"):
                        best_match = tsi
                        break

                if best_match is not None:
                    used_tool_spans.add(best_match)
                    ts_item = tool_spans_by_time[best_match]
                    try:
                        out = json.loads(ts_item["output"]) if ts_item["output"] else {}
                        tool_result = out.get("output", json.dumps(out, indent=2))
                    except:
                        tool_result = ts_item["output"] or "(no output)"

                # Clean up tool name for display
                display_name = tool_name
                if "__" in display_name:
                    parts = display_name.split("__")
                    display_name = parts[-1] if len(parts) > 1 else display_name

                current_msg["tools"].append({
                    "name": display_name,
                    "full_name": tool_name,
                    "args": tool_input,
                    "result": tool_result,
                    "status": tool_status,
                    "duration": tool_duration,
                })

        # Check if this turn produced tool_use blocks — if so, the next turn
        # will be the tool results followed by new assistant output, so flush
        has_tool_use = any(b.get("type") == "tool_use" for b in content_blocks)
        has_text = any(b.get("type") == "text" and b.get("text", "").strip() for b in content_blocks)

        # If we have text without tool_use, this is a final response — flush
        if has_text and not has_tool_use:
            _flush_msg()

    # Flush any remaining message
    _flush_msg()

    return messages


# ═══════════════════════════════════════════════════════════════════
# HTML generation
# ═══════════════════════════════════════════════════════════════════

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box;}
:root{
  --bg:#f0f2f5;--bg2:#fff;--bg3:#f6f8fa;--border:#d8dee4;
  --text:#1b1f24;--text2:#4d5561;--text3:#656d76;--text4:#8b949e;
  --accent:#0969da;--green:#1a7f37;--red:#cf222e;--amber:#9a6700;
  --buser:#0969da;--buser-t:#fff;--bai:#fff;--bai-b:#e1e4e8;--bai-t:#1b1f24;
  --tool-bg:#f6f8fa;--tool-b:#e1e4e8;--modal-bg:#fff;--overlay:rgba(0,0,0,.4);--code:#f6f8fa;
}
body.dark{
  --bg:#0d1117;--bg2:#161b22;--bg3:#1c2129;--border:#30363d;
  --text:#e6edf3;--text2:#b1bac4;--text3:#8b949e;--text4:#6e7681;
  --accent:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922;
  --buser:#1f6feb;--buser-t:#fff;--bai:#161b22;--bai-b:#30363d;--bai-t:#e6edf3;
  --tool-bg:#0d1117;--tool-b:#30363d;--modal-bg:#161b22;--overlay:rgba(0,0,0,.6);--code:#0d1117;
}
body{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;height:100vh;display:flex;flex-direction:column;}

/* ── Top bar ── */
.topbar{background:var(--bg2);border-bottom:1px solid var(--border);padding:10px 20px;display:flex;align-items:center;gap:12px;flex-shrink:0;}
.topbar h1{font-size:.92rem;font-weight:700;white-space:nowrap;}
.topbar .info{font-size:.7rem;color:var(--text4);white-space:nowrap;}
.btn{background:var(--bg3);border:1px solid var(--border);border-radius:16px;padding:4px 11px;font-size:.7rem;font-weight:600;color:var(--text3);cursor:pointer;font-family:inherit;white-space:nowrap;}
.btn:hover{opacity:.8;}

/* ── Tabs in topbar ── */
.top-tabs{display:flex;gap:2px;margin-left:8px;}
.top-tab{padding:5px 12px;font-size:.72rem;font-weight:600;color:var(--text3);cursor:pointer;border:none;background:none;font-family:inherit;border-bottom:2px solid transparent;transition:all .15s;}
.top-tab:hover{color:var(--text);}.top-tab.active{color:var(--accent);border-bottom-color:var(--accent);}

/* ── Main layout ── */
.main-area{flex:1;display:flex;overflow:hidden;position:relative;}
.page{display:none;flex:1;overflow:hidden;min-width:0;}.page.active{display:flex;}

/* ── Run selector ── */
.run-selector{display:flex;gap:4px;padding:10px 20px;background:var(--bg2);border-bottom:1px solid var(--border);flex-shrink:0;flex-wrap:wrap;}
.run-btn{padding:5px 12px;font-size:.7rem;font-weight:600;color:var(--text3);cursor:pointer;border:1px solid var(--border);background:var(--bg3);border-radius:6px;font-family:inherit;display:flex;align-items:center;gap:5px;transition:all .15s;}
.run-btn:hover{background:var(--border);}.run-btn.active{color:var(--accent);border-color:var(--accent);background:rgba(9,105,218,0.04);}
body.dark .run-btn.active{background:rgba(88,166,255,0.06);}
.badge{font-size:.55rem;font-weight:600;color:#fff;padding:1px 6px;border-radius:3px;text-transform:uppercase;}

/* ── Chat area ── */
.chat-area{flex:1;display:flex;flex-direction:column;overflow:hidden;position:relative;}
.chat-container{flex:1;overflow-y:auto;padding:20px 0;}
.chat-inner{max-width:800px;margin:0 auto;padding:0 20px;display:flex;flex-direction:column;gap:3px;}
.msg{display:flex;gap:8px;max-width:88%;}
.msg-user{align-self:flex-end;flex-direction:row-reverse;}
.msg-assistant{align-self:flex-start;}
.avatar{width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.65rem;font-weight:700;flex-shrink:0;margin-top:2px;}
.avatar-user{background:var(--accent);color:#fff;}
.avatar-assistant{background:var(--text);color:var(--bg);}
.bubble{padding:9px 13px;border-radius:14px;font-size:.8rem;line-height:1.55;word-break:break-word;}
.bubble-user{background:var(--buser);color:var(--buser-t);border-bottom-right-radius:4px;}
.bubble-assistant{background:var(--bai);color:var(--bai-t);border:1px solid var(--bai-b);border-bottom-left-radius:4px;}
.msg-ts{font-size:.58rem;color:var(--text4);margin-top:2px;padding:0 4px;}
.msg-user .msg-ts{text-align:right;}

/* ── Thinking ── */
.thinking{font-size:.66rem;color:var(--text4);font-style:italic;margin-bottom:3px;cursor:pointer;}
.thinking::before{content:'\\1F4AD ';}
.thinking-text{display:none;font-size:.64rem;color:var(--text4);background:var(--bg3);padding:5px 7px;border-radius:5px;margin-bottom:3px;font-style:normal;max-height:200px;overflow-y:auto;white-space:pre-wrap;font-family:'SF Mono',ui-monospace,monospace;line-height:1.4;}

/* ── Tool chips ── */
.tool-group{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px;}
.tool-chip{display:inline-flex;align-items:center;gap:4px;background:var(--tool-bg);border:1px solid var(--tool-b);border-radius:6px;padding:3px 9px;font-size:.66rem;font-weight:500;color:var(--accent);cursor:pointer;transition:all .15s;}
.tool-chip:hover{opacity:.7;}
.tool-chip::before{content:'\\26A1';font-size:.6rem;}

/* ── Modal ── */
.modal-overlay{display:none;position:fixed;inset:0;background:var(--overlay);z-index:1000;align-items:center;justify-content:center;}
.modal-overlay.active{display:flex;}
.modal{background:var(--modal-bg);border:1px solid var(--border);border-radius:10px;width:680px;max-width:92vw;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 8px 30px rgba(0,0,0,.2);}
.modal-header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);}
.modal-title{font-size:.82rem;font-weight:700;color:var(--accent);}
.modal-close{background:none;border:none;font-size:1.1rem;color:var(--text3);cursor:pointer;padding:2px 6px;border-radius:4px;}
.modal-close:hover{background:var(--bg3);}
.modal-body{padding:14px 16px;overflow-y:auto;flex:1;}
.modal-section{margin-bottom:14px;}.modal-section:last-child{margin-bottom:0;}
.modal-label{font-size:.65rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--text4);margin-bottom:5px;}
.modal-code{font-family:'SF Mono',ui-monospace,monospace;font-size:.68rem;color:var(--text2);background:var(--code);border:1px solid var(--border);border-radius:6px;padding:10px 12px;white-space:pre-wrap;word-break:break-all;max-height:300px;overflow-y:auto;line-height:1.5;}

/* ── Oracle slide-out panel (left side of Agent Runs page) ── */
.oracle-tab{position:absolute;left:0;top:50%;transform:translateY(-50%);background:var(--bg2);border:1px solid var(--border);border-left:none;border-radius:0 8px 8px 0;padding:10px 6px;cursor:pointer;writing-mode:vertical-rl;text-orientation:mixed;font-size:.7rem;font-weight:600;color:var(--text3);z-index:50;transition:all .2s;transform:translateY(-50%) rotate(180deg);}
.oracle-tab:hover{background:var(--bg3);}
body.oracle-open .oracle-tab{display:none;}
.oracle-panel{position:absolute;left:0;top:0;bottom:0;width:360px;background:var(--bg2);border-right:1px solid var(--border);transform:translateX(-100%);transition:transform .25s ease;z-index:40;display:flex;flex-direction:column;box-shadow:4px 0 20px rgba(0,0,0,.06);}
body.dark .oracle-panel{box-shadow:4px 0 20px rgba(0,0,0,.3);}
body.oracle-open .oracle-panel{transform:translateX(0);}
.op-header{padding:12px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}
.op-title{font-size:.82rem;font-weight:700;}
.op-close{background:none;border:none;font-size:1rem;color:var(--text3);cursor:pointer;padding:2px 6px;border-radius:4px;}
.op-close:hover{background:var(--bg3);}
.op-list{flex:1;overflow-y:auto;padding:10px 14px;}
.op-event{padding:8px 12px;margin-bottom:6px;background:var(--bg3);border-radius:6px;font-size:.75rem;color:var(--text2);line-height:1.5;border-left:3px solid var(--accent);}

/* ── Rubric slide-out panel (on Agent Runs page) ── */
.rubric-tab{position:absolute;right:0;top:50%;transform:translateY(-50%);background:var(--bg2);border:1px solid var(--border);border-right:none;border-radius:8px 0 0 8px;padding:10px 6px;cursor:pointer;writing-mode:vertical-rl;text-orientation:mixed;font-size:.7rem;font-weight:600;color:var(--text3);z-index:50;transition:all .2s;}
.rubric-tab:hover{background:var(--bg3);}
body.rubric-open .rubric-tab{display:none;}
.rubric-panel{position:absolute;right:0;top:0;bottom:0;width:380px;background:var(--bg2);border-left:1px solid var(--border);transform:translateX(100%);transition:transform .25s ease;z-index:40;display:flex;flex-direction:column;box-shadow:-4px 0 20px rgba(0,0,0,.06);}
body.dark .rubric-panel{box-shadow:-4px 0 20px rgba(0,0,0,.3);}
body.rubric-open .rubric-panel{transform:translateX(0);}
.rp-header{padding:12px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}
.rp-title{font-size:.82rem;font-weight:700;}
.rp-score{font-size:.78rem;font-weight:700;padding:3px 10px;border-radius:12px;}
.rp-score-pos{color:var(--green);background:rgba(26,127,55,.08);}
.rp-score-neg{color:var(--red);background:rgba(207,34,46,.08);}
.rp-score-zero{color:var(--text4);background:var(--bg3);}
.rp-close{background:none;border:none;font-size:1rem;color:var(--text3);cursor:pointer;padding:2px 6px;border-radius:4px;}
.rp-close:hover{background:var(--bg3);}
.rp-list{flex:1;overflow-y:auto;padding:8px 0;}
.rp-item{padding:8px 14px;border-bottom:1px solid var(--border);cursor:pointer;display:flex;align-items:flex-start;gap:8px;transition:background .1s;}
.rp-item:hover{background:var(--bg3);}
.rp-icon{flex-shrink:0;font-size:.78rem;font-weight:700;margin-top:1px;width:14px;text-align:center;}
.rp-icon-pass{color:var(--green);}
.rp-icon-fail{color:var(--red);}
.rp-icon-na{color:var(--text4);}
.rp-content{flex:1;min-width:0;}
.rp-text{font-size:.72rem;color:var(--text2);line-height:1.45;}
.rp-meta-row{display:flex;gap:5px;margin-top:3px;align-items:center;}
.rp-weight{font-family:'SF Mono',ui-monospace,monospace;font-size:.6rem;font-weight:600;padding:0 4px;border-radius:3px;height:16px;line-height:16px;}
.rp-w-pos{color:var(--green);background:rgba(26,127,55,.08);}
.rp-w-neg{color:var(--red);background:rgba(207,34,46,.08);}
.rp-w-neut{color:var(--text4);background:var(--bg3);}
.rp-cat{font-size:.58rem;color:var(--text4);}
.rp-just{display:none;font-size:.66rem;color:var(--text3);background:var(--bg3);padding:6px 8px;border-radius:5px;margin-top:5px;line-height:1.45;}
.rp-item.expanded .rp-just{display:block;}
.rp-list::-webkit-scrollbar{width:4px;}
.rp-list::-webkit-scrollbar-track{background:transparent;}
.rp-list::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;}

/* ── Overview tab ── */
.overview-content{flex:1;overflow-y:auto;padding:30px;max-width:940px;margin:0 auto;width:100%;min-width:0;}
.section{margin-bottom:1.8rem;}
.section-title{font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--text4);margin-bottom:.5rem;}
.prompt-box{background:var(--bg2);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:6px;padding:12px 16px;font-size:.82rem;color:var(--text2);line-height:1.7;overflow-wrap:anywhere;word-break:break-word;overflow:hidden;}
.oracle-item{background:var(--bg2);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:6px;padding:8px 14px;margin-bottom:6px;font-size:.78rem;color:var(--text2);line-height:1.6;}
.env-row{display:flex;gap:8px;align-items:baseline;padding:3px 0;font-size:.74rem;border-bottom:1px solid var(--border);}.env-row:last-child{border-bottom:none;}
.env-key{color:var(--text3);font-weight:600;min-width:120px;font-size:.7rem;}
.env-row code{font-family:'SF Mono',ui-monospace,monospace;font-size:.68rem;color:var(--text2);background:var(--code);padding:1px 5px;border-radius:3px;border:1px solid var(--border);}

/* ── Stats bar ── */
.stats-bar{display:flex;gap:24px;margin-bottom:1.8rem;flex-wrap:wrap;}
.stat{display:flex;flex-direction:column;}
.stat-label{font-size:.62rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text4);font-weight:600;margin-bottom:2px;}
.stat-value{font-size:1.1rem;font-weight:700;font-family:'SF Mono',ui-monospace,monospace;}
.stat-green{color:var(--green);}.stat-red{color:var(--red);}.stat-amber{color:var(--amber);}

/* ── Runs summary table ── */
.runs-summary-tbl td code{font-size:.7rem;background:var(--code);padding:1px 5px;border-radius:3px;border:1px solid var(--border);}

/* ── Tool breakdown ── */
.tool-breakdown{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:8px 14px;max-width:500px;display:flex;flex-direction:column;gap:1px;}
.tb-row{display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid var(--border);font-size:.76rem;}
.tb-row:last-child{border-bottom:none;}
.tb-name{color:var(--accent);font-weight:500;font-family:'SF Mono',ui-monospace,monospace;font-size:.72rem;}
.tb-count{font-family:'SF Mono',ui-monospace,monospace;font-size:.72rem;font-weight:600;color:var(--text);background:var(--bg3);padding:1px 8px;border-radius:10px;min-width:28px;text-align:center;}

/* ── Rubric table ── */
table{width:100%;border-collapse:collapse;font-size:.8rem;background:var(--bg2);border:1px solid var(--border);border-radius:8px;overflow:hidden;}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #eaeef2;vertical-align:top;}
body.dark th,body.dark td{border-bottom-color:#21262d;}
th{background:var(--bg3);color:var(--text3);font-weight:600;font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;position:sticky;top:0;z-index:10;border-bottom:1px solid var(--border);}
td.label{text-align:center;color:var(--text4);font-weight:600;font-size:.72rem;width:40px;}
.model-header{width:80px;text-align:center;font-size:.65rem;}
.crit-text{display:block;margin-bottom:3px;font-size:.78rem;color:var(--text);}
.crit-meta{display:flex;gap:5px;align-items:center;margin-top:2px;}
.cat{display:inline-flex;align-items:center;font-size:.58rem;font-weight:500;color:var(--text3);background:var(--bg3);border-radius:9px;padding:0 7px;height:17px;line-height:17px;white-space:nowrap;}
body.dark .cat{color:var(--text4);background:rgba(110,118,129,0.1);}
td.pass{text-align:center;font-weight:600;font-size:.82rem;color:var(--green);width:80px;}
td.fail{text-align:center;font-weight:600;font-size:.82rem;color:var(--red);width:80px;}
td.na{text-align:center;font-weight:600;font-size:.82rem;color:var(--text4);width:80px;}
.crit-row{cursor:pointer;transition:background .1s;}
.crit-row:hover td{background:var(--bg3);}
body.dark .crit-row:hover td{background:rgba(136,152,170,0.06);}
.score-row td{border-top:2px solid var(--border);background:var(--bg3);font-size:.92rem;padding:10px 12px;}
.score-pos{text-align:center;color:var(--green);font-weight:700;font-size:.95rem;}
.score-mid{text-align:center;color:var(--amber);font-weight:700;font-size:.95rem;}
.score-neg{text-align:center;color:var(--red);font-weight:700;font-size:.95rem;}
.score-zero{text-align:center;color:var(--text4);font-weight:700;font-size:.95rem;}

/* Justification panel */
.justification-row td{padding:0!important;border-bottom:1px solid #eaeef2;background:var(--bg2);}
body.dark .justification-row td{border-bottom-color:#21262d;}
.j-cell{padding:0!important;}
.j-panel{background:var(--bg3);margin:0 12px 8px 52px;border-radius:6px;border:1px solid var(--border);padding:10px 14px;display:flex;flex-direction:column;gap:6px;}
body.dark .j-panel{background:#111519;border-color:#21262d;}
.j-entry{display:flex;align-items:flex-start;gap:8px;font-size:.72rem;line-height:1.5;}
.j-icon{flex-shrink:0;font-weight:700;font-size:.78rem;width:14px;text-align:center;margin-top:1px;}
.j-pass{color:var(--green);}.j-fail{color:var(--red);}
.j-model{flex-shrink:0;color:var(--text);font-weight:600;font-size:.7rem;min-width:80px;}
.j-text{color:var(--text3);}.j-text em{color:var(--text4);font-style:italic;}
.legend{display:flex;gap:1.5rem;margin-bottom:1.2rem;font-size:.75rem;color:var(--text3);}
.legend .g{color:var(--green);font-weight:600;}.legend .r{color:var(--red);font-weight:600;}

/* ── Markdown ── */
.md-content{font-size:.8rem;line-height:1.6;}
.md-content p{margin-bottom:.5em;}.md-content p:last-child{margin-bottom:0;}
.md-content h1,.md-content h2,.md-content h3,.md-content h4{font-weight:700;margin:.6em 0 .3em;line-height:1.3;}
.md-content h1{font-size:1.1em;}.md-content h2{font-size:1em;}.md-content h3{font-size:.92em;}.md-content h4{font-size:.86em;}
.md-content ul,.md-content ol{margin:.3em 0 .3em 1.2em;padding:0;}
.md-content li{margin-bottom:.2em;}
.md-content code{font-family:'SF Mono',ui-monospace,monospace;font-size:.85em;background:var(--code);padding:1px 4px;border-radius:3px;border:1px solid var(--border);}
.md-content pre{background:var(--code);border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin:.4em 0;overflow-x:auto;}
.md-content pre code{background:none;border:none;padding:0;font-size:.78em;line-height:1.45;}
.md-content blockquote{border-left:3px solid var(--border);padding-left:10px;margin:.4em 0;color:var(--text3);}
.md-content strong{font-weight:600;}.md-content a{color:var(--accent);text-decoration:none;}

/* ── Scrollbars ── */
::-webkit-scrollbar{width:5px;}::-webkit-scrollbar-track{background:transparent;}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px;}
.empty{text-align:center;color:var(--text4);padding:80px 20px;font-size:.85rem;}

/* ── Walkthrough Tour ── */
.wt-backdrop{position:fixed;top:0;left:0;width:100%;height:100%;z-index:9000;pointer-events:none;transition:opacity .25s;}
.wt-backdrop.active{pointer-events:auto;}
.wt-backdrop svg{position:absolute;top:0;left:0;width:100%;height:100%;}
.wt-spotlight{position:fixed;z-index:9001;border-radius:8px;box-shadow:0 0 0 4000px rgba(0,0,0,.55);pointer-events:none;transition:all .3s ease;}
.wt-tooltip{position:fixed;z-index:9002;background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px 20px;max-width:360px;box-shadow:0 8px 32px rgba(0,0,0,.25);font-size:.82rem;line-height:1.55;transition:all .3s ease;pointer-events:auto;}
.wt-tooltip-title{font-weight:700;font-size:.9rem;margin-bottom:6px;color:var(--text);}
.wt-tooltip-body{color:var(--text2);margin-bottom:14px;}
.wt-tooltip-footer{display:flex;align-items:center;justify-content:space-between;gap:8px;}
.wt-step-count{font-size:.68rem;color:var(--text4);font-weight:500;}
.wt-btn{border:1px solid var(--border);border-radius:6px;padding:5px 14px;font-size:.72rem;font-weight:600;cursor:pointer;font-family:inherit;transition:all .15s;background:var(--bg3);color:var(--text2);}
.wt-btn:hover{background:var(--border);}
.wt-btn-primary{background:var(--accent);color:#fff;border-color:var(--accent);}
.wt-btn-primary:hover{opacity:.85;}
.wt-btn-skip{background:transparent;border:none;color:var(--text4);font-size:.7rem;cursor:pointer;font-family:inherit;}
.wt-btn-skip:hover{color:var(--text2);}
.wt-arrow{position:absolute;width:12px;height:12px;background:var(--bg2);border:1px solid var(--border);transform:rotate(45deg);z-index:-1;}
.wt-arrow-top{top:-7px;left:24px;border-right:none;border-bottom:none;}
.wt-arrow-bottom{bottom:-7px;left:24px;border-left:none;border-top:none;}
.wt-arrow-left{left:-7px;top:16px;border-top:none;border-right:none;}
.wt-pulse{animation:wt-pulse-ring 1.8s ease-out infinite;}
@keyframes wt-pulse-ring{0%{box-shadow:0 0 0 4000px rgba(0,0,0,.55),0 0 0 0 rgba(9,105,218,.4)}70%{box-shadow:0 0 0 4000px rgba(0,0,0,.55),0 0 0 10px rgba(9,105,218,0)}100%{box-shadow:0 0 0 4000px rgba(0,0,0,.55),0 0 0 0 rgba(9,105,218,0)}}
"""


def generate_viewer(task_id, response_data, traj_messages_map, email="", annotator=""):
    """
    Generate HTML viewer for a single task.
    traj_messages_map: dict mapping run_index -> list of message dicts
    """

    turn = response_data.get("turns", [{}])[0]

    # Extract task info
    persona = "unknown"
    prompt = ""
    oracle_events = ""
    for sk, sv in turn.items():
        if not isinstance(sv, dict):
            continue
        if sv.get("type") == "TextCollection":
            out = sv.get("output", {})
            if "persona_selection" in out:
                persona = out["persona_selection"]
            if "oracle_events" in out:
                oracle_events = out["oracle_events"]
        elif sv.get("type") == "PromptInput":
            prompt = sv.get("output", {}).get("content", "")
            # Strip ```markdown ... ``` fences if present
            import re as _re
            prompt = _re.sub(r'^```(?:markdown)?\s*\n?', '', prompt.strip())
            prompt = _re.sub(r'\n?```\s*$', '', prompt.strip())

    # Criteria
    criteria_list = []
    for sk, sv in turn.items():
        if isinstance(sv, dict) and sv.get("type") == "RubricCriteriaBuilder":
            criteria_list = sv.get("output", {}).get("criteria", [])
            break
    n_criteria = len(criteria_list)

    # Find ExternalApp steps with runs
    agent_runs = []
    verifier_runs = []
    pass_at_1 = None
    ext_steps = []
    for sk, sv in turn.items():
        if not isinstance(sv, dict) or sv.get("type") != "ExternalApp":
            continue
        items = sv.get("output", {}).get("items", [])
        if not items or not isinstance(items[0], dict):
            continue
        meta = items[0].get("metadata", {})
        ar = meta.get("agentRuns", [])
        if ar:
            ext_steps.append((sk, meta, ar))

    # Prefer step with verifier runs
    for sk, meta, ar in ext_steps:
        vr = meta.get("verifierRuns", [])
        if vr:
            agent_runs = ar
            verifier_runs = vr
            pass_at_1 = meta.get("passAt1")
            break
    if not agent_runs and ext_steps:
        sk, meta, ar = ext_steps[0]
        agent_runs = ar
        verifier_runs = meta.get("verifierRuns", [])
        pass_at_1 = meta.get("passAt1")

    # ── Collect environment data from all ExternalApp steps ──
    # Known step-key → label mapping (order: Verifier, Agent Runs, Explorer)
    _ENV_LABELS = [
        ("step-1772474550989-jw4n99", "Verifier Env"),
        ("step-1772044998707-h7ql51", "Agent Runs Env"),
        ("external_app",              "Explorer Env"),
    ]
    env_sections = []  # list of (label, deploy_data_dict)
    for step_key, label in _ENV_LABELS:
        sv = turn.get(step_key)
        if not isinstance(sv, dict) or sv.get("type") != "ExternalApp":
            continue
        items = sv.get("output", {}).get("items", [])
        if items and isinstance(items[0], dict):
            dd = items[0].get("metadata", {}).get("deployData", {})
            if dd:
                env_sections.append((label, dd))

    n_runs = len(agent_runs)

    # Build agent→verifier index mapping.
    # Verifiers skip errored agent runs, so if there are fewer verifier runs
    # than agent runs, we match them to non-errored agents in order.
    non_errored_agent_indices = [i for i, ar in enumerate(agent_runs) if ar.get("status") not in ("errored",)]
    agent_to_verifier = {}  # agent_run_index → verifier_run_index
    for vi, ai in enumerate(non_errored_agent_indices):
        if vi < len(verifier_runs):
            agent_to_verifier[ai] = vi

    # Build verifier map keyed by AGENT run index (not verifier index)
    verifier_map = {}
    for ai, vi in agent_to_verifier.items():
        vr = verifier_runs[vi]
        vres = vr.get("verificationResults", {})
        for _vk, vv in vres.items():
            run_results = {}
            for r in vv.get("results", []):
                run_results[r["id"]] = {
                    "score": r.get("score", 0),
                    "justification": r.get("justification", ""),
                    "result": r.get("result", False),
                }
            verifier_map[ai] = run_results

    # ── Build JS data for chat messages per run ──
    # We embed all tool data and messages as JS objects
    all_run_data = []  # list of {messages, model, status, v_status, score, n_msg, has_traj}
    for ri, ar in enumerate(agent_runs):
        model = ar.get("agent_run_model", "unknown")
        status = ar.get("status", "?")
        vi = agent_to_verifier.get(ri)
        v_status = verifier_runs[vi].get("status", "?") if vi is not None else "n/a"
        vr_data = verifier_map.get(ri, {})
        n_rubric_pass = sum(1 for c in criteria_list if vr_data.get(c["id"], {}).get("score") == 1)
        n_rubric_fail = sum(1 for c in criteria_list if vr_data.get(c["id"], {}).get("score") == 0)
        n_rubric_rated = n_rubric_pass + n_rubric_fail

        msgs = traj_messages_map.get(ri, [])
        n_msg = len(msgs)
        n_tools = sum(len(m.get("tools", [])) for m in msgs)
        has_traj = ri in traj_messages_map  # whether we actually have trajectory data

        all_run_data.append({
            "messages": msgs,
            "model": model,
            "status": status,
            "v_status": v_status,
            "score": n_rubric_pass,
            "n_rubric_pass": n_rubric_pass,
            "n_rubric_fail": n_rubric_fail,
            "n_rubric_rated": n_rubric_rated,
            "n_msg": n_msg,
            "n_tools": n_tools,
            "has_traj": has_traj,
        })

    # ── Build rubric data per run for slide-out panel ──
    all_rubric_data = []  # one entry per run
    for ri in range(n_runs):
        vr_data = verifier_map.get(ri, {})
        items = []
        total_score = 0
        max_score = 0
        for crit in criteria_list:
            cid = crit["id"]
            ann = crit.get("annotations", {})
            weight = crit.get("weight", 1)
            rating = vr_data.get(cid, {})
            score = rating.get("score", None)
            passed = score == 1
            if score is not None:
                total_score += weight if passed else 0
                max_score += abs(weight)
            items.append({
                "text": crit.get("title", ""),
                "weight": weight,
                "cat": ann.get("rubric_category", ""),
                "passed": passed,
                "score": score,
                "justification": rating.get("justification", ""),
            })
        pct = round(total_score / max_score * 100) if max_score else 0
        all_rubric_data.append({
            "items": items,
            "score": total_score,
            "max": max_score,
            "pct": pct,
        })

    rubric_data_js = json.dumps(all_rubric_data, ensure_ascii=False)

    # Serialize to JS
    run_data_js = json.dumps(all_run_data, ensure_ascii=False)

    # ── Run selector buttons ──
    run_buttons_html = ""
    for ri, rd in enumerate(all_run_data):
        # Badge based on verifier rubric results, not agent status
        if rd["v_status"] in ("n/a", "?"):
            badge_color = "#9a6700"
            badge_text = "N/A"
        elif rd["v_status"] == "passed":
            badge_color = "#1a7f37"
            badge_text = "PASSED"
        else:
            badge_color = "#cf222e"
            badge_text = "FAILED"
        active = "active" if ri == 0 else ""
        # Rubric info: show pass/rated (and total if some unrated)
        if rd["n_rubric_rated"] > 0:
            n_unrated = n_criteria - rd["n_rubric_rated"]
            if n_unrated > 0:
                rubric_info = f'{rd["n_rubric_pass"]}/{rd["n_rubric_rated"]} rated ({n_unrated} N/A)'
            else:
                rubric_info = f'{rd["n_rubric_pass"]}/{n_criteria}'
        else:
            rubric_info = ""
        traj_info = f'{rd["n_msg"]} msgs &middot; {rd["n_tools"]} tools' if rd["has_traj"] else "no trajectory"
        rubric_span = f' <span style="font-size:.6rem;color:var(--text3)">{rubric_info} rubrics</span>' if rubric_info else ""
        run_buttons_html += (
            f'<button class="run-btn {active}" data-run-idx="{ri}">'
            f'Run #{ri+1} '
            f'<span class="badge" style="background:{badge_color}">{_esc(badge_text)}</span>'
            f'{rubric_span} '
            f'<span style="font-size:.6rem;color:var(--text4)">{traj_info}</span>'
            f'</button>'
        )

    # ── Rubric table ──
    run_th = "".join(f'<th class="model-header">Run #{ri+1}</th>' for ri in range(n_runs))

    rubric_rows = ""
    for ci, crit in enumerate(criteria_list):
        cid = crit["id"]
        ann = crit.get("annotations", {})
        cat = ann.get("rubric_category", "")

        cells = ""
        scores = []
        for ri in range(n_runs):
            vr_data = verifier_map.get(ri, {})
            rating = vr_data.get(cid, {})
            score = rating.get("score", None)
            scores.append(score)
            if score == 1:
                cells += '<td class="pass">&#10003;</td>'
            elif score == 0:
                cells += '<td class="fail">&#10007;</td>'
            else:
                cells += '<td class="na">&mdash;</td>'

        valid = [s for s in scores if s is not None]
        pr = sum(1 for s in valid if s == 1) / len(valid) * 100 if valid else 0
        pr_cls = "pass" if pr >= 80 else "na" if pr >= 40 else "fail"

        rubric_rows += f'''<tr class="crit-row" data-idx="{ci}" onclick="toggleJust({ci})">
  <td class="label">{ci+1}</td>
  <td class="title"><span class="crit-text">{_esc(crit["title"])}</span>
    <div class="crit-meta"><span class="cat">{_esc(cat)}</span></div>
  </td>
  {cells}
  <td class="{pr_cls}" style="font-size:.78rem">{pr:.0f}%</td>
</tr>
'''
        j_entries = ""
        for ri in range(n_runs):
            vr_data = verifier_map.get(ri, {})
            rating = vr_data.get(cid, {})
            sc = rating.get("score", None)
            just = rating.get("justification", "")
            if sc == 1:
                icon_cls, icon = "j-pass", "&#10003;"
            elif sc == 0:
                icon_cls, icon = "j-fail", "&#10007;"
            else:
                icon_cls, icon = "", "&mdash;"
            j_entries += f'<div class="j-entry"><span class="j-icon {icon_cls}">{icon}</span><span class="j-model">Run #{ri+1}</span><span class="j-text">{_esc(just) if just else "<em>No justification</em>"}</span></div>'

        rubric_rows += f'<tr class="justification-row" id="just-{ci}" style="display:none"><td colspan="{n_runs+3}" class="j-cell"><div class="j-panel">{j_entries}</div></td></tr>\n'

    # Total row
    total_cells = ""
    for ri in range(n_runs):
        vr_data = verifier_map.get(ri, {})
        total = sum(1 for c in criteria_list if vr_data.get(c["id"], {}).get("score") == 1)
        pct = total / n_criteria * 100 if n_criteria else 0
        s_cls = "score-pos" if pct >= 80 else "score-mid" if pct >= 40 else "score-neg" if pct > 0 else "score-zero"
        total_cells += f'<td class="{s_cls}">{total}/{n_criteria}</td>'

    all_scores = []
    for ri in range(n_runs):
        vr_data = verifier_map.get(ri, {})
        for c in criteria_list:
            s = vr_data.get(c["id"], {}).get("score")
            if s is not None:
                all_scores.append(s)
    overall_pr = sum(1 for s in all_scores if s == 1) / len(all_scores) * 100 if all_scores else 0
    opr_cls = "score-pos" if overall_pr >= 80 else "score-mid" if overall_pr >= 40 else "score-neg"

    # Overview
    oracle_items = ""
    oracle_panel_items = ""
    for line in oracle_events.split("\n"):
        line = line.strip()
        if line:
            oracle_items += f'<div class="oracle-item">{_esc(line)}</div>'
            oracle_panel_items += f'<div class="op-event">{_esc(line)}</div>'

    # Build environment HTML for each section
    def _render_env_row(key, val_str):
        if val_str.startswith("http"):
            return f'<div class="env-row"><span class="env-key">{_esc(key)}</span><a href="{_esc(val_str)}" target="_blank" style="color:var(--accent);font-size:.72rem;word-break:break-all">{_esc(val_str)}</a></div>'
        return f'<div class="env-row"><span class="env-key">{_esc(key)}</span><code>{_esc(val_str)}</code></div>'

    def _render_env_block(dd):
        rows = ""
        for k, v in dd.items():
            if isinstance(v, list):
                continue  # skip large arrays like 'runs'
            if isinstance(v, dict):
                # Flatten nested dicts as parent.child rows
                for ck, cv in v.items():
                    if isinstance(cv, (dict, list)):
                        continue
                    rows += _render_env_row(f"{k}.{ck}", _redact_s3(str(cv)))
                continue
            rows += _render_env_row(k, _redact_s3(str(v)))
        return rows

    # Collect verifier task IDs from verifier_runs for display in Verifier Env
    # Map back to agent run numbers using agent_to_verifier
    verifier_to_agent = {vi: ai for ai, vi in agent_to_verifier.items()}
    verifier_task_ids_html = ""
    if verifier_runs:
        vtid_rows = ""
        seen_vtids = {}  # dedupe: vtid -> list of agent run indices (1-based display)
        for vi, vr in enumerate(verifier_runs):
            vtid = vr.get("verifierTaskId", "")
            if vtid:
                agent_idx = verifier_to_agent.get(vi, vi)
                seen_vtids.setdefault(vtid, []).append(agent_idx)
        if seen_vtids:
            vtid_rows = '<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);">'
            vtid_rows += '<div style="font-weight:600;font-size:.72rem;color:var(--text3);margin-bottom:4px;">Verifier Run Task IDs</div>'
            for vtid, indices in seen_vtids.items():
                run_labels = ", ".join(f"#{i+1}" for i in indices)
                vtid_rows += (
                    f'<div class="env-row">'
                    f'<span class="env-key">Run {run_labels}</span>'
                    f'<code>{_esc(vtid)}</code>'
                    f'</div>'
                )
            vtid_rows += '</div>'
            verifier_task_ids_html = vtid_rows

    env_items = ""
    if env_sections:
        for label, dd in env_sections:
            rows = _render_env_block(dd)
            # Append verifier task IDs to the Verifier Env section
            extra = verifier_task_ids_html if label == "Verifier Env" else ""
            env_items += f'''<div class="env-group" style="margin-bottom:12px;">
              <div style="font-weight:600;font-size:.76rem;color:var(--accent);margin-bottom:4px;display:flex;align-items:center;gap:6px;">
                <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent);"></span>{_esc(label)}
              </div>
              <div style="background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:10px 14px;">{rows}{extra}</div>
            </div>'''

    # A run is "valid" only if the agent completed (not errored) AND the verifier actually ran
    has_verifier = len(verifier_runs) > 0
    valid_indices = [
        i for i, rd in enumerate(all_run_data)
        if rd["status"] not in ("errored", "?") and rd["v_status"] not in ("n/a", "?")
    ]

    n_valid = len(valid_indices)
    n_agent_pass = sum(1 for i in valid_indices if all_run_data[i]["status"] == "passed")
    n_verifier_pass = sum(1 for i in valid_indices if all_run_data[i]["v_status"] == "passed") if n_valid > 0 else None
    p1_str = f"{pass_at_1:.0%}" if pass_at_1 is not None else "N/A"

    valid_run_data = [all_run_data[i] for i in valid_indices]
    # For tool/msg stats, only count runs that have trajectory data (avoid 0-tool distortion)
    valid_with_traj = [rd for rd in valid_run_data if rd["has_traj"]]
    total_tools_all = sum(rd["n_tools"] for rd in valid_with_traj)
    total_msgs_all = sum(rd["n_msg"] for rd in valid_with_traj)
    max_msgs = max((rd["n_msg"] for rd in valid_with_traj), default=0)
    max_tools = max((rd["n_tools"] for rd in valid_with_traj), default=0)
    min_tools = min((rd["n_tools"] for rd in valid_with_traj), default=0)
    passed_run_indices = [i for i in valid_indices if all_run_data[i]["v_status"] == "passed"]
    passed_with_traj = [i for i in passed_run_indices if all_run_data[i]["has_traj"]]
    passed_runs_tools = [all_run_data[i]["n_tools"] for i in passed_with_traj]
    max_tools_pass = max(passed_runs_tools) if passed_runs_tools else "N/A"
    min_tools_pass = min(passed_runs_tools) if passed_runs_tools else "N/A"

    # Pick the run for tool breakdown: min-pass if any pass, else max-tools among valid
    # Only consider runs with trajectory data (so we actually have tool info)
    valid_with_traj_i = [i for i in valid_indices if all_run_data[i]["has_traj"]]
    if passed_with_traj:
        breakdown_ri = min(passed_with_traj, key=lambda i: all_run_data[i]["n_tools"])
        breakdown_label = f"Run #{breakdown_ri+1} (min tools among verifier-passed)"
    elif valid_with_traj_i:
        breakdown_ri = max(valid_with_traj_i, key=lambda i: all_run_data[i]["n_tools"])
        breakdown_label = f"Run #{breakdown_ri+1} (max tools overall — no verifier pass)"
    else:
        breakdown_ri = 0
        breakdown_label = "Run #1 (fallback)"

    # Count tool calls by name for that run
    from collections import Counter
    tool_counter = Counter()
    br_msgs = all_run_data[breakdown_ri]["messages"]
    for m in br_msgs:
        for t in m.get("tools", []):
            tool_counter[t.get("name", "unknown")] += 1
    # Sort by frequency descending
    tool_breakdown_items = tool_counter.most_common()
    tool_breakdown_html = ""
    if tool_breakdown_items:
        tb_rows = ""
        for tname, tcount in tool_breakdown_items:
            tb_rows += f'<div class="tb-row"><span class="tb-name">{_esc(tname)}</span><span class="tb-count">{tcount}</span></div>'
        tool_breakdown_html = f"""
    <div class="section">
      <div class="section-title">Tool Usage &mdash; {_esc(breakdown_label)} ({all_run_data[breakdown_ri]["n_tools"]} calls)</div>
      <div class="tool-breakdown">{tb_rows}</div>
    </div>"""

    # ── Runs summary table for overview ──
    runs_summary_rows = ""
    for ri, rd in enumerate(all_run_data):
        is_excluded = ri not in valid_indices
        a_cls = "pass" if rd["status"] == "passed" else "fail" if rd["status"] in ("failed", "errored") else "na"
        verifier_ran = rd["v_status"] not in ("n/a", "?")
        v_cls = "pass" if rd["v_status"] == "passed" else "fail" if rd["v_status"] in ("failed", "errored") else "na"
        v_display = _esc(rd["v_status"]) if verifier_ran else "N/A"
        row_style = ' style="opacity:.45;font-style:italic"' if is_excluded else ""
        excl_badge = ' <span style="font-size:.6rem;color:var(--text4);font-weight:400" title="Excluded from stats: agent errored or verifier did not run">(excluded)</span>' if is_excluded else ""
        msgs_display = str(rd["n_msg"]) if rd["has_traj"] else "&mdash;"
        tools_display = str(rd["n_tools"]) if rd["has_traj"] else "&mdash;"
        # Rubric pass/fail display — N/A if verifier didn't run
        if not verifier_ran:
            rubric_display = '<span class="na">N/A</span>'
        elif rd["n_rubric_rated"] > 0:
            n_unrated = n_criteria - rd["n_rubric_rated"]
            unrated_span = f' <span style="color:var(--text4);font-size:.7rem">({n_unrated} N/A)</span>' if n_unrated > 0 else ""
            rubric_display = (
                f'<span style="color:var(--green);font-weight:600">{rd["n_rubric_pass"]}&#10003;</span>'
                f' / '
                f'<span style="color:var(--red);font-weight:600">{rd["n_rubric_fail"]}&#10007;</span>'
                f' <span style="color:var(--text4)">of {rd["n_rubric_rated"]}</span>'
                f'{unrated_span}'
            )
        else:
            rubric_display = "&mdash;"
        runs_summary_rows += (
            f'<tr{row_style}>'
            f'<td style="font-weight:600">#{ri+1}{excl_badge}</td>'
            f'<td><code style="font-size:.72rem">{_esc(rd["model"])}</code></td>'
            f'<td class="{a_cls}">{_esc(rd["status"])}</td>'
            f'<td class="{v_cls}">{v_display}</td>'
            f'<td>{msgs_display}</td>'
            f'<td>{tools_display}</td>'
            f'<td>{rubric_display}</td>'
            f'</tr>'
        )

    runs_summary_html = f"""
    <div class="section">
      <div class="section-title">Runs Summary</div>
      <table class="runs-summary-tbl">
        <thead><tr>
          <th>Run</th><th>Model</th><th>Agent</th><th>Verifier</th><th>Msgs</th><th>Tools</th><th>Rubrics</th>
        </tr></thead>
        <tbody>{runs_summary_rows}</tbody>
      </table>
    </div>"""

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MCP Advanced — {_esc(task_id)}</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>{CSS}</style>
</head>
<body>

<!-- Top bar -->
<div class="topbar">
  <h1><a href="index.html" style="color:inherit;text-decoration:none;">MCP Advanced</a></h1>
  <div class="top-tabs">
    <button class="top-tab active" onclick="showPage('overview',this)">Overview</button>
    <button class="top-tab" onclick="showPage('runs',this)">Agent Runs ({n_runs}) &middot; max {max_msgs} msgs &middot; {max_tools} tools</button>
    <button class="top-tab" onclick="showPage('rubric',this)">Rubric Scorecard ({n_criteria})</button>
  </div>
  <span class="info" id="info"><a href="https://dashboard.scale.com/corp/genai-ops-hub/search/{_esc(task_id)}" target="_blank" style="color:var(--accent)">{_esc(task_id)}</a> &middot; {_esc(persona)}{f' &middot; {_esc(annotator)}' if annotator else ''}{f' &middot; <a href="https://dashboard.scale.com/corp/genai-ops-hub/search/{_esc(email)}" target="_blank" style="color:var(--accent)">{_esc(email)}</a>' if email else ''}</span>
  <button class="btn" id="theme-btn" onclick="toggleTheme()">&#9679; Dark</button>
</div>

<!-- ═══ Overview page ═══ -->
<div class="page active" id="page-overview">
  <div class="overview-content">
    <div class="stats-bar" id="wt-stats-bar">
      <div class="stat"><span class="stat-label">Agent Runs</span><span class="stat-value {"stat-green" if n_valid > 0 and n_agent_pass > n_valid//2 else "stat-amber" if n_valid == 0 else "stat-red"}">{"N/A" if n_valid == 0 else f"{n_agent_pass}/{n_valid}"}</span></div>
      <div class="stat"><span class="stat-label">Verifier Pass</span><span class="stat-value {"stat-green" if n_verifier_pass and n_verifier_pass > n_valid//2 else "stat-amber" if n_verifier_pass is None else "stat-red"}">{"N/A" if n_verifier_pass is None else f"{n_verifier_pass}/{n_valid}"}</span></div>
      <div class="stat"><span class="stat-label">Pass@1</span><span class="stat-value {"stat-green" if pass_at_1 and pass_at_1 >= 0.5 else "stat-amber" if pass_at_1 and pass_at_1 > 0 else "stat-red"}">{p1_str}</span></div>
      <div class="stat"><span class="stat-label">Criteria</span><span class="stat-value">{n_criteria}</span></div>
      <div class="stat"><span class="stat-label">Max Tools</span><span class="stat-value {"stat-amber" if n_valid == 0 else ""}">{"N/A" if n_valid == 0 else max_tools}</span></div>
      <div class="stat"><span class="stat-label">Min Tools</span><span class="stat-value {"stat-amber" if n_valid == 0 else ""}">{"N/A" if n_valid == 0 else min_tools}</span></div>
      <div class="stat"><span class="stat-label">Max Tools (Pass)</span><span class="stat-value stat-green">{max_tools_pass}</span></div>
      <div class="stat"><span class="stat-label">Min Tools (Pass)</span><span class="stat-value stat-green">{min_tools_pass}</span></div>
    </div>
    <div id="wt-runs-summary">{runs_summary_html}</div>
    <div id="wt-tool-breakdown">{tool_breakdown_html}</div>
    <div class="section" id="wt-prompt">
      <div class="section-title">Prompt</div>
      <div style="display:flex;gap:8px;max-width:800px;">
        <div class="avatar avatar-user">U</div>
        <div><div class="bubble bubble-user" style="border-bottom-left-radius:14px;"><div class="md-content" id="prompt-md"></div></div></div>
      </div>
    </div>
    <div class="section" id="wt-oracle"><div class="section-title">Oracle Events</div>{oracle_items if oracle_items else '<span style="color:var(--text4);font-size:.78rem">No oracle events</span>'}</div>
    <div class="section" id="wt-env"><div class="section-title">Environments</div>
      <div style="max-width:700px">
        {env_items if env_items else '<span style="color:var(--text4);font-size:.78rem">No environment data</span>'}
      </div>
    </div>
  </div>
</div>

<!-- ═══ Agent Runs page (chat UI) ═══ -->
<div class="page" id="page-runs" style="flex-direction:column;">
  <div class="run-selector" id="run-selector">{run_buttons_html}</div>
  <div class="chat-area">
    <div class="oracle-tab" id="oracle-tab">&#128269; Oracle Events</div>
    <div class="oracle-panel" id="oracle-panel">
      <div class="op-header">
        <span class="op-title">Oracle Events</span>
        <button class="op-close" id="op-close">&#10005;</button>
      </div>
      <div class="op-list">{oracle_panel_items if oracle_panel_items else '<span style="color:var(--text4);font-size:.78rem;padding:8px">No oracle events</span>'}</div>
    </div>
    <div class="chat-container" id="chat-container">
      <div class="chat-inner" id="chat"></div>
    </div>
    <div class="rubric-tab" id="rubric-tab">&#128203; Rubrics</div>
    <div class="rubric-panel" id="rubric-panel">
      <div class="rp-header">
        <span class="rp-title">Rubric Results</span>
        <span class="rp-score" id="rp-score"></span>
        <button class="rp-close" id="rp-close">&#10005;</button>
      </div>
      <div class="rp-list" id="rp-list"></div>
    </div>
  </div>
</div>

<!-- ═══ Rubric page ═══ -->
<div class="page" id="page-rubric">
  <div class="overview-content">
    <div class="legend">
      <span><span class="g">&#10003;</span> Pass</span>
      <span><span class="r">&#10007;</span> Fail</span>
      <span>Click any row to expand justifications</span>
    </div>
    <table>
      <thead><tr>
        <th style="width:40px;text-align:center">#</th>
        <th>Criterion</th>
        {run_th}
        <th class="model-header">Pass&nbsp;Rate</th>
      </tr></thead>
      <tbody>
        {rubric_rows}
        <tr class="score-row">
          <td></td><td style="font-weight:700;font-size:.85rem">Total</td>
          {total_cells}
          <td class="{opr_cls}">{overall_pr:.0f}%</td>
        </tr>
      </tbody>
    </table>
  </div>
</div>

<!-- Tool modal -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <div class="modal-header">
      <span class="modal-title" id="modal-title"></span>
      <button class="modal-close" id="modal-close">&#10005;</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
var RUN_DATA={run_data_js};
var RUBRIC_DATA={rubric_data_js};

var toolData=[];
var currentRun=0;

function esc(s){{return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}

function showPage(name,btn){{
  document.querySelectorAll('.page').forEach(function(el){{el.classList.remove('active');}});
  document.querySelectorAll('.top-tab').forEach(function(el){{el.classList.remove('active');}});
  document.getElementById('page-'+name).classList.add('active');
  btn.classList.add('active');
  if(name==='runs'){{renderChat(currentRun);renderRunRubrics(currentRun);}}
}}

function renderRunRubrics(ri){{
  var list=document.getElementById('rp-list');
  var scoreEl=document.getElementById('rp-score');
  var r=RUBRIC_DATA[ri];
  if(!r){{list.innerHTML='<div style="padding:40px;text-align:center;color:var(--text4);font-size:.8rem">No rubric data.</div>';scoreEl.textContent='';return;}}
  var cls=r.pct>=80?'rp-score-pos':r.pct>0?'rp-score-neg':'rp-score-zero';
  scoreEl.className='rp-score '+cls;
  scoreEl.textContent=r.score+'/'+r.max+' ('+r.pct+'%)';
  var h='';
  for(var i=0;i<r.items.length;i++){{
    var it=r.items[i];
    var iconCls,icon;
    if(it.score===1){{iconCls='rp-icon-pass';icon='\\u2713';}}
    else if(it.score===0){{iconCls='rp-icon-fail';icon='\\u2717';}}
    else{{iconCls='rp-icon-na';icon='\\u2014';}}
    var w=it.weight;
    var wCls=w>0?'rp-w-pos':w<0?'rp-w-neg':'rp-w-neut';
    var wStr=w>0?'+'+w:String(w);
    h+='<div class="rp-item" data-action="expand">';
    h+='<span class="rp-icon '+iconCls+'">'+icon+'</span>';
    h+='<div class="rp-content">';
    h+='<div class="rp-text">'+esc(it.text)+'</div>';
    h+='<div class="rp-meta-row"><span class="rp-weight '+wCls+'">'+wStr+'</span><span class="rp-cat">'+esc(it.cat)+'</span></div>';
    if(it.justification)h+='<div class="rp-just">'+esc(it.justification)+'</div>';
    h+='</div></div>';
  }}
  list.innerHTML=h;
  list.scrollTop=0;
}}

function renderChat(ri){{
  currentRun=ri;
  toolData=[];
  renderRunRubrics(ri);
  var chat=document.getElementById('chat');
  var rd=RUN_DATA[ri];
  if(!rd||!rd.messages||!rd.messages.length){{
    var reason=rd&&rd.status==='errored'?'Agent errored — no trajectory available.':'No trajectory data for this run.';
    chat.innerHTML='<div class="empty">'+reason+'</div>';return;
  }}

  // Update run selector active state
  document.querySelectorAll('.run-btn').forEach(function(b){{b.classList.remove('active');}});
  var sel=document.querySelector('[data-run-idx="'+ri+'"]');
  if(sel)sel.classList.add('active');

  var msgs=rd.messages;
  var h='';
  for(var mi=0;mi<msgs.length;mi++){{
    var msg=msgs[mi];
    var isUser=msg.role==='user';
    var cls=isUser?'msg-user':'msg-assistant';
    var aCls=isUser?'avatar-user':'avatar-assistant';
    var bCls=isUser?'bubble-user':'bubble-assistant';
    var aL=isUser?'U':'A';
    var content='';

    // Thinking
    if(msg.thinking){{
      var thId='th-'+ri+'-'+mi;
      content+='<div class="thinking" data-think-id="'+thId+'">Thinking\u2026</div>';
      content+='<div class="thinking-text" id="'+thId+'">'+esc(msg.thinking)+'</div>';
    }}

    // Text
    if(msg.text){{
      if(isUser){{
        var userText=msg.text;
        if(userText.length>2000)userText=userText.substring(0,2000)+'\\n\\n... (truncated)';
        content+='<div>'+esc(userText).replace(/\\n/g,'<br>')+'</div>';
      }}else{{
        content+='<div class="md-content">'+marked.parse(msg.text)+'</div>';
      }}
    }}

    // Tool chips (skip on last assistant message — those are final output tools)
    var isLastAssistant=(!isUser && mi===msgs.length-1);
    if(msg.tools&&msg.tools.length>0&&!isLastAssistant){{
      content+='<div class="tool-group">';
      for(var ti=0;ti<msg.tools.length;ti++){{
        var idx=toolData.length;
        toolData.push(msg.tools[ti]);
        content+='<span class="tool-chip" data-modal-idx="'+idx+'">'+esc(msg.tools[ti].name)+'</span>';
      }}
      content+='</div>';
    }}

    h+='<div class="msg '+cls+'">';
    h+='<div class="avatar '+aCls+'">'+aL+'</div>';
    h+='<div><div class="bubble '+bCls+'">'+content+'</div>';
    if(msg.ts)h+='<div class="msg-ts">'+msg.ts+'</div>';
    h+='</div></div>';
  }}
  chat.innerHTML=h;
  document.getElementById('chat-container').scrollTop=0;
}}

function showModal(idx){{
  var t=toolData[idx];if(!t)return;
  document.getElementById('modal-title').textContent=t.name;
  var h='';
  h+='<div class="modal-section"><div class="modal-label">Parameters</div><div class="modal-code">'+esc(typeof t.args==='object'?JSON.stringify(t.args,null,2):(t.args||'{{}}'))+'</div></div>';
  var result=t.result||'(no output)';
  if(typeof result==='object')result=JSON.stringify(result,null,2);
  h+='<div class="modal-section"><div class="modal-label">Output</div><div class="modal-code">'+esc(result)+'</div></div>';
  document.getElementById('modal-body').innerHTML=h;
  document.getElementById('modal-overlay').classList.add('active');
}}

// Event delegation
document.addEventListener('click',function(e){{
  var think=e.target.closest('[data-think-id]');
  if(think){{var t=document.getElementById(think.getAttribute('data-think-id'));if(t)t.style.display=t.style.display==='none'?'block':'none';return;}}
  var chip=e.target.closest('[data-modal-idx]');
  if(chip){{e.stopPropagation();showModal(parseInt(chip.getAttribute('data-modal-idx')));return;}}
  var rpItem=e.target.closest('.rp-item[data-action="expand"]');
  if(rpItem){{rpItem.classList.toggle('expanded');return;}}
  var runBtn=e.target.closest('[data-run-idx]');
  if(runBtn){{renderChat(parseInt(runBtn.getAttribute('data-run-idx')));return;}}
}});

document.getElementById('modal-close').addEventListener('click',function(){{document.getElementById('modal-overlay').classList.remove('active');}});
document.getElementById('modal-overlay').addEventListener('click',function(e){{if(e.target===e.currentTarget)e.currentTarget.classList.remove('active');}});
document.addEventListener('keydown',function(e){{if(e.key==='Escape')document.getElementById('modal-overlay').classList.remove('active');}});

// Rubric panel toggle
document.getElementById('rubric-tab').addEventListener('click',function(){{document.body.classList.add('rubric-open');}});
document.getElementById('rp-close').addEventListener('click',function(){{document.body.classList.remove('rubric-open');}});

// Oracle panel toggle
document.getElementById('oracle-tab').addEventListener('click',function(){{document.body.classList.add('oracle-open');}});
document.getElementById('op-close').addEventListener('click',function(){{document.body.classList.remove('oracle-open');}});

function toggleJust(idx){{
  var row=document.getElementById('just-'+idx);
  row.style.display=row.style.display==='none'?'table-row':'none';
}}

function toggleTheme(){{
  document.body.classList.toggle('dark');
  var btn=document.getElementById('theme-btn');
  btn.innerHTML=document.body.classList.contains('dark')?'&#9679; Light':'&#9679; Dark';
}}

// Render prompt markdown on load
var PROMPT_TEXT=`{_js_safe(prompt)}`;
(function(){{var el=document.getElementById('prompt-md');if(el&&PROMPT_TEXT)el.innerHTML=marked.parse(PROMPT_TEXT);}})();

// ═══ Walkthrough Tour ═══
(function(){{
  var overviewSteps=[
    {{el:'#wt-stats-bar',title:'Metrics at a Glance',body:'Key stats for this task: agent/verifier pass rates, criteria count, and tool usage across runs.',pos:'bottom'}},
    {{el:'#wt-runs-summary',title:'Runs Summary',body:'A table showing each agent run with model, pass/fail status, message count, tool count, and overall score. Quickly compare run performance.',pos:'bottom'}},
    {{el:'#wt-tool-breakdown',title:'Tool Usage Breakdown',body:'Shows which tools were called and how often in the best verifier-passing run (or max-tools run if none passed). Helps spot patterns in tool usage.',pos:'bottom'}},
    {{el:'#wt-prompt',title:'Task Prompt',body:'The original prompt given to the agent. This is the instruction the agent attempts to fulfill across all runs.',pos:'top'}},
    {{el:'#wt-oracle',title:'Oracle Events',body:'Expected actions or milestones the agent should hit. Used to verify the agent completed the right steps.',pos:'top'}},
    {{el:'#wt-env',title:'Environments',body:'Three environment configurations: Verifier Env (used for verifier runs), Agent Runs Env (used for agent runs), and Explorer Env (the base/snapshotted environment).',pos:'top'}}
  ];
  var runsSteps=[
    {{el:'#run-selector',title:'Run Selector',body:'Switch between agent runs. Each button shows the run number with message and tool counts. The active run is highlighted.',pos:'bottom'}},
    {{el:'#chat-container',title:'Chat Conversation',body:'The full agent trajectory rendered as a chat. User messages appear on the left, agent responses on the right with rendered markdown.',pos:'left'}},
    {{el:'#oracle-tab',title:'Oracle Events Panel',body:'Click this tab to slide open the oracle events — the expected actions or milestones the agent should complete. Use these to verify agent behaviour while reviewing the chat.',pos:'right'}},
    {{el:'#rubric-tab',title:'Rubric Panel',body:'Click this tab to slide open a rubric panel showing pass/fail results and justifications for this specific run.',pos:'left'}}
  ];

  var wtBackdrop,wtSpotlight,wtTooltip,wtSteps,wtIdx,wtPage;

  function wtInit(){{
    wtBackdrop=document.createElement('div');wtBackdrop.className='wt-backdrop';
    wtSpotlight=document.createElement('div');wtSpotlight.className='wt-spotlight wt-pulse';
    wtTooltip=document.createElement('div');wtTooltip.className='wt-tooltip';
    document.body.appendChild(wtBackdrop);document.body.appendChild(wtSpotlight);document.body.appendChild(wtTooltip);
  }}

  function wtStart(steps,page){{
    if(!wtBackdrop)wtInit();
    wtSteps=steps;wtIdx=0;wtPage=page;
    wtBackdrop.classList.add('active');
    wtShow();
  }}

  function wtShow(){{
    if(wtIdx>=wtSteps.length){{wtEnd();return;}}
    var step=wtSteps[wtIdx];
    var el=document.querySelector(step.el);
    if(!el){{wtIdx++;wtShow();return;}}
    el.scrollIntoView({{behavior:'smooth',block:'center'}});
    setTimeout(function(){{
      var r=el.getBoundingClientRect();
      var pad=6;
      wtSpotlight.style.display='block';
      wtSpotlight.style.top=(r.top-pad)+'px';
      wtSpotlight.style.left=(r.left-pad)+'px';
      wtSpotlight.style.width=(r.width+pad*2)+'px';
      wtSpotlight.style.height=(r.height+pad*2)+'px';

      var n=wtIdx+1;var total=wtSteps.length;
      var h='<div class="wt-tooltip-title">'+step.title+'</div>';
      h+='<div class="wt-tooltip-body">'+step.body+'</div>';
      h+='<div class="wt-tooltip-footer">';
      h+='<span class="wt-step-count">'+n+' of '+total+'</span>';
      h+='<div style="display:flex;gap:6px;align-items:center">';
      h+='<button class="wt-btn-skip" onclick="window._wtEnd()">Skip</button>';
      if(wtIdx>0)h+='<button class="wt-btn" onclick="window._wtPrev()">Back</button>';
      h+='<button class="wt-btn wt-btn-primary" onclick="window._wtNext()">'+(n===total?'Done':'Next')+'</button>';
      h+='</div></div>';

      var arrowCls='wt-arrow-top';
      wtTooltip.innerHTML=h;
      wtTooltip.style.display='block';

      // Position tooltip
      var tw=360;
      var tH=wtTooltip.offsetHeight;
      if(step.pos==='bottom'){{
        wtTooltip.style.top=(r.bottom+14)+'px';
        wtTooltip.style.left=Math.max(8,Math.min(r.left,window.innerWidth-tw-16))+'px';
        arrowCls='wt-arrow-top';
      }}else if(step.pos==='top'){{
        wtTooltip.style.top=(r.top-tH-14)+'px';
        wtTooltip.style.left=Math.max(8,Math.min(r.left,window.innerWidth-tw-16))+'px';
        arrowCls='wt-arrow-bottom';
      }}else if(step.pos==='left'){{
        wtTooltip.style.top=Math.max(8,r.top)+'px';
        wtTooltip.style.left=Math.max(8,r.left-tw-14)+'px';
        arrowCls='';
      }}
      // Remove old arrow, add new
      var oldA=wtTooltip.querySelector('.wt-arrow');if(oldA)oldA.remove();
      if(arrowCls){{var arEl=document.createElement('div');arEl.className='wt-arrow '+arrowCls;wtTooltip.appendChild(arEl);}}
    }},350);
  }}

  function wtNext(){{wtIdx++;wtShow();}}
  function wtPrev(){{if(wtIdx>0)wtIdx--;wtShow();}}
  function wtEnd(){{
    wtBackdrop.classList.remove('active');
    wtSpotlight.style.display='none';
    wtTooltip.style.display='none';
    // Remember dismissal per page
    try{{sessionStorage.setItem('wt_done_'+wtPage,'1');}}catch(e){{}}
  }}

  window._wtNext=wtNext;window._wtPrev=wtPrev;window._wtEnd=wtEnd;

  // Launch on page show
  var origShowPage=window.showPage||function(){{}};
  function showPageHook(name,btn){{
    // Call original
    document.querySelectorAll('.page').forEach(function(el){{el.classList.remove('active');}});
    document.querySelectorAll('.top-tab').forEach(function(el){{el.classList.remove('active');}});
    document.getElementById('page-'+name).classList.add('active');
    btn.classList.add('active');
    if(name==='runs'){{renderChat(currentRun);renderRunRubrics(currentRun);}}
    // Trigger walkthrough
    setTimeout(function(){{
      try{{if(sessionStorage.getItem('wt_done_'+name))return;}}catch(e){{}}
      if(name==='overview')wtStart(overviewSteps,'overview');
      else if(name==='runs')wtStart(runsSteps,'runs');
    }},500);
  }}
  // Replace global showPage
  window.showPage=showPageHook;

  // Auto-launch overview walkthrough on first load
  setTimeout(function(){{
    try{{if(sessionStorage.getItem('wt_done_overview'))return;}}catch(e){{}}
    wtStart(overviewSteps,'overview');
  }},800);

  // Allow re-launching via keyboard (press ?)
  document.addEventListener('keydown',function(e){{
    if(e.key==='?'&&!e.ctrlKey&&!e.metaKey){{
      var activePage=document.querySelector('.page.active');
      if(!activePage)return;
      var id=activePage.id.replace('page-','');
      // Clear stored dismissal
      try{{sessionStorage.removeItem('wt_done_'+id);}}catch(ex){{}}
      if(id==='overview')wtStart(overviewSteps,'overview');
      else if(id==='runs')wtStart(runsSteps,'runs');
    }}
  }});
}})();
</script>
</body>
</html>"""

    # Tool freq from the "featured" run (the one whose count appears as Max Tools in overview)
    # If pass runs exist → max tools among passed; else → max tools among valid
    # Only consider runs with trajectory data for featured run selection
    from collections import Counter as _Counter
    if passed_with_traj:
        featured_ri = max(passed_with_traj, key=lambda i: all_run_data[i]["n_tools"])
    elif valid_with_traj_i:
        featured_ri = max(valid_with_traj_i, key=lambda i: all_run_data[i]["n_tools"])
    else:
        featured_ri = 0
    featured_tool_counter = _Counter()
    if all_run_data:
        for m in all_run_data[featured_ri].get("messages", []):
            for t in m.get("tools", []):
                featured_tool_counter[t.get("name", "unknown")] += 1

    stats = {
        "task_id": task_id,
        "persona": persona,
        "email": email,
        "annotator": annotator,
        "n_runs": n_valid,  # only count valid runs
        "n_total_runs": n_runs,  # total including errored/no-verifier
        "n_agent_pass": n_agent_pass,
        "has_verifier": has_verifier,
        "n_verifier_pass": n_verifier_pass if n_verifier_pass is not None else 0,
        "verifier_display": f"{n_verifier_pass}/{n_valid}" if n_verifier_pass is not None else "N/A",
        "agent_display": f"{n_agent_pass}/{n_valid}" if n_valid > 0 else "N/A",
        "pass_at_1": p1_str,
        "n_criteria": n_criteria,
        "max_tools": max_tools,
        "min_tools": min_tools,
        "max_tools_pass": max_tools_pass,
        "min_tools_pass": min_tools_pass,
        "run_tools_list": [all_run_data[i]["n_tools"] for i in valid_indices],
        "tool_freq": dict(featured_tool_counter.most_common()),
    }

    return html_out, stats


def load_trajectories_for_task(task_id, attempt_id, response_data):
    """Load trajectory files and parse into messages for each run.

    Looks for trajectory filenames in three locations (first match wins per run):
      1. deployData.runs[ri].trajectoryS3Uri
      2. metadata.agentRuns[ri].trajectoryS3Uri
      3. metadata.agentRuns[ri].taskStepContext.prompt_responses[].agent_trajectory_s3_uri
    """
    traj_dir = os.path.join(TRAJ_DIR, task_id, attempt_id)
    if not os.path.isdir(traj_dir):
        return {}

    turn = response_data.get("turns", [{}])[0]

    run_file_map = {}  # run_index -> filename
    for sk, sv in turn.items():
        if not isinstance(sv, dict) or sv.get("type") != "ExternalApp":
            continue
        items = sv.get("output", {}).get("items", [])
        if not items or not isinstance(items[0], dict):
            continue
        meta = items[0].get("metadata", {})
        dd = meta.get("deployData", {})

        # Source 1: deployData.runs[].trajectoryS3Uri
        for ri, r in enumerate(dd.get("runs", [])):
            url = r.get("trajectoryS3Uri", "")
            if url and ri not in run_file_map:
                fname = url.split("?")[0].split("/")[-1]
                run_file_map[ri] = fname

        # Source 2 & 3: metadata.agentRuns
        for ri, ar in enumerate(meta.get("agentRuns", [])):
            # Source 2: direct trajectoryS3Uri on agentRun
            url = ar.get("trajectoryS3Uri", "")
            if url and ri not in run_file_map:
                fname = url.split("?")[0].split("/")[-1]
                run_file_map[ri] = fname
            # Source 3: taskStepContext.prompt_responses[].agent_trajectory_s3_uri
            prs = ar.get("taskStepContext", {}).get("prompt_responses", [])
            for pr in prs:
                s3_uri = pr.get("agent_trajectory_s3_uri", "")
                if s3_uri and ri not in run_file_map:
                    fname = s3_uri.split("?")[0].split("/")[-1]
                    run_file_map[ri] = fname

    result = {}
    for ri, fname in sorted(run_file_map.items()):
        fpath = os.path.join(traj_dir, fname)
        if os.path.isfile(fpath):
            try:
                with open(fpath) as f:
                    spans = json.load(f)
                messages = parse_trajectory_to_messages(spans)
                result[ri] = messages
            except Exception as e:
                print(f"    ⚠ Failed to parse {fname}: {e}")

    return result


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    print(f"CSV:          {CSV_PATH}")
    print(f"Trajectories: {TRAJ_DIR}")
    print(f"Output:       {OUT_DIR}\n")

    with open(CSV_PATH) as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    print(f"Processing {len(rows)} tasks from CSV\n")

    generated = []
    # Detect column layout
    hdr_lower = [h.lower().strip() for h in header]
    col_taskid = hdr_lower.index("taskid")
    col_attemptid = hdr_lower.index("attemptid")
    col_review_level = hdr_lower.index("review_level")
    col_response = hdr_lower.index("response")
    col_traj = hdr_lower.index("trajectory_urls") if "trajectory_urls" in hdr_lower else None
    col_email = hdr_lower.index("email") if "email" in hdr_lower else None
    col_annotator = hdr_lower.index("annotator") if "annotator" in hdr_lower else None

    for row in rows:
        task_id = row[col_taskid]
        attempt_id = row[col_attemptid]
        raw_response = row[col_response].strip()
        review_level = row[col_review_level].strip() if col_review_level is not None else None
        if not raw_response:
            print(f"Task: {task_id}  Attempt: {attempt_id}")
            print(f"  ⚠ Skipping — empty response column\n")
            continue
        try:
            response_data = json.loads(raw_response)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"Task: {task_id}  Attempt: {attempt_id}")
            print(f"  ⚠ Skipping — invalid JSON in response: {e}\n")
            continue
        email = row[col_email].strip() if col_email is not None else ""
        annotator = row[col_annotator].strip() if col_annotator is not None else ""

        # Merge signed URLs into response data
        merged = response_data.copy()
        if col_traj is not None and row[col_traj].strip():
            traj_raw = row[col_traj].strip()
            try:
                traj_json = json.loads(traj_raw)
            except (json.JSONDecodeError, TypeError):
                traj_json = {}

            if isinstance(traj_json, dict):
                first_key = next(iter(traj_json), "")
                if "turns" in traj_json and traj_json["turns"]:
                    # Legacy full-blob format — merge ExternalApp steps
                    for sk in traj_json["turns"][0]:
                        sv_t = traj_json["turns"][0][sk]
                        if not isinstance(sv_t, dict) or sv_t.get("type") != "ExternalApp":
                            continue
                        if sk in merged.get("turns", [{}])[0]:
                            merged["turns"][0][sk] = sv_t
                elif first_key.endswith(".json"):
                    # Compact {filename: url} format — nothing to merge into response
                    # (download_trajectories.py already downloaded the files)
                    pass

        print(f"Task: {task_id}  Attempt: {attempt_id}")
        if annotator:
            print(f"  Annotator: {annotator} ({email})")

        # Load trajectory data as messages
        traj_messages_map = load_trajectories_for_task(task_id, attempt_id, merged)
        total_msgs = sum(len(m) for m in traj_messages_map.values())
        print(f"  Loaded trajectories for {len(traj_messages_map)} runs ({total_msgs} total messages)")

        # Generate viewer
        html, stats = generate_viewer(task_id, merged, traj_messages_map, email=email, annotator=annotator)

        out_path = os.path.join(OUT_DIR, f"{task_id}_viewer.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)

        size_kb = len(html) // 1024
        print(f"  -> {out_path} ({size_kb}KB)")
        generated.append({"task_id": task_id, "attempt_id": attempt_id, "review_level": review_level, "path": out_path, "size": len(html), "stats": stats})
        print()

    print(f"Done: {len(generated)} viewer(s) generated")

    # Generate homepage
    generate_homepage(generated)

    return generated


def generate_homepage(generated):
    """Generate index.html homepage with summary table linking to all viewers."""

    rows_html = ""
    all_max_tools = []
    all_min_tools = []
    for g in generated:
        s = g["stats"]
        tid = s["task_id"]
        review_level = g["review_level"]
        viewer_file = f"{tid}_viewer.html"

        vp_display = s["verifier_display"]
        vp_cls = "hp-green" if s["has_verifier"] and s["n_verifier_pass"] > s["n_runs"] // 2 else "hp-amber" if not s["has_verifier"] else "hp-red"
        p1_val = s["pass_at_1"]
        p1_cls = "hp-green" if p1_val not in ("N/A", "0%") else "hp-red"

        email_val = s.get("email", "")
        annotator_val = s.get("annotator", "")
        email_link = f'<a href="https://dashboard.scale.com/corp/genai-ops-hub/search/{_esc(email_val)}" target="_blank" onclick="event.stopPropagation()" style="color:var(--accent);text-decoration:underline">{_esc(email_val)}</a>' if email_val else "&mdash;"

        # Consolidated Max Tools column: prefer pass, else overall valid, else N/A
        no_valid = s["n_runs"] == 0
        mtp = s["max_tools_pass"]
        has_max_pass = mtp != "N/A"
        if no_valid:
            max_val = "N/A"
            max_cls = "hp-amber"
            max_suffix = ""
            max_tooltip = "No valid runs (agent errored or verifier did not run)"
        elif has_max_pass:
            max_val = str(mtp)
            max_cls = "hp-green"
            max_suffix = ' <span class="hp-pass-tag">(pass)</span>'
            max_tooltip = f"Max tools across verifier-passed runs"
        else:
            max_val = str(s["max_tools"])
            max_cls = ""
            max_suffix = ""
            max_tooltip = f"Max tools across all valid runs (no verifier passes)"

        # Consolidated Min Tools column: prefer pass, else overall valid, else N/A
        mntp = s["min_tools_pass"]
        has_min_pass = mntp != "N/A"
        if no_valid:
            min_val = "N/A"
            min_cls = "hp-amber"
            min_suffix = ""
            min_tooltip = "No valid runs (agent errored or verifier did not run)"
        elif has_min_pass:
            min_val = str(mntp)
            min_cls = "hp-green"
            min_suffix = ' <span class="hp-pass-tag">(pass)</span>'
            min_tooltip = f"Min tools across verifier-passed runs"
        else:
            min_val = str(s["min_tools"])
            min_cls = ""
            min_suffix = ""
            min_tooltip = f"Min tools across all valid runs (no verifier passes)"

        if not no_valid:
            all_max_tools.append(int(max_val))
            all_min_tools.append(int(min_val))

        rows_html += f"""<tr class="hp-row" onclick="window.location.href='{viewer_file}'">
  <td class="hp-id"><code>{_esc(tid)}</code></td>
  <td class="hp-review-level">{_esc(review_level)}</td>
  <td class="hp-persona">{_esc(s["persona"])}</td>
  <td class="hp-annotator">{_esc(annotator_val) if annotator_val else '&mdash;'}</td>
  <td class="hp-email">{email_link}</td>
  <td class="{"hp-amber" if s["n_runs"] == 0 else ""}">{s["agent_display"]}</td>
  <td class="{vp_cls}">{vp_display}</td>
  <td class="{p1_cls}">{_esc(p1_val)}</td>
  <td>{s["n_criteria"]}</td>
  <td class="{max_cls}" title="{max_tooltip}">{max_val}{max_suffix}</td>
  <td class="{min_cls}" title="{min_tooltip}">{min_val}{min_suffix}</td>
</tr>
"""
    avg_max_tools = round(sum(all_max_tools) / len(all_max_tools)) if all_max_tools else 0
    avg_min_tools = round(sum(all_min_tools) / len(all_min_tools)) if all_min_tools else 0
    total_runs = sum(g["stats"]["n_runs"] for g in generated)

    # ── Collect chart data ──
    # 1. Box plot: use the same consolidated value shown in the overview table
    #    (pass value if available, else overall valid; skip tasks with no valid runs)
    bp_max_vals = []
    bp_min_vals = []
    for g in generated:
        s = g["stats"]
        if s["n_runs"] == 0:
            continue  # no valid runs, skip
        bp_max_vals.append(s["max_tools_pass"] if s["max_tools_pass"] != "N/A" else s["max_tools"])
        bp_min_vals.append(s["min_tools_pass"] if s["min_tools_pass"] != "N/A" else s["min_tools"])

    # 2. Tool usage: aggregate across all tasks
    from collections import Counter as _C2
    global_tool_freq = _C2()
    for g in generated:
        for k, v in g["stats"].get("tool_freq", {}).items():
            global_tool_freq[k] += v
    tool_names = [t for t, _ in global_tool_freq.most_common()]
    tool_counts = [global_tool_freq[t] for t in tool_names]

    # 3. Verifier passes histogram: how many tasks have 0, 1, 2, ... passes (skip tasks without verifier)
    max_possible = max((g["stats"]["n_runs"] for g in generated), default=6)
    vp_hist = [0] * (max_possible + 1)
    for g in generated:
        if g["stats"]["has_verifier"]:
            vp_hist[g["stats"]["n_verifier_pass"]] += 1

    # 4. Persona usage
    persona_counter = _C2(g["stats"]["persona"] for g in generated)
    persona_names = [p for p, _ in persona_counter.most_common()]
    persona_counts = [persona_counter[p] for p in persona_names]

    # JSON-safe for embedding
    import json as _j2
    bp_max_js = _j2.dumps(bp_max_vals)
    bp_min_js = _j2.dumps(bp_min_vals)
    tool_names_js = _j2.dumps(tool_names)
    tool_counts_js = _j2.dumps(tool_counts)
    vp_hist_js = _j2.dumps(vp_hist)
    persona_names_js = _j2.dumps(persona_names)
    persona_counts_js = _j2.dumps(persona_counts)

    homepage = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MCP Advanced — Task Overview</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/@sgratzl/chartjs-chart-boxplot@4"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
*{{margin:0;padding:0;box-sizing:border-box;}}
:root{{
  --bg:#f0f2f5;--bg2:#fff;--bg3:#f6f8fa;--border:#d8dee4;
  --text:#1b1f24;--text2:#4d5561;--text3:#656d76;--text4:#8b949e;
  --accent:#0969da;--green:#1a7f37;--red:#cf222e;--amber:#bf8700;
}}
body.dark{{
  --bg:#0d1117;--bg2:#161b22;--bg3:#1c2129;--border:#30363d;
  --text:#e6edf3;--text2:#b1bac4;--text3:#8b949e;--text4:#6e7681;
  --accent:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922;
}}
body{{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;min-height:100vh;}}

.topbar{{background:var(--bg2);border-bottom:1px solid var(--border);padding:10px 16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;}}
.topbar h1{{font-size:1rem;font-weight:700;white-space:nowrap;}}
.topbar .subtitle{{font-size:.72rem;color:var(--text4);margin-left:10px;font-weight:400;}}
.nav-stats{{display:flex;gap:14px;flex:1;flex-wrap:wrap;}}
.nav-stat{{font-size:.68rem;color:var(--text3);font-weight:600;white-space:nowrap;}}
.nav-stat span{{font-family:'SF Mono',ui-monospace,monospace;font-weight:700;color:var(--text);margin-left:3px;}}
.btn{{background:var(--bg3);border:1px solid var(--border);border-radius:16px;padding:4px 11px;font-size:.7rem;font-weight:600;color:var(--text3);cursor:pointer;font-family:inherit;}}
.btn:hover{{opacity:.8;}}

.container{{max-width:100%;margin:0 auto;padding:20px 16px;}}

/* ── Chart cards (thumbnails) ── */
.charts-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px;}}
@media(max-width:900px){{.charts-grid{{grid-template-columns:repeat(2,1fr);}}}}
.chart-card{{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:10px;cursor:pointer;transition:all .15s;position:relative;}}
.chart-card:hover{{box-shadow:0 2px 12px rgba(0,0,0,.1);transform:translateY(-1px);}}
.chart-card .cc-title{{font-size:.6rem;text-transform:uppercase;letter-spacing:.04em;color:var(--text4);font-weight:600;margin-bottom:6px;}}
.chart-card canvas{{width:100%!important;height:120px!important;}}
.chart-card .cc-expand{{position:absolute;top:8px;right:10px;font-size:.6rem;color:var(--text4);opacity:0;transition:opacity .15s;}}
.chart-card:hover .cc-expand{{opacity:1;}}

/* ── Chart modal ── */
.chart-modal-overlay{{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:8000;display:none;align-items:center;justify-content:center;}}
.chart-modal-overlay.active{{display:flex;}}
.chart-modal{{background:var(--bg2);border:1px solid var(--border);border-radius:12px;width:90vw;max-width:900px;max-height:85vh;padding:20px 24px;position:relative;}}
.chart-modal .cm-title{{font-size:.9rem;font-weight:700;margin-bottom:12px;}}
.chart-modal .cm-close{{position:absolute;top:12px;right:16px;background:none;border:none;font-size:1.1rem;color:var(--text3);cursor:pointer;}}
.chart-modal canvas{{width:100%!important;height:55vh!important;}}

table{{width:100%;border-collapse:collapse;background:var(--bg2);border:1px solid var(--border);border-radius:8px;overflow:hidden;font-size:.74rem;}}
thead th{{padding:7px 8px;text-align:left;font-size:.6rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--text4);background:var(--bg3);border-bottom:1px solid var(--border);position:sticky;top:0;white-space:nowrap;}}
tbody td{{padding:7px 8px;border-bottom:1px solid var(--border);vertical-align:middle;}}
.hp-row{{cursor:pointer;transition:background .1s;}}
.hp-row:hover{{background:var(--bg3);}}
.hp-row:last-child td{{border-bottom:none;}}
.hp-id code{{font-family:'SF Mono',ui-monospace,monospace;font-size:.68rem;color:var(--accent);background:rgba(9,105,218,0.06);padding:2px 5px;border-radius:4px;}}
body.dark .hp-id code{{background:rgba(88,166,255,0.08);}}
.hp-persona{{max-width:150px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text2);font-size:.72rem;}}
.hp-annotator{{max-width:160px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text2);font-size:.72rem;}}
.hp-email{{max-width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.68rem;}}
.hp-green{{color:var(--green);font-weight:600;}}
.hp-red{{color:var(--red);font-weight:600;}}
.hp-amber{{color:var(--text3);font-weight:600;font-style:italic;}}
.hp-pass-tag{{font-size:.58rem;color:var(--green);font-weight:600;opacity:.8;}}
th[title]{{cursor:help;}}

.footer{{text-align:center;padding:16px;font-size:.65rem;color:var(--text4);}}

/* ── Walkthrough Tour ── */
.wt-spotlight{{position:fixed;z-index:9001;border-radius:8px;box-shadow:0 0 0 4000px rgba(0,0,0,.55);pointer-events:none;transition:all .3s ease;display:none;}}
.wt-tooltip{{position:fixed;z-index:9002;background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px 20px;max-width:360px;box-shadow:0 8px 32px rgba(0,0,0,.25);font-size:.82rem;line-height:1.55;transition:all .3s ease;display:none;}}
.wt-tooltip-title{{font-weight:700;font-size:.9rem;margin-bottom:6px;color:var(--text);}}
.wt-tooltip-body{{color:var(--text2);margin-bottom:14px;}}
.wt-tooltip-footer{{display:flex;align-items:center;justify-content:space-between;gap:8px;}}
.wt-step-count{{font-size:.68rem;color:var(--text4);font-weight:500;}}
.wt-btn{{border:1px solid var(--border);border-radius:6px;padding:5px 14px;font-size:.72rem;font-weight:600;cursor:pointer;font-family:inherit;transition:all .15s;background:var(--bg3);color:var(--text2);}}
.wt-btn:hover{{background:var(--border);}}
.wt-btn-primary{{background:var(--accent);color:#fff;border-color:var(--accent);}}
.wt-btn-primary:hover{{opacity:.85;}}
.wt-btn-skip{{background:transparent;border:none;color:var(--text4);font-size:.7rem;cursor:pointer;font-family:inherit;}}
.wt-btn-skip:hover{{color:var(--text2);}}
.wt-arrow{{position:absolute;width:12px;height:12px;background:var(--bg2);border:1px solid var(--border);transform:rotate(45deg);z-index:-1;}}
.wt-arrow-top{{top:-7px;left:24px;border-right:none;border-bottom:none;}}
.wt-arrow-bottom{{bottom:-7px;left:24px;border-left:none;border-top:none;}}
.wt-pulse{{animation:wt-pulse-ring 1.8s ease-out infinite;}}
@keyframes wt-pulse-ring{{0%{{box-shadow:0 0 0 4000px rgba(0,0,0,.55),0 0 0 0 rgba(9,105,218,.4)}}70%{{box-shadow:0 0 0 4000px rgba(0,0,0,.55),0 0 0 10px rgba(9,105,218,0)}}100%{{box-shadow:0 0 0 4000px rgba(0,0,0,.55),0 0 0 0 rgba(9,105,218,0)}}}}
</style>
</head>
<body>
<div class="topbar" id="hp-topbar">
  <div><h1>MCP Advanced</h1></div>
  <div class="nav-stats">
    <div class="nav-stat">Tasks:<span>{len(generated)}</span></div>
    <div class="nav-stat">Avg Max Tools:<span>{avg_max_tools}</span></div>
    <div class="nav-stat">Avg Min Tools:<span>{avg_min_tools}</span></div>
  </div>
  <button class="btn" onclick="document.body.classList.toggle('dark');this.innerHTML=document.body.classList.contains('dark')?'&#9679; Light':'&#9679; Dark';rebuildCharts()">&#9679; Dark</button>
</div>
<div class="container">

  <!-- Chart thumbnails -->
  <div class="charts-grid" id="hp-charts">
    <div class="chart-card" onclick="openChart(0)">
      <div class="cc-title">Tool Count Distribution</div>
      <canvas id="thumb-0"></canvas>
      <span class="cc-expand">&#x26F6; expand</span>
    </div>
    <div class="chart-card" onclick="openChart(1)">
      <div class="cc-title">Tool Usage (All Tasks)</div>
      <canvas id="thumb-1"></canvas>
      <span class="cc-expand">&#x26F6; expand</span>
    </div>
    <div class="chart-card" onclick="openChart(2)">
      <div class="cc-title">Verifier Passes</div>
      <canvas id="thumb-2"></canvas>
      <span class="cc-expand">&#x26F6; expand</span>
    </div>
    <div class="chart-card" onclick="openChart(3)">
      <div class="cc-title">Persona Usage</div>
      <canvas id="thumb-3"></canvas>
      <span class="cc-expand">&#x26F6; expand</span>
    </div>
  </div>

  <table id="hp-table">
    <thead>
      <tr>
        <th>Task ID</th>
        <th>Review Level</th>
        <th>Persona</th>
        <th>Annotator</th>
        <th>Annotator Email</th>
        <th>Agent Runs</th>
        <th>Verifier Pass</th>
        <th>Pass@1</th>
        <th>Criteria</th>
        <th title="Max tools from verifier-passed runs if any passed, otherwise max tools across all runs">Max Tools</th>
        <th title="Min tools from verifier-passed runs if any passed, otherwise min tools across all runs">Min Tools</th>
      </tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
</div>
<div class="footer">Click any row to open the task viewer &middot; Click charts to expand &middot; Press <kbd style="background:var(--bg3);border:1px solid var(--border);border-radius:3px;padding:1px 5px;font-size:.65rem">?</kbd> for walkthrough</div>

<!-- Chart Modal -->
<div class="chart-modal-overlay" id="chart-modal-overlay">
  <div class="chart-modal">
    <div class="cm-title" id="cm-title"></div>
    <button class="cm-close" id="cm-close">&#10005;</button>
    <canvas id="modal-canvas"></canvas>
  </div>
</div>

<div class="wt-spotlight wt-pulse" id="wt-spotlight"></div>
<div class="wt-tooltip" id="wt-tooltip"></div>

<script>
// ── Chart data ──
var BP_MAX={bp_max_js};
var BP_MIN={bp_min_js};
var TOOL_NAMES={tool_names_js};
var TOOL_COUNTS={tool_counts_js};
var VP_HIST={vp_hist_js};
var PERSONA_NAMES={persona_names_js};
var PERSONA_COUNTS={persona_counts_js};

function getColors(){{
  var s=getComputedStyle(document.body);
  return {{
    accent:s.getPropertyValue('--accent').trim(),
    green:s.getPropertyValue('--green').trim(),
    red:s.getPropertyValue('--red').trim(),
    text:s.getPropertyValue('--text').trim(),
    text4:s.getPropertyValue('--text4').trim(),
    border:s.getPropertyValue('--border').trim(),
    bg3:s.getPropertyValue('--bg3').trim(),
    amber:'#bf8700'
  }};
}}

var thumbCharts=[null,null,null,null];
var modalChart=null;

function chartConfigs(large){{
  var c=getColors();
  var fs=large?12:9;
  var pad=large?16:4;
  var leg=large;

  // 0: Real box plot – two boxes: Max Tools & Min Tools across all tasks
  var cfg0={{
    type:'boxplot',
    data:{{
      labels:['Max Tools','Min Tools'],
      datasets:[{{
        label:'Tool Count',
        data:[BP_MAX, BP_MIN],
        backgroundColor:[c.accent+'35', c.green+'35'],
        borderColor:[c.accent, c.green],
        borderWidth:1.5,
        outlierRadius:0,
        itemRadius:0,
        meanRadius:0,
        medianColor:[c.accent, c.green],
      }}]
    }},
    options:{{
      responsive:true,
      maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{
        x:{{ticks:{{font:{{size:fs}},color:c.text}},grid:{{display:false}}}},
        y:{{title:{{display:large,text:'Tools',font:{{size:11}},color:c.text4}},ticks:{{font:{{size:fs}},color:c.text4}},grid:{{color:c.border+'44'}}}}
      }}
    }}
  }};

  // 1: Tool usage bar
  var cfg1={{
    type:'bar',
    data:{{labels:TOOL_NAMES,datasets:[{{label:'Calls',data:TOOL_COUNTS,backgroundColor:c.accent+'88',borderColor:c.accent,borderWidth:1}}]}},
    options:{{indexAxis:'y',responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{ticks:{{font:{{size:fs}},color:c.text4}},grid:{{color:c.border+'44'}}}},y:{{ticks:{{font:{{size:fs}},color:c.text}},grid:{{display:false}}}}}}}}
  }};

  // 2: Verifier passes histogram
  var vpLabels=[];for(var i=0;i<VP_HIST.length;i++)vpLabels.push(String(i));
  var vpColors=VP_HIST.map(function(v,i){{return i===0?c.red+'88':c.green+'88';}});
  var vpBorders=VP_HIST.map(function(v,i){{return i===0?c.red:c.green;}});
  var cfg2={{
    type:'bar',
    data:{{labels:vpLabels,datasets:[{{label:'Tasks',data:VP_HIST,backgroundColor:vpColors,borderColor:vpBorders,borderWidth:1}}]}},
    options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{title:{{display:large,text:'Verifier Passes',font:{{size:11}},color:c.text4}},ticks:{{font:{{size:fs}},color:c.text4}},grid:{{display:false}}}},y:{{title:{{display:large,text:'# Tasks',font:{{size:11}},color:c.text4}},ticks:{{stepSize:1,font:{{size:fs}},color:c.text4}},grid:{{color:c.border+'44'}}}}}}}}
  }};

  // 3: Persona usage bar
  var pColors=[c.accent+'88',c.green+'88',c.amber+'88',c.red+'88'];
  var pBorders=[c.accent,c.green,c.amber,c.red];
  var cfg3={{
    type:'bar',
    data:{{labels:PERSONA_NAMES,datasets:[{{label:'Tasks',data:PERSONA_COUNTS,backgroundColor:PERSONA_NAMES.map(function(_,i){{return pColors[i%pColors.length];}}),borderColor:PERSONA_NAMES.map(function(_,i){{return pBorders[i%pBorders.length];}}),borderWidth:1}}]}},
    options:{{responsive:true,maintainAspectRatio:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{ticks:{{font:{{size:fs}},color:c.text4,maxRotation:45}},grid:{{display:false}}}},y:{{ticks:{{stepSize:1,font:{{size:fs}},color:c.text4}},grid:{{color:c.border+'44'}}}}}}}}
  }};

  return [cfg0,cfg1,cfg2,cfg3];
}}

var chartTitles=['Tool Count Distribution (Box Plot)','Tool Usage Across All Tasks','Verifier Pass Distribution','Persona Usage'];

function buildThumbs(){{
  var cfgs=chartConfigs(false);
  for(var i=0;i<4;i++){{
    var ctx=document.getElementById('thumb-'+i).getContext('2d');
    thumbCharts[i]=new Chart(ctx,cfgs[i]);
  }}
}}

function rebuildCharts(){{
  for(var i=0;i<4;i++){{if(thumbCharts[i])thumbCharts[i].destroy();}}
  buildThumbs();
  if(modalChart){{modalChart.destroy();modalChart=null;}}
}}

function openChart(idx){{
  var overlay=document.getElementById('chart-modal-overlay');
  document.getElementById('cm-title').textContent=chartTitles[idx];
  overlay.classList.add('active');
  if(modalChart)modalChart.destroy();
  var ctx=document.getElementById('modal-canvas').getContext('2d');
  var cfgs=chartConfigs(true);
  modalChart=new Chart(ctx,cfgs[idx]);
}}

document.getElementById('cm-close').addEventListener('click',function(){{
  document.getElementById('chart-modal-overlay').classList.remove('active');
  if(modalChart){{modalChart.destroy();modalChart=null;}}
}});
document.getElementById('chart-modal-overlay').addEventListener('click',function(e){{
  if(e.target===e.currentTarget){{
    e.currentTarget.classList.remove('active');
    if(modalChart){{modalChart.destroy();modalChart=null;}}
  }}
}});
document.addEventListener('keydown',function(e){{
  if(e.key==='Escape'){{
    var ov=document.getElementById('chart-modal-overlay');
    if(ov.classList.contains('active')){{ov.classList.remove('active');if(modalChart){{modalChart.destroy();modalChart=null;}}}}
  }}
}});

buildThumbs();

// ── Walkthrough ──
(function(){{
  var steps=[
    {{el:'#hp-topbar',title:'Navigation Bar',body:'Quick stats at a glance: task count, total runs, and average tool usage across all tasks.',pos:'bottom'}},
    {{el:'#hp-charts',title:'Analytics Charts',body:'Four interactive charts showing tool distribution, tool usage frequency, verifier pass rates, and persona breakdown. Click any chart to expand it to full screen.',pos:'bottom'}},
    {{el:'#hp-table thead',title:'Task Table',body:'Each row is a task. Columns show the persona, annotator, pass rates, criteria count, and tool usage. Max/Min Tools show pass-run values when available (tagged with "(pass)"). Hover column headers for details.',pos:'bottom'}},
    {{el:'.hp-row',title:'Task Row',body:'Click any row to open the full task viewer with agent trajectories, rubric scorecard, and detailed overview.',pos:'bottom'}}
  ];

  var spot=document.getElementById('wt-spotlight');
  var tip=document.getElementById('wt-tooltip');
  var idx=0;

  function show(){{
    if(idx>=steps.length){{end();return;}}
    var s=steps[idx];
    var el=document.querySelector(s.el);
    if(!el){{idx++;show();return;}}
    el.scrollIntoView({{behavior:'smooth',block:'center'}});
    setTimeout(function(){{
      var r=el.getBoundingClientRect();
      var pad=6;
      spot.style.display='block';
      spot.style.top=(r.top-pad)+'px';
      spot.style.left=(r.left-pad)+'px';
      spot.style.width=(r.width+pad*2)+'px';
      spot.style.height=(r.height+pad*2)+'px';
      var n=idx+1,total=steps.length;
      var h='<div class="wt-tooltip-title">'+s.title+'</div>';
      h+='<div class="wt-tooltip-body">'+s.body+'</div>';
      h+='<div class="wt-tooltip-footer">';
      h+='<span class="wt-step-count">'+n+' of '+total+'</span>';
      h+='<div style="display:flex;gap:6px;align-items:center">';
      h+='<button class="wt-btn-skip" onclick="window._hpEnd()">Skip</button>';
      if(idx>0)h+='<button class="wt-btn" onclick="window._hpPrev()">Back</button>';
      h+='<button class="wt-btn wt-btn-primary" onclick="window._hpNext()">'+(n===total?'Done':'Next')+'</button>';
      h+='</div></div>';
      var arrowCls='wt-arrow-top';
      tip.innerHTML=h;
      tip.style.display='block';
      var tw=360,tH=tip.offsetHeight;
      if(s.pos==='bottom'){{
        tip.style.top=(r.bottom+14)+'px';
        tip.style.left=Math.max(8,Math.min(r.left,window.innerWidth-tw-16))+'px';
      }}else{{
        tip.style.top=(r.top-tH-14)+'px';
        tip.style.left=Math.max(8,Math.min(r.left,window.innerWidth-tw-16))+'px';
        arrowCls='wt-arrow-bottom';
      }}
      var oldA=tip.querySelector('.wt-arrow');if(oldA)oldA.remove();
      var ar=document.createElement('div');ar.className='wt-arrow '+arrowCls;tip.appendChild(ar);
    }},350);
  }}

  function end(){{spot.style.display='none';tip.style.display='none';try{{sessionStorage.setItem('wt_done_hp','1');}}catch(e){{}}}}
  window._hpNext=function(){{idx++;show();}};
  window._hpPrev=function(){{if(idx>0)idx--;show();}};
  window._hpEnd=end;

  setTimeout(function(){{
    try{{if(sessionStorage.getItem('wt_done_hp'))return;}}catch(e){{}}
    show();
  }},800);

  document.addEventListener('keydown',function(e){{
    if(e.key==='?'&&!e.ctrlKey&&!e.metaKey){{
      idx=0;try{{sessionStorage.removeItem('wt_done_hp');}}catch(ex){{}}
      show();
    }}
  }});
}})();
</script>
</body>
</html>"""

    out_path = os.path.join(OUT_DIR, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(homepage)
    print(f"\nHomepage: {out_path}")


if __name__ == "__main__":
    main()
