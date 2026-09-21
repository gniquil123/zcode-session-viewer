# -*- coding: utf-8 -*-
"""ZCode Session Viewer — 浏览、搜索与管理所有 ZCode 会话的零依赖本地工具

数据源(自动探测,均只读):
  ~/.zcode/cli/db/db.sqlite        会话内容(message / part)
  ~/.zcode/v2/tasks-index.sqlite   会话列表索引(归档 / 置顶 / 项目)

唯一写操作:归档 / 移出归档(更新 tasks.archived,与 ZCode 界面同一字段)。

用法:
    python src/viewer.py [--port 8787] [--no-open]
                         [--db-main PATH] [--db-tasks PATH]
"""
import argparse
import json
import os
import re
import sqlite3
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote

# DB paths are resolved in main() — see detect_zcode_home()
DB_MAIN = None
DB_TASKS = None

# User-saved paths (written by the in-app setup dialog)
CONFIG_FILE = Path.home() / ".zcode-session-viewer.json"


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), "utf-8")


def detect_zcode_home():
    """Find the ZCode data directory automatically.

    Candidate order: $ZCODE_HOME, ~/.zcode (default layout on all platforms),
    a few common per-OS fallbacks. Returns the first directory that actually
    contains one of the two databases, else None.
    """
    cands = []
    env = os.environ.get("ZCODE_HOME")
    if env:
        cands.append(Path(env).expanduser())
    home = Path.home()
    cands += [
        home / ".zcode",
        home / ".config" / "zcode",
        home / ".config" / "ZCode",
        home / "Library" / "Application Support" / "ZCode",
        home / "Library" / "Application Support" / "zcode",
    ]
    for c in cands:
        if (c / "cli" / "db" / "db.sqlite").exists() or (c / "v2" / "tasks-index.sqlite").exists():
            return c
    return None


def q(db, sql, params=()):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        con.close()


def set_archived(sid, flag):
    """归档/移出归档:更新 ZCode 界面读取的同一字段。返回受影响行数。"""
    con = sqlite3.connect(DB_TASKS, timeout=3)
    try:
        cur = con.execute(
            "UPDATE tasks SET archived = ? WHERE task_id = ?",
            (1 if flag else 0, sid),
        )
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def list_sessions(query="", archived="all"):
    """合并两个库:tasks-index(归档/置顶/项目) + db.session(消息数)。"""
    tasks = q(
        DB_TASKS,
        """SELECT task_id, title, workspace_path, created_at, updated_at,
                  pinned, archived, forked_from_task_id
           FROM tasks WHERE deleted = 0""",
    )
    sess = q(
        DB_MAIN,
        """SELECT s.id, s.title, s.directory, s.parent_id,
                  (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) AS msgs
           FROM session s""",
    )
    items = {}
    for t in tasks:
        items[t["task_id"]] = {
            "id": t["task_id"],
            "title": t["title"] or "",
            "project": t["workspace_path"] or "",
            "time_created": int(t["created_at"] or 0),
            "time_updated": int(t["updated_at"] or 0),
            "pinned": str(t["pinned"]) == "1",
            "archived": str(t["archived"]) == "1",
            "parent_id": t["forked_from_task_id"] or None,
            "msgs": 0,
            "source": "ui",
        }
    for s in sess:
        it = items.get(s["id"])
        if it:
            it["msgs"] = s["msgs"]
            if s["title"] and not it["title"]:
                it["title"] = s["title"]
        else:
            items[s["id"]] = {
                "id": s["id"],
                "title": s["title"] or "",
                "project": s["directory"] or "",
                "time_created": 0,
                "time_updated": 0,
                "pinned": False,
                "archived": False,
                "parent_id": s["parent_id"],
                "msgs": s["msgs"],
                "source": "cli",
            }

    rows = list(items.values())
    if archived == "yes":
        rows = [r for r in rows if r["archived"]]
    elif archived == "no":
        rows = [r for r in rows if not r["archived"]]
    if query:
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        hit_ids = {
            r["session_id"]
            for r in q(
                DB_MAIN,
                """SELECT DISTINCT m.session_id AS session_id FROM part p
                   JOIN message m ON p.message_id = m.id
                   WHERE json_extract(p.data,'$.type')='text'
                     AND json_extract(p.data,'$.text') LIKE ? ESCAPE '\\'""",
                (like,),
            )
        }
        ql = query.lower()
        rows = [
            r
            for r in rows
            if ql in (r["title"] or "").lower()
            or ql in r["project"].lower()
            or ql in r["id"].lower()
            or r["id"] in hit_ids
        ]
    rows.sort(key=lambda r: r["time_updated"], reverse=True)
    return rows


TOOL_BRIEF_KEYS = [
    "command", "file_path", "url", "description", "query", "skill",
    "pattern", "path", "table", "task_id", "run_id", "title", "content",
]


def get_session(sid):
    sess = q(DB_MAIN, "SELECT id, title, directory FROM session WHERE id = ?", (sid,))
    tasks = q(
        DB_TASKS,
        "SELECT title, archived, pinned, workspace_path, forked_from_task_id FROM tasks WHERE task_id = ?",
        (sid,),
    )
    if not sess and not tasks:
        return None
    msgs = q(
        DB_MAIN,
        """SELECT id, sequence, time_created, data FROM message
           WHERE session_id = ? ORDER BY sequence""",
        (sid,),
    )
    parts = q(
        DB_MAIN,
        "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY sequence",
        (sid,),
    )
    by_msg = {}
    for p in parts:
        by_msg.setdefault(p["message_id"], []).append(json.loads(p["data"]))

    out = []
    for m in msgs:
        try:
            meta = json.loads(m["data"])
        except Exception:
            meta = {}
        role = meta.get("role", "?")
        sem = meta.get("semantics") or {}
        if meta.get("synthetic") or sem.get("uiVisibility") == "hidden" or meta.get("visibility") == "model-only":
            continue
        rendered = []
        for p in by_msg.get(m["id"], []):
            t = p.get("type")
            if t == "text":
                if p.get("text", "").strip():
                    rendered.append({"kind": "text", "text": p["text"]})
            elif t == "reasoning":
                if p.get("text", "").strip():
                    rendered.append({"kind": "reasoning", "text": p["text"]})
            elif t == "tool":
                st = p.get("state") or {}
                inp = st.get("input") or {}
                brief = ""
                for k in TOOL_BRIEF_KEYS:
                    if inp.get(k):
                        brief = str(inp[k]).replace("\n", " ")
                        break
                if not brief and isinstance(inp, dict) and inp:
                    brief = json.dumps(inp, ensure_ascii=False)
                rendered.append({
                    "kind": "tool",
                    "tool": p.get("tool", "?"),
                    "status": st.get("status", ""),
                    "brief": brief[:300],
                    "input": json.dumps(inp, ensure_ascii=False, indent=1)[:6000],
                    "output": str(st.get("output", ""))[:5000],
                })
        if rendered:
            out.append({
                "role": role,
                "time": m["time_created"],
                "model": meta.get("modelId", ""),
                "parts": rendered,
            })

    t = tasks[0] if tasks else {}
    s = sess[0] if sess else {}
    return {
        "id": sid,
        "title": t.get("title") or s.get("title") or "",
        "directory": t.get("workspace_path") or s.get("directory") or "",
        "archived": str(t.get("archived", "0")) == "1",
        "pinned": str(t.get("pinned", "0")) == "1",
        "parent_id": t.get("forked_from_task_id") or s.get("parent_id"),
        "messages": out,
    }


