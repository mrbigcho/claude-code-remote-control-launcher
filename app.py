#!/usr/bin/env python3
"""Claude Remote Control launcher.

A tiny stdlib HTTP service: pick a workspace, it starts `claude remote-control`
(server mode) in that directory inside a detached tmux session, scrapes the
claude.ai/code session URL, and shows it so you can pick the session up on your
phone (Claude app -> Code) or any browser.

Security: binds 127.0.0.1 only; expects Caddy in front with Basic Auth + TLS.
Launches are restricted to auto-discovered git workspaces (no client-supplied
paths are executed). Each launch starts a full-access Claude agent on this box.
"""
from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

# ---- config ----------------------------------------------------------------
PORT = 8900
HOST = "127.0.0.1"
HOME = Path.home()
CLAUDE = HOME / ".local/bin/claude"
SCAN_ROOTS = [HOME / "stacks/services", HOME / "projects", HOME / "src", HOME / "code"]
MAX_DEPTH = 2  # how deep under each scan root to look for git repos
EXCLUDE_PARTS = {".git", ".venv", "node_modules", ".cache", "__pycache__", ".local"}
TMUX_PREFIX = "cc-"
URL_RE = re.compile(r"https://claude\.ai/code(?:\?environment=env_|/session_)[A-Za-z0-9_]+")
LAUNCH_WAIT_S = 22  # how long to poll the pane for the session URL


# ---- workspace discovery ---------------------------------------------------
def discover_workspaces() -> list[dict]:
    found: dict[str, dict] = {}

    def scan(root: Path, depth: int) -> None:
        if depth > MAX_DEPTH or not root.is_dir():
            return
        if (root / ".git").is_dir():
            found[str(root)] = {"path": str(root), "name": root.name}
            return  # don't descend into a repo
        try:
            for child in sorted(root.iterdir()):
                if child.is_dir() and child.name not in EXCLUDE_PARTS and not child.name.startswith("."):
                    scan(child, depth + 1)
        except PermissionError:
            pass

    for r in SCAN_ROOTS:
        scan(r, 0)
    return sorted(found.values(), key=lambda w: w["name"].lower())


def allowed_paths() -> set[str]:
    return {w["path"] for w in discover_workspaces()}


# ---- tmux helpers ----------------------------------------------------------
def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def session_for(path: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", Path(path).name)[:40]
    return f"{TMUX_PREFIX}{slug}"


def has_session(name: str) -> bool:
    return _tmux("has-session", "-t", name).returncode == 0


def capture(name: str) -> str:
    return _tmux("capture-pane", "-p", "-t", name).stdout


def scrape_url(name: str) -> str | None:
    m = URL_RE.search(capture(name))
    return m.group(0) if m else None


def running_sessions() -> list[dict]:
    out = _tmux("list-sessions", "-F", "#{session_name}").stdout
    names = [n for n in out.splitlines() if n.startswith(TMUX_PREFIX)]
    return [{"name": n, "url": scrape_url(n)} for n in sorted(names)]


# ---- trust + launch --------------------------------------------------------
def pretrust(path: str) -> None:
    """Set hasTrustDialogAccepted=true for `path` in ~/.claude.json (atomic).

    `claude remote-control` refuses to start in an untrusted workspace; these
    are the user's own repos selected via the UI, so we accept trust for them.
    """
    cfg = HOME / ".claude.json"
    data = json.loads(cfg.read_text())
    entry = data.setdefault("projects", {}).setdefault(path, {})
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("allowedTools", [])
    entry.setdefault("projectOnboardingSeenCount", 1)
    tmp = cfg.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, cfg)


def clear_bridge_pointer(path: str) -> None:
    """Drop the cached remote-control environment pointer for this dir.

    remote-control writes ~/.claude/projects/<enc>/bridge-pointer.json and on the
    next start REUSES that environment — which resurfaces the previous (now
    archived/dead) session instead of creating a fresh live one. We only get here
    when no local tmux session is running, so the pointer is always stale; remove
    it so a brand-new environment + live session is created.
    """
    enc = path.replace("/", "-")
    ptr = HOME / ".claude" / "projects" / enc / "bridge-pointer.json"
    try:
        ptr.unlink()
    except FileNotFoundError:
        pass


