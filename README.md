# cc-launcher — Claude Remote Control launcher

A tiny self-hosted web page to start a [Claude Code **Remote Control**](https://code.claude.com/docs/en/remote-control)
session for a chosen workspace, so you can pick it up from the Claude mobile app
(**Code** tab) or any browser.

Open the page → pick a git workspace → **Start session**. The backend starts
`claude remote-control` in that directory inside a detached `tmux` session and
shows the `claude.ai/code` session link. Stop ends the session cleanly.

> ⚠️ **Security:** each launch starts a Claude agent with **full local access**
> (your files, shell, tools) running as your user. Put it behind authentication
> (the reference setup uses a Caddy reverse proxy with Basic Auth) and HTTPS.
> Never expose it unauthenticated.

## How it works

- Single-file **Python stdlib** HTTP service (`app.py`), no dependencies. Binds
  `127.0.0.1:8900`; a reverse proxy terminates TLS + auth in front of it.
- Launch flow per workspace:
  1. Pre-trust the directory (`hasTrustDialogAccepted` in `~/.claude.json`) —
     remote-control refuses untrusted workspaces.
  2. Clear any stale `bridge-pointer.json` so a **fresh live session** is created
     (otherwise remote-control resurrects the previous, now-archived session).
  3. Run `claude remote-control --name cc-<slug> --spawn <mode>` in a detached
     `tmux` session (shared user socket → attach with `tmux attach -t cc-<slug>`).
  4. Scrape the `https://claude.ai/code/...` URL from the pane.
- **Stop** sends `Ctrl+C` (SIGINT) so remote-control deregisters the worker
  server-side, with a force-kill fallback. (Removing the stopped entry from the
  Claude app list is an app-side action — there is no documented box-side delete.)
- Default spawn mode is `session` (one session, immediately visible in the app);
  the "worktree" checkbox uses `--spawn worktree` for isolated on-demand sessions.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/` | Web UI |
| GET  | `/api/state` | discovered workspaces + running sessions (JSON) |
| POST | `/launch` `{path, mode}` | start a session (path validated against discovered set) |
| POST | `/stop` `{name}` | stop a session |
| GET  | `/health` | liveness |

## Requirements

- `claude` CLI **v2.1.51+**, authenticated with a **claude.ai subscription**
  (OAuth via `claude` → `/login`; API keys / `setup-token` are not supported by
  Remote Control). No `ANTHROPIC_API_KEY` in the environment.
- `tmux`, Python 3, and a reverse proxy (Caddy/nginx) for TLS + auth.

## Setup

1. Edit `SCAN_ROOTS` / `PORT` in `app.py` if needed (workspaces are git repos
   found under the scan roots).
2. Install the service (runs as your user; no `PrivateTmp` so tmux sockets are
   shared):
   ```bash
   sudo cp cc-launcher.service /etc/systemd/system/
   sudo systemctl enable --now cc-launcher
   ```
3. Put it behind a reverse proxy with auth + TLS. Example Caddy block:
   ```caddyfile
   cc.example.com {
       basic_auth {
           youruser <bcrypt-hash from `caddy hash-password`>
       }
       reverse_proxy 127.0.0.1:8900
   }
   ```
4. Rotate the Basic Auth password any time with `./set-password.sh [username]`
   (prompts privately, updates the Caddyfile, reloads Caddy).

## License

MIT