# ---------------- 导出 ----------------

EXPORT_I18N = {
    "zh": {"user": "用户", "asst": "助手", "thinking": "思考过程", "input": "输入",
           "output": "输出", "sid": "会话 ID", "proj": "项目", "count": "消息数"},
    "en": {"user": "User", "asst": "Assistant", "thinking": "Thinking", "input": "Input",
           "output": "Output", "sid": "Session ID", "proj": "Project", "count": "Messages"},
    "ru": {"user": "Пользователь", "asst": "Ассистент", "thinking": "Размышления", "input": "Ввод",
           "output": "Вывод", "sid": "ID сессии", "proj": "Проект", "count": "Сообщений"},
}


def export_raw(sid):
    """ZCode 原始记录导出:session 整行 + 每条 message 整行 + 其下每个 part 整行,
    data 列保持库里的原样 JSON,零过滤零截断。返回 JSONL 文本,会话不存在返回 None。"""
    sess = q(DB_MAIN, "SELECT * FROM session WHERE id = ?", (sid,))
    if not sess:
        return None
    msgs = q(DB_MAIN, "SELECT * FROM message WHERE session_id = ? ORDER BY sequence", (sid,))
    parts = q(DB_MAIN, "SELECT * FROM part WHERE session_id = ? ORDER BY sequence", (sid,))
    by_msg = {}
    for p in parts:
        by_msg.setdefault(p["message_id"], []).append(p)

    def parse(d):
        try:
            return json.loads(d)
        except Exception:
            return d

    lines = [json.dumps({"type": "session", "row": sess[0]}, ensure_ascii=False, default=str)]
    for m in msgs:
        row = dict(m)
        row["data"] = parse(row["data"])
        lines.append(json.dumps({"type": "message", "row": row}, ensure_ascii=False, default=str))
        for p in by_msg.get(m["id"], []):
            prow = dict(p)
            prow["data"] = parse(prow["data"])
            lines.append(json.dumps({"type": "part", "row": prow}, ensure_ascii=False, default=str))
    return "\n".join(lines)


def export_session(sid, fmt, lang="zh"):
    """导出一个会话。返回 (body, 扩展名, MIME, 标题);会话不存在时 body 为 None。"""
    s = get_session(sid)
    if s is None:
        return None, "", "", ""
    tr = EXPORT_I18N.get(lang, EXPORT_I18N["zh"])

    def ts(ms):
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else ""

    if fmt == "md":
        out = [f"# {s['title']}", "", f"- {tr['sid']}:`{s['id']}`"]
        if s.get("directory"):
            out.append(f"- {tr['proj']}:`{s['directory']}`")
        out += [f"- {tr['count']}:{len(s['messages'])}", "", "---", ""]
        for m in s["messages"]:
            who = tr["user"] if m["role"] == "user" else tr["asst"]
            model = f" `{m['model']}`" if m["model"] else ""
            out += [f"## {who}{model} · {ts(m['time'])}", ""]
            for p in m["parts"]:
                if p["kind"] == "text":
                    out += [p["text"], ""]
                elif p["kind"] == "reasoning":
                    out += ["<details>", f"<summary>💭 {tr['thinking']}</summary>", "",
                            p["text"], "", "</details>", ""]
                else:
                    out += [f"**🔧 {p['tool']}** `[{p['status']}]` {p['brief'][:200]}", ""]
                    if p["input"] and p["input"] != "{}":
                        out += ["<details>", f"<summary>{tr['input']}</summary>", "",
                                "```json", p["input"], "```", "</details>", ""]
                    if p["output"]:
                        out += ["<details>", f"<summary>{tr['output']}</summary>", "",
                                "```", p["output"], "```", "</details>", ""]
            out += ["---", ""]
        body, ext, mime = "\n".join(out), ".md", "text/markdown; charset=utf-8"
    elif fmt == "jsonl":
        lines = [json.dumps(
            {"type": "session", "id": s["id"], "title": s["title"],
             "directory": s["directory"], "archived": s["archived"],
             "parent_id": s["parent_id"], "message_count": len(s["messages"])},
            ensure_ascii=False)]
        for m in s["messages"]:
            lines.append(json.dumps({"type": "message", **m}, ensure_ascii=False))
        body, ext, mime = "\n".join(lines), ".jsonl", "application/x-ndjson; charset=utf-8"
    elif fmt == "raw":
        body = export_raw(sid)
        if body is None:
            return None, "", "", ""
        return body, ".jsonl", "application/x-ndjson; charset=utf-8", s["title"]
    else:
        body, ext, mime = json.dumps(s, ensure_ascii=False, indent=1), ".json", "application/json; charset=utf-8"
    return body, ext, mime, s["title"]


# ---------------- 页面 ----------------

ICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
        "%3Crect width='64' height='64' rx='14' fill='%231c1f26'/%3E"
        "%3Cpath d='M12 17q0-7 7-7h26q7 0 7 7v17q0 7-7 7H31l-11 10v-10h-1q-7 0-7-7z' fill='%234f8cff'/%3E"
        "%3Ccircle cx='32' cy='25' r='11' fill='%231c1f26'/%3E"
        "%3Ccircle cx='32' cy='25' r='11' fill='none' stroke='%23e8edf5' stroke-width='2.5'/%3E"
        "%3Cpath d='M32 18.5V25l5 3.5' stroke='%23e8edf5' stroke-width='2.5' fill='none' "
        "stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")