def launch(path: str, mode: str) -> dict:
    name = session_for(path)
    if has_session(name):
        return {"name": name, "url": scrape_url(name), "status": "already-running"}

    pretrust(path)
    clear_bridge_pointer(path)
    cmd = f"exec {CLAUDE} remote-control --name {name} --spawn {mode}"
    _tmux("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", path)
    _tmux("send-keys", "-t", name, cmd, "C-m")

    deadline = time.time() + LAUNCH_WAIT_S
    answered = False
    while time.time() < deadline:
        time.sleep(1)
        if not has_session(name):
            return {"name": name, "url": None, "status": "exited"}
        pane = capture(name)
        if "not trusted" in pane.lower():
            _tmux("kill-session", "-t", name)
            return {"name": name, "url": None, "status": "trust-error"}
        if not answered and "Enable Remote Control?" in pane:
            _tmux("send-keys", "-t", name, "y", "C-m")  # one-time consent fallback
            answered = True
            continue
        m = URL_RE.search(pane)
        if m:
            return {"name": name, "url": m.group(0), "status": "running"}
    return {"name": name, "url": scrape_url(name), "status": "timeout"}


def stop(name: str) -> bool:
    if not name.startswith(TMUX_PREFIX):
        return False
    if not has_session(name):
        return True
    # Graceful: send Ctrl+C (SIGINT) so remote-control deregisters the session
    # server-side (POST /work/<id>/stop) instead of lingering in the Claude app.
    # `exec claude …` means the pane ends — and the tmux session closes — once
    # claude exits. Fall back to a force kill if it doesn't wind down.
    for _ in range(2):
        _tmux("send-keys", "-t", name, "C-c")
        for _ in range(12):
            if not has_session(name):
                return True
            time.sleep(0.5)
    _tmux("kill-session", "-t", name)
    return not has_session(name)


