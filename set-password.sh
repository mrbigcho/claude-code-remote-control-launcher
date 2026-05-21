#!/usr/bin/env bash
# Change the Basic Auth password for the launcher (prompts privately; the
# password never appears in your shell history or on screen).
# Usage: bash set-password.sh <username>
set -euo pipefail

CADDYFILE=/etc/caddy/Caddyfile
USER_NAME="${1:?usage: set-password.sh <username>}"

read -rsp "New password for '$USER_NAME': " PW; echo
read -rsp "Confirm: " PW2; echo
[ "$PW" = "$PW2" ] || { echo "passwords don't match" >&2; exit 1; }
[ -n "$PW" ]        || { echo "empty password" >&2; exit 1; }

HASH=$(/usr/local/bin/caddy hash-password --plaintext "$PW")
TMP=$(mktemp /tmp/Caddyfile.XXXXXX)
sudo cat "$CADDYFILE" > "$TMP"

python3 - "$USER_NAME" "$HASH" "$TMP" <<'PY'
import sys, re
user, h, path = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(path).read()
s, n = re.subn(rf'(\n\s*{re.escape(user)} )\S+', lambda m: m.group(1) + h, s)
assert n == 1, f"expected exactly one '{user}' line in Caddyfile, found {n}"
open(path, "w").write(s)
PY

# validate as a Caddyfile (not JSON) before applying
/usr/local/bin/caddy validate --adapter caddyfile --config "$TMP" >/dev/null
sudo cp "$TMP" "$CADDYFILE"
rm -f "$TMP"
sudo systemctl reload caddy
echo "✓ password updated for '$USER_NAME' and Caddy reloaded."
