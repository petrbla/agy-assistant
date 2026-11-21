#!/usr/bin/env bash
set -euo pipefail

ASISTENT_HOME="/home/asistent"
WORKSPACE_DIR="${WORKSPACE_DIR:-$ASISTENT_HOME/workspace}"
DATA_DIR="${DATA_DIR:-$ASISTENT_HOME/data}"
TEMPLATE_DIR="/app/workspace_template"
JMENO_VALUE="${JMENO:-${ASSISTANT_NAME:-}}"

mkdir -p "$WORKSPACE_DIR" "$DATA_DIR" "$ASISTENT_HOME/secrets" \
    "$ASISTENT_HOME/.gemini/antigravity-cli" "$ASISTENT_HOME/.gemini/config"

# Nakopíruje šablony workspace, pokud ještě v persistentním volume neexistují
if [ -d "$TEMPLATE_DIR" ]; then
    cp -rn "$TEMPLATE_DIR/." "$WORKSPACE_DIR/" 2>/dev/null || true
    mkdir -p "$WORKSPACE_DIR/memory" "$WORKSPACE_DIR/.agents/skills"
    if [ -f "$WORKSPACE_DIR/SOUL.md" ] && [ -n "$JMENO_VALUE" ] && [ "$JMENO_VALUE" != "Asistent" ] && [ "$JMENO_VALUE" != "Jarvis" ]; then
        JMENO_ENV="$JMENO_VALUE" SOUL_FILE="$WORKSPACE_DIR/SOUL.md" python3 - <<'PY'
import os, re
from pathlib import Path

p = Path(os.environ["SOUL_FILE"])
name = os.environ["JMENO_ENV"].strip()
txt = p.read_text(encoding="utf-8")
txt = txt.replace("{{JMENO}}", name)
txt = re.sub(
    r"^(\s*(?:[-*]\s*)?\*\*Jméno:\*\*)\s*\[DOPLNIT:.*\]\s*$",
    rf"\1 {name}",
    txt,
    flags=re.MULTILINE,
)
p.write_text(txt, encoding="utf-8")
PY
    fi
fi

# Inicializace settings.json pro agy CLI, pokud ještě neexistuje
SETTINGS_FILE="$ASISTENT_HOME/.gemini/antigravity-cli/settings.json"
if [ ! -f "$SETTINGS_FILE" ]; then
    if [ -n "${GEMINI_API_KEY:-}" ]; then
        cat > "$SETTINGS_FILE" <<'EOF'
{
  "modelProvider": "gemini",
  "agentMode": "auto"
}
EOF
    else
        cat > "$SETTINGS_FILE" <<'EOF'
{
  "agentMode": "auto"
}
EOF
    fi
fi

# Volitelné vygenerování výchozího mcp_config.json z env proměnných (pokud ještě neexistuje)
MCP_FILE="$ASISTENT_HOME/.gemini/config/mcp_config.json"
if [ ! -f "$MCP_FILE" ]; then
    python3 - <<'PY'
import json, os
from pathlib import Path

servers = {}

def add_http(name: str, url_env: str, token_env: str):
    url = os.environ.get(url_env, "").strip()
    if not url:
        return
    entry = {"url": url}
    token = os.environ.get(token_env, "").strip()
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    servers[name] = entry

add_http("n8n-prace", "MCP_PRACE_N8N_URL", "MCP_PRACE_N8N_TOKEN")
add_http("nocodb-prace", "MCP_PRACE_NOCODB_URL", "MCP_PRACE_NOCODB_TOKEN")
add_http("n8n-osobni", "MCP_OSOBNI_N8N_URL", "MCP_OSOBNI_N8N_TOKEN")
add_http("nocodb-osobni", "MCP_OSOBNI_NOCODB_URL", "MCP_OSOBNI_NOCODB_TOKEN")
add_http("google-osobni", "MCP_OSOBNI_GOOGLE_URL", "MCP_OSOBNI_GOOGLE_TOKEN")

target = Path("/home/asistent/.gemini/config/mcp_config.json")
target.write_text(json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PY
fi

# Jednorázové sjednocení vlastnictví pro čistý single-user běh pod `asistent`
chown -R asistent:asistent "$ASISTENT_HOME" 2>/dev/null || true

exec gosu asistent "$@"