PAGE = r"""<!doctype html>
<html lang="zh" data-theme="dark"><head><meta charset="utf-8">
<title>ZCode Session Viewer</title>
<link rel="icon" href="__ICON__">
<style>
:root{--bg:#14161a;--panel:#1c1f26;--panel2:#181b21;--line:#2b303a;--fg:#d7dce4;--dim:#8b93a1;
--acc:#4f8cff;--green:#3aa76d;--red:#e05561;
--hover:#232833;--onbg:#253a5e;--onbg2:#2a4268;--ontx:#7fb0ff;
--userbg:#20324d;--toolbg:#232936;--thinkbg:#1a1d23;--codebg:#15181d;
--thinkfg:#8fa3c4;--tname:#9fc1ff;--pretx:#b9c2cf;--codetx:#e8c07a;
--markbg:#5a4a1e;--marktx:#ffd97a;--hitbg:#6b5417;--hittx:#ffe9a8}
html[data-theme="light"]{--bg:#f5f6f8;--panel:#ffffff;--panel2:#eef1f5;--line:#d9dee6;--fg:#24292f;--dim:#68707c;
--acc:#2563eb;--green:#188a52;--red:#d43f33;
--hover:#eceff4;--onbg:#dfe9fd;--onbg2:#d2e2fc;--ontx:#1d4fd7;
--userbg:#e3ecfb;--toolbg:#f0f3f7;--thinkbg:#f2f4f8;--codebg:#f0f2f6;
--thinkfg:#5b6b8c;--tname:#2458c4;--pretx:#3a4250;--codetx:#9a6b00;
--markbg:#ffe9a0;--marktx:#5b4a00;--hitbg:#ffe08a;--hittx:#5b4a00}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.65 "Segoe UI","Microsoft YaHei",sans-serif;height:100vh;display:flex;flex-direction:column}
header{padding:8px 14px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:center;flex:0 0 auto;flex-wrap:wrap}
header b{color:var(--acc)}
#q{flex:1;min-width:160px;max-width:360px;background:var(--panel);border:1px solid var(--line);color:var(--fg);
padding:6px 10px;border-radius:6px;outline:none}
select{background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px}
main{flex:1;display:flex;min-height:0}
#list{width:360px;border-right:1px solid var(--line);overflow-y:auto;flex:0 0 auto}
#detail{flex:1;overflow-y:auto;padding:14px 26px 60px}
#dragbar{width:5px;cursor:col-resize;background:transparent;flex:0 0 auto;transition:background .15s}
#dragbar:hover,#dragbar.dragging{background:var(--acc)}
.grp{padding:6px 12px;color:var(--acc);font-size:12px;font-weight:600;background:var(--panel2);
border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
position:sticky;top:0;cursor:pointer;user-select:none;display:flex;align-items:center;gap:6px;z-index:1}
.grp .arr{flex:0 0 auto;width:12px;display:inline-block;transition:transform .15s}
.grp.closed .arr{transform:rotate(-90deg)}
.sess{padding:8px 12px;border-bottom:1px solid var(--line);cursor:pointer;position:relative}
.sess:hover{background:var(--hover)}
.sess.on{background:var(--onbg);box-shadow:inset 3px 0 0 var(--acc)}
.sess.on .t{color:var(--ontx)}
.sess.on:hover{background:var(--onbg2)}
.sess .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-right:74px}
.sess .m{color:var(--dim);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.act{position:absolute;right:8px;top:8px;font-size:11px;padding:2px 8px;border-radius:6px;
border:1px solid var(--line);background:var(--panel);color:var(--fg);cursor:pointer;display:none}
.sess:hover .act{display:block}
.act:hover{border-color:var(--acc);color:var(--acc)}
.badge{display:inline-block;font-size:11px;padding:0 6px;border-radius:8px;margin-left:6px;background:#4a3b1e;color:#e8b34b}
.badge.pin{background:#1e3a4a;color:#6cc3e8}.badge.fork{background:#3a2e4f;color:#b79ae8}
.badge.cli{background:#333a45;color:#aab3c0}
html[data-theme="light"] .badge{background:#f3e6c2;color:#7a5b00}
html[data-theme="light"] .badge.pin{background:#d9eef8;color:#0b5e79}
html[data-theme="light"] .badge.fork{background:#e8def5;color:#5b3a99}
html[data-theme="light"] .badge.cli{background:#e2e6ec;color:#4a5563}
.msg{margin:12px 0;border-radius:10px;padding:10px 16px}
.msg.user{background:var(--userbg);border-left:3px solid var(--acc)}
.msg.assistant{background:var(--panel);border-left:3px solid var(--green)}
.meta{color:var(--dim);font-size:11px;line-height:1.3;margin-bottom:2px;display:flex;gap:8px;align-items:center}
.meta b{font-size:11.5px;color:var(--dim);font-weight:600}
.msep{border-top:1px dashed var(--line);margin:10px 0 6px}
details.think{margin:6px 0;color:var(--dim)}
details.think summary{cursor:pointer;font-size:12.5px;color:var(--thinkfg);user-select:none}
details.think .body{background:var(--thinkbg);border-left:3px solid var(--line);padding:6px 12px;margin-top:4px;
white-space:pre-wrap;font-size:13px}
details.tool{background:var(--toolbg);border-radius:8px;margin:6px 0;padding:5px 12px;font-family:var(--mono);font-size:12.5px}
details.tool summary{cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;user-select:none;display:flex;gap:6px;align-items:baseline}
.tname{color:var(--tname);font-weight:600;flex:0 0 auto}
.tbrief{color:var(--dim);overflow:hidden;text-overflow:ellipsis}
.st-completed{color:var(--green)}.st-running{color:var(--acc)}.st-error{color:var(--red)}
pre{white-space:pre-wrap;word-break:break-all;margin:6px 0;color:var(--pretx);font-size:12.5px;
background:var(--codebg);border-radius:6px;padding:8px 10px}
.md{white-space:normal;word-break:break-word}
.md code{background:var(--codebg);color:var(--codetx);border-radius:4px;padding:1px 5px;font-family:var(--mono);font-size:13px}
.md pre{margin:8px 0}.md pre code{background:none;color:var(--pretx);padding:0}
.md h1,.md h2,.md h3,.md h4{margin:12px 0 6px;line-height:1.4}
.md h1{font-size:18px}.md h2{font-size:16.5px}.md h3{font-size:15px}
.md a{color:var(--acc)}.md blockquote{border-left:3px solid var(--line);margin:6px 0;padding:2px 12px;color:var(--dim)}
.md table{border-collapse:collapse;margin:8px 0}.md th,.md td{border:1px solid var(--line);padding:4px 10px;font-size:13px}
.md th{background:var(--toolbg)}.md ul,.md ol{margin:6px 0;padding-left:26px}
.md hr{border:none;border-top:1px solid var(--line);margin:12px 0}
.sysrem{color:var(--dim);font-size:12.5px;background:var(--panel2);border:1px dashed var(--line);border-radius:6px;
padding:4px 10px;margin:4px 0}
#head{position:sticky;top:-1px;background:var(--bg);padding:8px 0 10px;border-bottom:1px solid var(--line);z-index:2;margin-bottom:10px}
#sid{font-family:var(--mono);color:var(--acc);cursor:pointer}
.hint{color:var(--dim);font-size:12px}
mark{background:var(--markbg);color:var(--marktx)}
mark.hit{background:var(--hitbg);color:var(--hittx);padding:0 1px}
mark.hit.cur{background:#b45309;color:#fff;outline:2px solid #fbbf24}
#srow{display:flex;gap:8px;align-items:center;margin-top:8px}
#sq{flex:0 1 260px;background:var(--panel);border:1px solid var(--line);color:var(--fg);padding:4px 10px;border-radius:6px;outline:none}
.ibtn{background:var(--panel);border:1px solid var(--line);color:var(--fg);border-radius:6px;
padding:3px 10px;cursor:pointer;font-size:12px}
.ibtn:hover{border-color:var(--acc);color:var(--acc)}
.ibtn:disabled{opacity:.4;cursor:not-allowed}
.abtn{background:var(--userbg);border:1px solid var(--acc);color:var(--acc);border-radius:6px;padding:3px 12px;cursor:pointer;font-size:12px}
.abtn:hover{background:var(--acc);color:#fff}
.xmenu{position:relative;margin-left:auto}
.xmenu summary{list-style:none;user-select:none;cursor:pointer;display:inline-block}
.xmenu summary::-webkit-details-marker{display:none}
.xdrop{position:absolute;right:0;top:calc(100% + 4px);background:var(--panel);border:1px solid var(--line);
border-radius:8px;z-index:10;display:flex;flex-direction:column;min-width:200px;box-shadow:0 8px 24px rgba(0,0,0,.3)}
.xdrop a{padding:7px 14px;color:var(--fg);text-decoration:none;font-size:13px}
.xdrop a:hover{background:var(--onbg);color:var(--acc)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;z-index:100}
.modal.show{display:flex}
.mbox{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 24px;width:min(540px,92vw)}
.mbox h3{margin:0 0 6px;font-size:16px}
.mbox label{display:block;font-size:12.5px;color:var(--dim);margin:10px 0 4px}
.mbox input{width:100%;background:var(--codebg);border:1px solid var(--line);color:var(--fg);padding:7px 10px;border-radius:6px;outline:none;font-family:var(--mono);font-size:12.5px}
.mbox input:focus{border-color:var(--acc)}
.merr{color:var(--red);font-size:12.5px;margin-top:8px;white-space:pre-wrap}
@media(max-width:780px){#list{width:45%}}
</style></head><body>
<header>
  <b data-i18n="title">ZCode Session Viewer</b>
  <input id="q" data-i18n-ph="search_ph" placeholder=""/>
  <select id="view">
    <option value="project" data-i18n="view_project"></option>
    <option value="time" data-i18n="view_time"></option>
  </select>
  <button class="ibtn" id="foldAll" data-i18n-title="fold_all" data-i18n="fold_all"></button>
  <button class="ibtn" id="unfoldAll" data-i18n-title="unfold_all" data-i18n="unfold_all"></button>
  <select id="dir">
    <option value="desc" data-i18n="dir_desc"></option>
    <option value="asc" data-i18n="dir_asc"></option>
  </select>
  <select id="arch">
    <option value="all" data-i18n="arch_all"></option>
    <option value="no" data-i18n="arch_no"></option>
    <option value="yes" data-i18n="arch_yes"></option>
  </select>
  <select id="lang">
    <option value="zh">中文</option><option value="en">English</option><option value="ru">Русский</option>
  </select>
  <button class="ibtn" id="theme" title="dark / light">🌙</button>
  <button class="ibtn" id="cfgbtn" data-i18n-title="cfg">⚙</button>
  <span class="hint" id="count"></span>
  <span style="flex:1"></span>
</header>
<main>
  <div id="list"></div>
  <div id="dragbar" data-i18n-title="drag"></div>
  <div id="detail"><div class="hint" style="padding:30px" data-i18n="pick"></div></div>
</main>
<div class="modal" id="setup"><div class="mbox">
  <h3 data-i18n="setup_t"></h3>
  <div class="hint" data-i18n="setup_d"></div>
  <label data-i18n="setup_home"></label>
  <input id="cfg_home" data-i18n-ph="setup_home_ph"/>
  <details style="margin-top:10px"><summary class="hint" data-i18n="setup_adv"></summary>
    <label data-i18n="setup_dbm"></label><input id="cfg_main"/>
    <label data-i18n="setup_dbt"></label><input id="cfg_tasks"/>
  </details>
  <div class="merr" id="cfg_err"></div>
  <div style="margin-top:14px;text-align:right"><button class="abtn" id="cfg_save" data-i18n="setup_save"></button></div>
</div></div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

/* ---------- i18n ---------- */
const I18N={
zh:{title:"ZCode 会话浏览器",search_ph:"搜索标题 / 项目 / 全文…(回车)",
view_project:"🗂 按项目分组",view_time:"⏱ 按时间排序",fold_all:"⊟ 折叠",unfold_all:"⊞ 展开",
dir_desc:"新 → 旧",dir_asc:"旧 → 新",arch_all:"全部",arch_no:"未归档",arch_yes:"已归档",
count:"{n} 个会话",hint:"点会话 ID 复制,可 #sess 引用恢复",drag:"拖拽调整宽度",
pick:"← 从左侧选择一个会话",no_match:"无匹配会话",no_msgs:"此会话没有可见消息",
b_arch:"归档",b_pin:"置顶",b_fork:"fork",b_cli:"CLI",arch:"归档",unarch:"移出归档",sid_l:"会话 ID",
role_u:"用户",role_a:"助手",think_n:"💭 思考过程({n} 字)",in_l:"输入:",out_l:"输出:",
sq_ph:"在当前会话内搜索…",no_res:"无结果",export:"⬇ 导出",
ex_md:"📄 Markdown(直观阅读)",ex_json:"🧩 JSON(完整结构)",ex_jsonl:"📑 JSONL(逐行消息)",
ex_raw:"🗄 RAW(ZCode 原始记录)",fork_from:"fork 自",copied:"已复制 ",
sysrem:"⚠ 系统提醒",err_index:"该会话不在 ZCode 界面索引中(CLI 内部会话),无法归档",
cfg:"配置数据目录",setup_t:"配置 ZCode 数据目录",
setup_d:"未自动找到 ZCode 的数据目录,请手动指定后保存(会记忆,下次不再询问)。",
setup_home:"ZCode 数据目录",setup_home_ph:"例如 ~/.zcode",
setup_adv:"高级:单独指定两个数据库路径",
setup_dbm:"会话内容库(cli/db/db.sqlite)",setup_dbt:"会话索引库(v2/tasks-index.sqlite)",
setup_save:"保存并连接",setup_err:"路径无效,未找到以下文件:"},
en:{title:"ZCode Session Viewer",search_ph:"Search title / project / full text… (Enter)",
view_project:"🗂 By project",view_time:"⏱ By time",fold_all:"⊟ Collapse all",unfold_all:"⊞ Expand all",
dir_desc:"New → Old",dir_asc:"Old → New",arch_all:"All",arch_no:"Unarchived",arch_yes:"Archived",
count:"{n} sessions",hint:"Click a session ID to copy; use #sess to restore",drag:"Drag to resize",
pick:"← Pick a session on the left",no_match:"No matching sessions",no_msgs:"No visible messages in this session",
b_arch:"Archived",b_pin:"Pinned",b_fork:"fork",b_cli:"CLI",arch:"Archive",unarch:"Unarchive",sid_l:"Session ID",
role_u:"User",role_a:"Assistant",think_n:"💭 Thinking ({n} chars)",in_l:"Input:",out_l:"Output:",
sq_ph:"Search within this session…",no_res:"No results",export:"⬇ Export",
ex_md:"📄 Markdown (readable)",ex_json:"🧩 JSON (structured)",ex_jsonl:"📑 JSONL (line per message)",
ex_raw:"🗄 RAW (native ZCode records)",fork_from:"forked from",copied:"Copied ",
sysrem:"⚠ System reminder",err_index:"This session is not in the ZCode UI index (CLI-internal); cannot archive",
cfg:"Configure data directory",setup_t:"Configure ZCode data directory",
setup_d:"ZCode's data directory was not found automatically. Specify it manually and save (remembered for next time).",
setup_home:"ZCode data directory",setup_home_ph:"e.g. ~/.zcode",
setup_adv:"Advanced: set the two database paths individually",
setup_dbm:"Sessions DB (cli/db/db.sqlite)",setup_dbt:"Tasks index DB (v2/tasks-index.sqlite)",
setup_save:"Save & connect",setup_err:"Invalid path, these files were not found:"},
ru:{title:"ZCode Просмотр сессий",search_ph:"Поиск по названию / проекту / тексту… (Enter)",
view_project:"🗂 По проектам",view_time:"⏱ По времени",fold_all:"⊟ Свернуть все",unfold_all:"⊞ Развернуть все",
dir_desc:"Новые → старые",dir_asc:"Старые → новые",arch_all:"Все",arch_no:"Не в архиве",arch_yes:"В архиве",
count:"{n} сессий",hint:"Клик по ID сессии — копировать; #sess для восстановления",drag:"Потяните для изменения ширины",
pick:"← Выберите сессию слева",no_match:"Сессии не найдены",no_msgs:"В этой сессии нет видимых сообщений",
b_arch:"Архив",b_pin:"Закреплено",b_fork:"fork",b_cli:"CLI",arch:"В архив",unarch:"Из архива",sid_l:"ID сессии",
role_u:"Пользователь",role_a:"Ассистент",think_n:"💭 Размышления ({n} симв.)",in_l:"Ввод:",out_l:"Вывод:",
sq_ph:"Поиск внутри сессии…",no_res:"Нет результатов",export:"⬇ Экспорт",
ex_md:"📄 Markdown (для чтения)",ex_json:"🧩 JSON (структурированный)",ex_jsonl:"📑 JSONL (строка на сообщение)",
ex_raw:"🗄 RAW (исходные записи ZCode)",fork_from:"форк от",copied:"Скопировано ",
sysrem:"⚠ Системное напоминание",err_index:"Этой сессии нет в индексе интерфейса ZCode (внутренняя сессия CLI); архивация невозможна",
cfg:"Настроить каталог данных",setup_t:"Настройка каталога данных ZCode",
setup_d:"Каталог данных ZCode не найден автоматически. Укажите его вручную и сохраните (запомнится).",
setup_home:"Каталог данных ZCode",setup_home_ph:"напр. ~/.zcode",
setup_adv:"Дополнительно: пути к двум базам отдельно",
setup_dbm:"БД сессий (cli/db/db.sqlite)",setup_dbt:"Индекс-БД (v2/tasks-index.sqlite)",
setup_save:"Сохранить и подключить",setup_err:"Неверный путь, файлы не найдены:"}};

let LANG=localStorage.getItem("zsv-lang")||"zh";
const LOCALE={zh:"zh-CN",en:"en-US",ru:"ru-RU"};
const t=(k)=>{const d=I18N[LANG]||I18N.zh;return (k in d?d[k]:I18N.zh[k])||k;};
const tf=(k,n)=>t(k).replace("{n}",n);
function applyLang(){
  document.documentElement.lang=LANG;
  document.title=t("title");
  document.querySelectorAll("[data-i18n]").forEach(e=>e.textContent=t(e.dataset.i18n));
  document.querySelectorAll("[data-i18n-ph]").forEach(e=>e.placeholder=t(e.dataset.i18nPh));
  document.querySelectorAll("[data-i18n-title]").forEach(e=>e.title=t(e.dataset.i18nTitle));
}

/* ---------- 主题 ---------- */
let THEME=localStorage.getItem("zsv-theme")||"dark";
document.documentElement.dataset.theme=THEME;
$("#theme").onclick=()=>{THEME=THEME==="dark"?"light":"dark";
  localStorage.setItem("zsv-theme",THEME);document.documentElement.dataset.theme=THEME;
  $("#theme").textContent=THEME==="dark"?"🌙":"☀️";};
$("#theme").textContent=THEME==="dark"?"🌙":"☀️";

const fmt=ms=>ms?new Date(ms).toLocaleString(LOCALE[LANG],{hour12:false}):"—";
const fmtT=ms=>{if(!ms)return"";const d=new Date(ms),n=new Date();
 const tm=d.toTimeString().slice(0,8);
 if(d.toDateString()===n.toDateString())return tm;
 const day=`${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
 return d.getFullYear()===n.getFullYear()?`${day} ${tm.slice(0,5)}`:`${d.getFullYear()}-${day} ${tm.slice(0,5)}`};
const hl=(s,q)=>{if(!q)return esc(s);const i=s.toLowerCase().indexOf(q.toLowerCase());
 if(i<0)return esc(s);return esc(s.slice(0,i))+"<mark>"+esc(s.slice(i,i+q.length))+"</mark>"+esc(s.slice(i+q.length))};

/* 轻量 markdown:先整体 escape,再按块解析 */
function md(src){
  const lines=esc(src).split("\n");let out=[],i=0,inCode=false,code=[],lang="";
  const inline=s=>s
    .replace(/`([^`]+)`/g,(m,c)=>"<code>"+c+"</code>")
    .replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g,"$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank">$1</a>');
  while(i<lines.length){
    const L=lines[i];
    if(L.startsWith("```")){ if(!inCode){inCode=true;lang=L.slice(3).trim();code=[];}
      else{inCode=false;out.push(`<pre><code>${code.join("\n")}</code></pre>`);} i++;continue;}
    if(inCode){code.push(L);i++;continue;}
    if(/^#{1,4}\s/.test(L)){const n=L.match(/^#+/)[0].length;
      out.push(`<h${n}>${inline(L.replace(/^#+\s*/,""))}</h${n}>`);i++;continue;}
    if(/^\s*(-{3,}|\*{3,})\s*$/.test(L)){out.push("<hr>");i++;continue;}
    if(/^\s*&gt;/.test(L)){let b=[];while(i<lines.length&&/^\s*&gt;/.test(lines[i])){b.push(lines[i].replace(/^\s*&gt;\s?/,""));i++;}
      out.push("<blockquote>"+inline(b.join(" "))+"</blockquote>");continue;}
    if(/^\s*\|.*\|\s*$/.test(L)&&i+1<lines.length&&/^\s*\|[\s:|-]+\|\s*$/.test(lines[i+1])){
      const cells=r=>r.trim().replace(/^\||\|$/g,"").split("|").map(c=>inline(c.trim()));
      let html="<table><tr>"+cells(L).map(c=>"<th>"+c+"</th>").join("")+"</tr>";i+=2;
      while(i<lines.length&&/^\s*\|.*\|\s*$/.test(lines[i])){html+="<tr>"+cells(lines[i]).map(c=>"<td>"+c+"</td>").join("")+"</tr>";i++;}
      out.push(html+"</table>");continue;}
    if(/^\s*[-*+]\s+/.test(L)){let b=[];while(i<lines.length&&/^\s*[-*+]\s+/.test(lines[i])){
      b.push("<li>"+inline(lines[i].replace(/^\s*[-*+]\s+/,""))+"</li>");i++;}out.push("<ul>"+b.join("")+"</ul>");continue;}
    if(/^\s*\d+[.)]\s+/.test(L)){let b=[];while(i<lines.length&&/^\s*\d+[.)]\s+/.test(lines[i])){
      b.push("<li>"+inline(lines[i].replace(/^\s*\d+[.)]\s+/,""))+"</li>");i++;}out.push("<ol>"+b.join("")+"</ol>");continue;}
    if(L.trim()===""){i++;continue;}
    let para=[L];i++;
    while(i<lines.length&&lines[i].trim()!==""&&!/^(#{1,4}\s|```|\s*[-*+]\s|\s*\d+[.)]\s|\s*&gt;|\s*\|)/.test(lines[i])){para.push(lines[i]);i++;}
    out.push("<p style='margin:6px 0'>"+inline(para.join("\n"))+"</p>");
  }
  if(inCode)out.push(`<pre><code>${code.join("\n")}</code></pre>`);
  return out.join("");
}

function toolRow(p){
  const ic=p.status==="completed"?'<span class="st-completed">✓</span>'
    :p.status==="error"?'<span class="st-error">✗</span>'
    :p.status==="running"?'<span class="st-running">⟳</span>'
    :'<span class="hint">·</span>';
  return `<details class="tool"><summary>${ic}<span class="tname">${esc(p.tool)}</span>
    <span class="tbrief">${esc(p.brief.slice(0,150))}</span></summary>
    ${p.input&&p.input!=="{}"?`<b class="hint">${t("in_l")}</b><pre>${esc(p.input)}</pre>`:""}
    ${p.output?`<b class="hint">${t("out_l")}</b><pre>${esc(p.output)}</pre>`:""}</details>`;
}
function textPart(txt){
  const sys=txt.match(/<system-reminder>([\s\S]*?)<\/system-reminder>/);
  if(sys&&txt.trim().startsWith("<system-reminder>"))
    return `<div class="sysrem">${t("sysrem")}:${esc(sys[1].trim().slice(0,500))}${sys[1].length>500?"…":""}</div>`;
  return `<div class="md">${md(txt)}</div>`;
}

/* 相邻同角色消息合并为一个气泡,组内边界用淡虚线 */
function groupsHtml(msgs){
  const groups=[];
  msgs.forEach(m=>{const g=groups[groups.length-1];
    if(g&&g.role===m.role)g.items.push(m);else groups.push({role:m.role,items:[m]});});
  return groups.map(g=>{
    const f=g.items[0];
    return `
   <div class="msg ${g.role}">
     <div class="meta"><b>${g.role==="user"?t("role_u"):t("role_a")}</b>${f.model?`<span>${esc(f.model)}</span>`:""}<span>${fmtT(f.time)}</span></div>
     ${g.items.map((m,mi)=>(mi?'<div class="msep"></div>':"")+
       m.parts.map(p=>p.kind==="text"?textPart(p.text)
        :p.kind==="reasoning"?`<details class="think"><summary>${tf("think_n",p.text.length)}</summary><div class="body">${esc(p.text)}</div></details>`
        :toolRow(p)).join("")).join("")}
   </div>`;}).join("");
}

/* ---------- 列表 ---------- */
let collapsed=JSON.parse(localStorage.getItem("zsv-collapsed")||"{}");
let ROWS=[],CUR=null;

function sortRows(rows){
  const d=$("#dir").value==="asc"?1:-1;
  return rows.slice().sort((a,b)=>(b.pinned-a.pinned)||d*(a.time_updated-b.time_updated));
}
function sessHtml(s,qstr){
  const canArch=s.source==="ui";
  return `<div class="sess" data-id="${s.id}">
   <div class="t">${s.pinned?'📌 ':""}${hl(s.title||"("+t("no_msgs")+")",qstr)}${s.archived?`<span class="badge">${t("b_arch")}</span>`:""}${s.pinned?`<span class="badge pin">${t("b_pin")}</span>`:""}${s.parent_id?`<span class="badge fork">${t("b_fork")}</span>`:""}${s.source==="cli"?`<span class="badge cli">${t("b_cli")}</span>`:""}</div>
   ${$("#view").value==="time"?`<div class="m">${hl(s.project,qstr)}</div>`:""}
   <div class="m">${fmt(s.time_updated)} · ${s.msgs}</div>
   ${canArch?`<button class="act" data-arch="${s.archived?0:1}">${s.archived?t("unarch"):t("arch")}</button>`:""}
   </div>`;
}
async function loadList(){
  applyLang();
  const qstr=$("#q").value.trim(),view=$("#view").value,arch=$("#arch").value;
  const grouped=view==="project"&&!qstr;
  $("#foldAll").disabled=$("#unfoldAll").disabled=!grouped;
  const r=await fetch(`/api/sessions?q=${encodeURIComponent(qstr)}&archived=${arch}`);
  ROWS=await r.json();
  $("#count").textContent=tf("count",ROWS.length);
  let html="";
  if(grouped){
    const groups={};
    sortRows(ROWS).forEach(s=>{const g=s.project||"(no project)";(groups[g]=groups[g]||[]).push(s);});
    const sorted=Object.keys(groups).sort((a,b)=>{
      const ma=Math.max(...groups[a].map(x=>x.time_updated)),mb=Math.max(...groups[b].map(x=>x.time_updated));
      return (mb-ma);});
    for(const g of sorted){
      const closed=collapsed[g];
      html+=`<div class="grp ${closed?"closed":""}" data-g="${esc(g)}"><span class="arr">▾</span>📁 ${esc(g)} · ${groups[g].length}</div>`;
      html+=`<div class="gi" data-g="${esc(g)}" ${closed?'style="display:none"':""}>${groups[g].map(s=>sessHtml(s,qstr)).join("")}</div>`;
    }
  }else html=sortRows(ROWS).map(s=>sessHtml(s,qstr)).join("");
  $("#list").innerHTML=html||`<div class="hint" style="padding:20px">${t("no_match")}</div>`;
  document.querySelectorAll(".sess").forEach(el=>{
    el.onclick=()=>openSession(el.dataset.id);
    const b=el.querySelector(".act");
    if(b)b.onclick=e=>{e.stopPropagation();toggleArch(el.dataset.id,b.dataset.arch==="1");};
  });
  document.querySelectorAll(".grp").forEach(el=>el.onclick=()=>{
    const g=el.dataset.g;collapsed[g]=!collapsed[g];
    localStorage.setItem("zsv-collapsed",JSON.stringify(collapsed));
    el.classList.toggle("closed",collapsed[g]);
    document.querySelector(`.gi[data-g="${CSS.escape(g)}"]`).style.display=collapsed[g]?"none":"";
  });
  if(CUR)document.querySelector(`.sess[data-id="${CUR}"]`)?.classList.add("on");
}

async function toggleArch(sid,flag){
  const r=await fetch("/api/archive",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({id:sid,archived:flag})});
  const j=await r.json();
  if(j.error){alert(j.error==="not_in_index"?t("err_index"):j.error);return;}
  await loadList();
  if(CUR===sid)openSession(sid,true);
}

/* ---------- 详情 + 会话内搜索 ---------- */
let hits=[],cur=-1;
function clearHits(){
  hits.forEach(m=>{const p=m.parentNode;if(!p)return;
    p.replaceChild(document.createTextNode(m.textContent),m);p.normalize();});
  hits=[];cur=-1;const h=$("#hitinfo");if(h)h.textContent="";
}
function doSearch(){
  clearHits();cur=-1;
  const qv=$("#sq").value.trim();if(!qv)return;
  const low=qv.toLowerCase();
  const walker=document.createTreeWalker($("#detail"),NodeFilter.SHOW_TEXT,{
    acceptNode:n=>(n.parentNode.closest("#head,script,style")||!n.nodeValue.toLowerCase().includes(low))
      ?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
  const nodes=[];let n;while(n=walker.nextNode())nodes.push(n);
  nodes.forEach(node=>{
    const v=node.nodeValue,l=v.toLowerCase(),frag=document.createDocumentFragment();
    let i=0,j;
    while((j=l.indexOf(low,i))>=0){
      frag.appendChild(document.createTextNode(v.slice(i,j)));
      const m=document.createElement("mark");m.className="hit";m.textContent=v.slice(j,j+qv.length);
      frag.appendChild(m);hits.push(m);i=j+qv.length;}
    frag.appendChild(document.createTextNode(v.slice(i)));
    node.parentNode.replaceChild(frag,node);
  });
  $("#hitinfo").textContent=hits.length?`1/${hits.length}`:t("no_res");
  if(hits.length)gotoHit(0,false);
}
function gotoHit(d,rel=true){
  if(!hits.length)return;
  cur=rel?(cur+d+hits.length)%hits.length:cur;
  hits.forEach((m,i)=>m.classList.toggle("cur",i===cur));
  const m=hits[cur],dt=m.closest("details");if(dt)dt.open=true;
  m.scrollIntoView({block:"center",behavior:"smooth"});
  $("#hitinfo").textContent=`${cur+1}/${hits.length}`;
}

async function openSession(sid,keepScroll=false){
  const r=await fetch("/api/session/"+sid);
  const s=await r.json();
  CUR=sid;
  document.querySelectorAll(".sess.on").forEach(e=>e.classList.remove("on"));
  const li=document.querySelector(`.sess[data-id="${sid}"]`);
  if(li){li.classList.add("on");li.scrollIntoView({block:"nearest"});}
  clearHits();
  $("#detail").innerHTML=`
   <div id="head">
     <div style="display:flex;gap:10px;align-items:center">
       <div style="font-weight:600;font-size:15px;flex:1">${s.pinned?"📌 ":""}${esc(s.title)}${s.archived?` <span class="badge">${t("b_arch")}</span>`:""}</div>
       ${s.source!=="cli"?`<button class="abtn" id="archbtn">${s.archived?t("unarch"):t("arch")}</button>`:""}
     </div>
     <div class="hint">${esc(s.directory||"")}</div>
     <div class="hint">${t("sid_l")}:<span id="sid" title="${t("copied")}">${esc(s.id)}</span>${s.parent_id?" · "+t("fork_from")+" "+esc(s.parent_id):""}</div>
     <div id="srow">🔎 <input id="sq" placeholder="${t("sq_ph")}"/> <span id="hitinfo" class="hint"></span>
       <button class="ibtn" onclick="gotoHit(-1)">▲</button><button class="ibtn" onclick="gotoHit(1)">▼</button>
       <details class="xmenu"><summary class="ibtn">${t("export")}</summary>
         <div class="xdrop">
           <a href="/api/export/${s.id}?fmt=md&lang=${LANG}" download onclick="this.closest('details').open=false">${t("ex_md")}</a>
           <a href="/api/export/${s.id}?fmt=json&lang=${LANG}" download onclick="this.closest('details').open=false">${t("ex_json")}</a>
           <a href="/api/export/${s.id}?fmt=jsonl&lang=${LANG}" download onclick="this.closest('details').open=false">${t("ex_jsonl")}</a>
           <a href="/api/export/${s.id}?fmt=raw&lang=${LANG}" download onclick="this.closest('details').open=false">${t("ex_raw")}</a>
         </div>
       </details></div>
   </div>`+
   groupsHtml(s.messages)||`<div class="hint" style="padding:20px">${t("no_msgs")}</div>`;
  const ab=$("#archbtn");
  if(ab)ab.onclick=()=>toggleArch(s.id,!s.archived);
  $("#sid").onclick=()=>{navigator.clipboard.writeText(s.id);const e=$("#sid");e.textContent=t("copied")+s.id;
    setTimeout(()=>e.textContent=s.id,1200);};
  let deb;$("#sq").oninput=()=>{clearTimeout(deb);
    if(!$("#sq").value.trim()){doSearch();return;}
    deb=setTimeout(doSearch,250)};
  $("#sq").onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();gotoHit(e.shiftKey?-1:1);}};
  if(keepScroll){/* 归档切换后保持阅读位置 */}
  $("#detail").scrollTop=0;
}

/* ---------- 列表宽度拖拽 ---------- */
(()=>{
  const savedW=parseInt(localStorage.getItem("zsv-listw"));
  if(savedW)$("#list").style.width=savedW+"px";
  let drag=false;
  $("#dragbar").addEventListener("mousedown",e=>{
    drag=true;e.preventDefault();
    $("#dragbar").classList.add("dragging");document.body.style.cursor="col-resize";
    document.body.style.userSelect="none";});
  document.addEventListener("mousemove",e=>{
    if(!drag)return;
    const w=Math.min(Math.max(e.clientX,240),window.innerWidth*0.6);
    $("#list").style.width=w+"px";});
  document.addEventListener("mouseup",()=>{
    if(!drag)return;drag=false;
    $("#dragbar").classList.remove("dragging");
    document.body.style.cursor="";document.body.style.userSelect="";
    localStorage.setItem("zsv-listw",parseInt($("#list").style.width)||360);});
})();

$("#q").oninput=()=>{clearTimeout(window._d);window._d=setTimeout(loadList,300)};
$("#view").onchange=loadList;$("#dir").onchange=loadList;$("#arch").onchange=loadList;
async function collapseAll(v){
  new Set(ROWS.map(s=>s.project||"(no project)")).forEach(g=>collapsed[g]=v);
  localStorage.setItem("zsv-collapsed",JSON.stringify(collapsed));
  loadList();
}
$("#foldAll").onclick=()=>collapseAll(true);
$("#unfoldAll").onclick=()=>collapseAll(false);
$("#lang").onchange=()=>{LANG=$("#lang").value;localStorage.setItem("zsv-lang",LANG);
  loadList();if(CUR)openSession(CUR,true);};
$("#lang").value=LANG;
applyLang();

/* ---------- 数据目录配置 ---------- */
async function openSetup(){
  const st=await (await fetch("/api/status")).json();
  $("#cfg_home").value=st.home||"";
  $("#cfg_main").value=st.db_main||"";
  $("#cfg_tasks").value=st.db_tasks||"";
  $("#cfg_err").textContent="";
  $("#setup").classList.add("show");
}
async function saveSetup(){
  const body={home:$("#cfg_home").value,db_main:$("#cfg_main").value,db_tasks:$("#cfg_tasks").value};
  const r=await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)});
  const j=await r.json();
  if(j.ok){$("#setup").classList.remove("show");loadList();return;}
  let msg=t("setup_err");
  if(j.missing&&j.missing.length)msg+="\n"+j.missing.join("\n");
  $("#cfg_err").textContent=msg;
}
$("#cfgbtn").onclick=openSetup;
$("#cfg_save").onclick=saveSetup;

async function boot(){
  const st=await (await fetch("/api/status")).json();
  if(st.configured)loadList();else openSetup();
}
boot();
</script></body></html>""".replace("__ICON__", ICON)


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/favicon.svg":
                return self._send(200, Path(__file__).resolve().parent.parent
                                  .joinpath("assets", "icon.svg").read_text("utf-8"),
                                  "image/svg+xml")
            qs = parse_qs(u.query)
            if u.path.startswith("/api/export/"):
                sid = unquote(u.path[len("/api/export/"):])
                fmt = qs.get("fmt", ["md"])[0]
                if fmt not in ("md", "json", "jsonl", "raw"):
                    fmt = "md"
                lang = qs.get("lang", ["zh"])[0]
                if lang not in EXPORT_I18N:
                    lang = "zh"
                body, ext, mime, title = export_session(sid, fmt, lang)
                if body is None:
                    return self._send(404, '{"error":"not found"}')
                safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", title).strip()[:40] or "session"
                ascii_fn = sid[:24] + "_" + fmt + ext
                utf8_fn = quote(f"{safe}_{fmt}{ext}")
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{ascii_fn}"; filename*=UTF-8\'\'{utf8_fn}',
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if u.path == "/api/status":
                cfg = load_config()
                configured = bool(
                    DB_MAIN and DB_TASKS
                    and Path(DB_MAIN).exists() and Path(DB_TASKS).exists()
                )
                return self._send(200, json.dumps(
                    {"configured": configured, "home": cfg.get("home", ""),
                     "db_main": DB_MAIN or "", "db_tasks": DB_TASKS or ""},
                    ensure_ascii=False))
            if u.path == "/api/sessions":
                if not (DB_MAIN and Path(DB_MAIN).exists()):
                    return self._send(200, "[]")
                rows = list_sessions(qs.get("q", [""])[0], qs.get("archived", ["all"])[0])
                return self._send(200, json.dumps(rows, ensure_ascii=False))
            if u.path.startswith("/api/session/"):
                s = get_session(unquote(u.path[len("/api/session/"):]))
                if s is None:
                    return self._send(404, '{"error":"not found"}')
                return self._send(200, json.dumps(s, ensure_ascii=False))
            if u.path == "/api/search":
                qv = qs.get("q", [""])[0]
                like = "%" + qv.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                rows = q(
                    DB_MAIN,
                    """SELECT m.session_id AS sid, s.title,
                              substr(json_extract(p.data,'$.text'),1,180) AS snippet, m.time_created
                       FROM part p JOIN message m ON p.message_id = m.id
                       JOIN session s ON s.id = m.session_id
                       WHERE json_extract(p.data,'$.type')='text'
                         AND json_extract(p.data,'$.text') LIKE ? ESCAPE '\\'
                       ORDER BY m.time_created DESC LIMIT 60""",
                    (like,),
                )
                return self._send(200, json.dumps(rows, ensure_ascii=False))
            self._send(404, '{"error":"not found"}')
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))

    def do_POST(self):
        global DB_MAIN, DB_TASKS
        u = urlparse(self.path)
        try:
            if u.path == "/api/archive":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                sid, flag = body.get("id"), bool(body.get("archived"))
                if not sid:
                    return self._send(400, '{"error":"missing id"}')
                n = set_archived(sid, flag)
                if n == 0:
                    return self._send(404, '{"error":"not_in_index"}')
                return self._send(200, json.dumps({"ok": True, "archived": flag}))
            if u.path == "/api/config":
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                home = (body.get("home") or "").strip()
                dbm = (body.get("db_main") or "").strip()
                dbt = (body.get("db_tasks") or "").strip()
                if dbm and dbt:
                    cand_main, cand_tasks = Path(dbm).expanduser(), Path(dbt).expanduser()
                elif home:
                    h = Path(home).expanduser()
                    cand_main = h / "cli" / "db" / "db.sqlite"
                    cand_tasks = h / "v2" / "tasks-index.sqlite"
                else:
                    return self._send(400, '{"error":"empty"}')
                miss = [str(p) for p in (cand_main, cand_tasks) if not p.exists()]
                if miss:
                    return self._send(200, json.dumps(
                        {"ok": False, "error": "invalid", "missing": miss},
                        ensure_ascii=False))
                DB_MAIN, DB_TASKS = str(cand_main), str(cand_tasks)
                save_config({"home": home, "db_main": DB_MAIN, "db_tasks": DB_TASKS})
                return self._send(200, '{"ok": true}')
            self._send(404, '{"error":"not found"}')
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))


