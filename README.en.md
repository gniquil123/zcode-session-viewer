<p align="center"><a href="./README.md">简体中文</a> | <b>English</b> | <a href="./README.ru.md">Русский</a></p>

# ZCode Session Viewer

A **dependency-free** local web tool to browse, search, and manage all [ZCode](https://z.ai) session history (including archived sessions).

> The ZCode desktop app only shows the session list of the current project and makes it hard to search past conversations in depth. This tool reads ZCode's local databases directly and, in your browser, offers cross-project browsing, full-text search, archive management, and in-session search.

## Features

- **Full session list**: merges the desktop UI index with the session database so you can browse every session across projects; badges for archived / pinned / fork / CLI-only (internal subagent sessions)
- **Two list views**: `🗂 by project` (groups collapsible, collapse-all / expand-all, state remembered) and `⏱ by time` (ascending / descending)
- **Archive management**: one-click archive / unarchive — writes the very same field the ZCode UI reads, so both stay in sync
- **Global search**: title / project / session ID / full-text content (millisecond SQLite LIKE)
- **In-session search**: highlighted hits, hit counter, ▲▼ / Enter to cycle, auto-expands collapsed tool/thinking blocks
- **ZCode-style reading experience**: markdown rendering (headings / code blocks / tables / lists), tool-call rows (status dot + argument summary, expand for full input/output), collapsible thinking, de-emphasized system-injected content
- **Dark / light theme · multilingual UI**: toggle with 🌙/☀️; UI available in 中文 (default), English, Русский; exported Markdown labels follow the UI language; all preferences remembered locally
- **Session export**: one-click export of a single session, filename carries the format tag (`title_md.md` / `title_json.json` / `title_jsonl.jsonl` / `title_raw.jsonl`) — `Markdown` (human-readable, tool calls/thinking as collapsible blocks), `JSON` (full structured data), `JSONL` (one message per line, stream-friendly), `RAW` (the native records ZCode writes to its database — session/message/part rows verbatim, zero filtering, zero truncation)
- **Adjustable layout**: drag to resize the session pane (remembered)
- **One-click copy of the session ID**: paste it in ZCode as `#sess_xxx` to restore context
- The session currently open is highlighted in the list and auto-scrolled into view

## Quick start

```bash
python src/viewer.py
# opens http://127.0.0.1:8787 automatically
```

Requires Python 3.8+ (standard library only — no third-party packages, nothing to build or install).

```bash
python src/viewer.py --port 9000        # different port
python src/viewer.py --no-open          # don't auto-open the browser
python src/viewer.py --db-main PATH --db-tasks PATH   # set database paths manually
```

## Data sources & privacy

The tool runs **entirely locally** — it makes no external network requests and uploads nothing.

| Database (auto-detected, see below) | Purpose | Access |
| --- | --- | --- |
| `<data dir>/cli/db/db.sqlite` | session content (message / part) | read-only |
| `<data dir>/v2/tasks-index.sqlite` | session-list index (archived / pinned / project) | read-only + archive flag write |

The only write operation is archive / unarchive (`UPDATE tasks SET archived = ...`), targeting the same field the ZCode UI reads, so changes made here show up in the ZCode app once its session list is re-opened.

**Data-directory auto-detection** (in order): `ZCODE_HOME` env var → `~/.zcode` (default layout on all platforms) → `~/.config/zcode` → `~/Library/Application Support/ZCode` (macOS fallback). If none is found, **the page pops up a setup dialog** — enter the data directory (or set the two database paths individually under Advanced) and save. The choice is remembered in `~/.zcode-session-viewer.json`, so you won't be asked again; the ⚙ button in the header reopens it anytime. Command-line `--db-main` / `--db-tasks` take top priority.

### Platform support

The code uses only the Python standard library (pathlib / sqlite3 / http.server) with no platform-specific APIs, so it runs on Windows / macOS / Linux alike. Verified on Windows; macOS and Linux are expected to work out of the box (same home-directory layout) — if your ZCode installation keeps data elsewhere, just pass the launch arguments. Issue reports from those platforms are welcome.

## Project layout

```
zcode-session-viewer/
├── src/
│   └── viewer.py      # all source code (single file)
├── assets/
│   └── icon.svg       # site icon source (same SVG is inlined in the page)
├── README.md          # 简体中文 docs (default)
├── README.en.md       # English docs
├── README.ru.md       # Русская документация
├── LICENSE            # MIT
└── .gitignore
```

No build step; no dependency manifest — because there are zero dependencies.

## Known limitations

- Archiving waits up to 3 s on ZCode's own lock (database-level) when writing the same row concurrently; on failure you get an error message and no data is damaged
- The ZCode app caches its session list in memory: after archiving here, re-open the list in ZCode to see the change
- "CLI-only" sessions are absent from the ZCode UI index, so they get no archive button

## License

[MIT](./LICENSE)

## Disclaimer

This is an **unofficial** community tool, not affiliated with or endorsed by Z.ai. It only reads the session databases that ZCode already stores on **your own machine**, plus a single archive-flag write to the tasks index. The authors accept no responsibility for any data loss — consider backing up your ZCode data directory before first use. ZCode and related names belong to their respective owners and are used here for identification only.