# ---- HTML ------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<title>Claude Remote Control</title>
<style>
 :root{{color-scheme:light dark;
  --bg:#fff;--fg:#1a1a1a;--muted:#666;--faint:#888;--border:#ddd;--card:#fff;
  --btn-bg:#f6f6f6;--btn-bd:#bbb;--primary-bg:#1a1a1a;--primary-fg:#fff;--accent:#2563eb;--code:rgba(0,0,0,.06);--hr:#eee}}
 @media (prefers-color-scheme:dark){{:root{{
  --bg:#16181c;--fg:#e9e9ea;--muted:#a0a6ad;--faint:#7e858e;--border:#32363c;--card:#1d2024;
  --btn-bg:#282c32;--btn-bd:#444a52;--primary-bg:#e9e9ea;--primary-fg:#16181c;--accent:#4b90f6;--code:rgba(255,255,255,.10);--hr:#2a2e34}}}}
 body{{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:680px;margin:32px auto;padding:0 18px;line-height:1.5;color:var(--fg);background:var(--bg)}}
 h1{{font-size:1.3rem;margin:0 0 4px}} .sub{{color:var(--muted);font-size:.85rem;margin:0 0 20px}}
 .ws{{border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin:10px 0;display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:var(--card)}}
 .ws .meta{{flex:1;min-width:160px}} .ws .name{{font-weight:600}} .ws .path{{color:var(--faint);font-size:.78rem;word-break:break-all}}
 .dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;margin-right:5px}}
 button{{font:inherit;padding:7px 14px;border-radius:8px;border:1px solid var(--btn-bd);background:var(--btn-bg);color:var(--fg);cursor:pointer}}
 button.primary{{background:var(--primary-bg);color:var(--primary-fg);border-color:var(--primary-bg)}} button:disabled{{opacity:.5;cursor:wait}}
 a.open{{padding:7px 14px;border-radius:8px;background:var(--accent);color:#fff;text-decoration:none;font-size:.9rem}}
 label.iso{{font-size:.78rem;color:var(--muted);display:flex;align-items:center;gap:4px}}
 .empty{{color:var(--faint)}} .note{{font-size:.8rem;color:var(--muted);margin-top:24px;border-top:1px solid var(--hr);padding-top:12px}}
 code{{background:var(--code);padding:1px 5px;border-radius:4px;font-size:.85em}}
</style></head><body>
<h1>Claude Remote Control</h1>
<p class="sub">워크스페이스를 골라 세션을 띄우면, 폰의 Claude 앱 <b>Code</b> 탭(또는 브라우저)에서 이어서 작업할 수 있어요.</p>
<div id="list">{rows}</div>
<p class="note">세션은 이 박스에서 <b>전체 권한</b>으로 실행됩니다(로컬 파일·도구 접근). 폰 연결: Claude 앱 → <b>Code</b> → 같은 계정으로 로그인 → 세션 이름으로 접속. 터미널에서 직접 보려면 <code>tmux attach -t cc-&lt;name&gt;</code>.</p>
<script>
async function post(u,b){{const r=await fetch(u,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});return r.json()}}
async function launch(btn,path){{btn.disabled=true;btn.textContent='띄우는 중…';
 const iso=btn.closest('.ws').querySelector('.iso input').checked;
 const r=await post('/launch',{{path,mode:iso?'worktree':'session'}});
 location.reload()}}
async function stop(btn,name){{btn.disabled=true;btn.textContent='중지 중…';await post('/stop',{{name}});location.reload()}}
</script>
</body></html>"""


def render() -> str:
    ws = discover_workspaces()
    running = {s["name"]: s for s in running_sessions()}
    rows = []
    for w in ws:
        name = session_for(w["path"])
        sess = running.get(name)
        meta = (f'<div class="meta"><div class="name">{html.escape(w["name"])}</div>'
                f'<div class="path">{html.escape(w["path"])}</div></div>')
        if sess:
            url = sess["url"] or "https://claude.ai/code"
            ctl = (f'<a class="open" href="{html.escape(url)}" target="_blank" rel="noopener">열기</a>'
                   f'<button onclick="stop(this,\'{html.escape(name)}\')">중지</button>'
                   f'<span class="path"><span class="dot"></span>running</span>')
        else:
            ctl = (f'<label class="iso"><input type="checkbox">worktree 격리</label>'
                   f'<button class="primary" onclick="launch(this,\'{html.escape(w["path"])}\')">세션 시작</button>')
        rows.append(f'<div class="ws">{meta}{ctl}</div>')
    if not rows:
        rows.append('<p class="empty">스캔된 워크스페이스가 없어요. ' + html.escape(", ".join(str(r) for r in SCAN_ROOTS)) + ' 아래에 git repo를 두세요.</p>')
    return PAGE.format(rows="\n".join(rows))


# ---- HTTP ------------------------------------------------------------------
FAVICON = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#d97706"/><text x="16" y="22" font-size="18" text-anchor="middle" fill="#fff" font-family="sans-serif">C</text></svg>'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quieter logs
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, render(), "text/html; charset=utf-8")
        elif self.path == "/favicon.svg":
            self._send(200, FAVICON, "image/svg+xml")
        elif self.path == "/api/state":
            self._send(200, json.dumps({"workspaces": discover_workspaces(),
                                        "running": running_sessions()}))
        elif self.path == "/health":
            self._send(200, json.dumps({"ok": True}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        try:
            body = self._json_body()
            if self.path == "/launch":
                path = body.get("path", "")
                if path not in allowed_paths():
                    return self._send(403, json.dumps({"error": "workspace not allowed"}))
                mode = body.get("mode", "session")
                if mode not in ("same-dir", "worktree", "session"):
                    mode = "same-dir"
                return self._send(200, json.dumps(launch(path, mode)))
            if self.path == "/stop":
                ok = stop(body.get("name", ""))
                return self._send(200, json.dumps({"ok": ok}))
            self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(e)}))


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