def main():
    global DB_MAIN, DB_TASKS
    ap = argparse.ArgumentParser(description="ZCode Session Viewer")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-open", action="store_true", help="do not open browser on start")
    ap.add_argument("--db-main", default=None, help="override sessions DB path")
    ap.add_argument("--db-tasks", default=None, help="override tasks index DB path")
    a = ap.parse_args()

    home = detect_zcode_home()
    cfg = load_config()
    if a.db_main:
        DB_MAIN = a.db_main
    elif cfg.get("db_main"):
        DB_MAIN = cfg["db_main"]
    elif home:
        DB_MAIN = str(home / "cli" / "db" / "db.sqlite")
    if a.db_tasks:
        DB_TASKS = a.db_tasks
    elif cfg.get("db_tasks"):
        DB_TASKS = cfg["db_tasks"]
    elif home:
        DB_TASKS = str(home / "v2" / "tasks-index.sqlite")

    if not (DB_MAIN and DB_TASKS and Path(DB_MAIN).exists() and Path(DB_TASKS).exists()):
        print("ZCode databases were not found automatically.")
        print("The web page will open a setup dialog — enter your ZCode data "
              "directory there (saved to " + str(CONFIG_FILE) + ").")
        print("Searched automatically in: $ZCODE_HOME, ~/.zcode, ~/.config/zcode, "
              "~/Library/Application Support/ZCode")
    else:
        print(f"Data: {DB_MAIN}")
        print(f"      {DB_TASKS}")
    url = f"http://127.0.0.1:{a.port}"
    print(f"ZCode Session Viewer: {url}  (Ctrl+C to exit)")
    if not a.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
