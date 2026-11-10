#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

ENV_DIR="$ROOT_DIR/env"
MAIN_ENV="$ENV_DIR/.env"
MAIN_EXAMPLE="$ENV_DIR/.env.example"
CURLEC_ENV="$ENV_DIR/curlec.env"
CURLEC_EXAMPLE="$ENV_DIR/curlec.env.example"

FORCE_CONFIGURE=false
START_SERVER=true
FOREGROUND=false

for arg in "$@"; do
  case "$arg" in
    --configure|--reconfigure)
      FORCE_CONFIGURE=true
      ;;
    --no-start|--setup-only)
      START_SERVER=false
      ;;
    --foreground)
      FOREGROUND=true
      ;;
  esac
done

mkdir -p "$ENV_DIR"

configure_env_file() {
  local target_file="$1"
  local example_file="$2"
  local label="$3"

  if [[ ! -f "$example_file" ]]; then
    echo "Error: Missing template $example_file" >&2
    exit 1
  fi

  if [[ -f "$target_file" && "$FORCE_CONFIGURE" == "false" ]]; then
    echo "[✓] Found existing $label ($target_file)"
    return
  fi

  if [[ ! -t 0 ]]; then
    cp "$example_file" "$target_file"
    chmod 600 "$target_file" 2>/dev/null || true
    echo "[✓] Created $label from $example_file (non-interactive mode)"
    return
  fi

  echo ""
  echo "============================================================"
  echo " Configuring $label"
  echo " Press Enter to accept the default/current value in [brackets]"
  echo "============================================================"

  declare -A existing_values=()
  if [[ -f "$target_file" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line%$'\r'}"
      [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
      if [[ "$line" == *"="* ]]; then
        local k="${line%%=*}"
        local v="${line#*=}"
        v="${v%%#*}"
        v="$(echo "$v" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        existing_values["$k"]="$v"
      fi
    done < "$target_file"
  fi

  local tmp_file="${target_file}.tmp"
  : > "$tmp_file"

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    if [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]]; then
      echo "$line" >> "$tmp_file"
      continue
    fi

    if [[ "$line" == *"="* ]]; then
      local key="${line%%=*}"
      local raw_val="${line#*=}"
      local hint=""
      local default_val="$raw_val"

      if [[ "$raw_val" == *"#"* ]]; then
        default_val="${raw_val%%#*}"
        hint="${raw_val#*#}"
        hint="$(echo "$hint" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      fi
      default_val="$(echo "$default_val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

      if [[ -n "${existing_values[$key]+_}" ]]; then
        default_val="${existing_values[$key]}"
      fi

      local prompt_text="  $key"
      if [[ -n "$hint" ]]; then
        prompt_text="$prompt_text ($hint)"
      fi
      prompt_text="$prompt_text [$default_val]: "

      local user_input=""
      read -r -p "$prompt_text" user_input </dev/tty || true
      if [[ -z "$user_input" ]]; then
        user_input="$default_val"
      fi

      echo "${key}=${user_input}" >> "$tmp_file"
    else
      echo "$line" >> "$tmp_file"
    fi
  done < "$example_file"

  mv "$tmp_file" "$target_file"
  chmod 600 "$target_file" 2>/dev/null || true
  echo "[✓] Saved $target_file"
}

configure_env_file "$MAIN_ENV" "$MAIN_EXAMPLE" "App & AI Environment (env/.env)"
configure_env_file "$CURLEC_ENV" "$CURLEC_EXAMPLE" "Curlec Payment Environment (env/curlec.env)"

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Error: Python 3 is not installed. Please install python3 and python3-venv." >&2
  exit 1
fi

if [[ ! -d "$ROOT_DIR/venv" ]]; then
  echo "[*] Creating Python virtual environment in ./venv ..."
  "$PYTHON_BIN" -m venv "$ROOT_DIR/venv"
fi

# Activate virtualenv (supports Linux/macOS and Git Bash on Windows)
if [[ -f "$ROOT_DIR/venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/venv/bin/activate"
elif [[ -f "$ROOT_DIR/venv/Scripts/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/venv/Scripts/activate"
fi

echo "[*] Installing Python dependencies from requirements.txt ..."
pip install --upgrade pip >/dev/null
pip install -r "$ROOT_DIR/requirements.txt"

if [[ "$START_SERVER" == "false" ]]; then
  echo "[✓] Setup complete. Run 'bash install.sh' to start the server."
  exit 0
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

if [[ "$FOREGROUND" == "false" ]] && command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  SUDO=""
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      SUDO="sudo"
    fi
  fi

  SERVICE_USER="$(id -un)"
  SERVICE_FILE="/etc/systemd/system/afrc.service"

  echo "[*] Setting up systemd service ($SERVICE_FILE) ..."
  $SUDO tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=AFRC FastAPI Server
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${ROOT_DIR}
ExecStart=${ROOT_DIR}/venv/bin/uvicorn backend.main:app --host ${HOST} --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

  # Stop any manual/nohup uvicorn process before starting the systemd unit
  $SUDO systemctl stop afrc.service 2>/dev/null || true
  pkill -f "uvicorn backend.main:app" 2>/dev/null || true

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable afrc.service >/dev/null
  $SUDO systemctl restart afrc.service

  echo ""
  echo "[✓] AFRC is now running in the background via systemd on http://${HOST}:${PORT}"
  echo "    - Check status : sudo systemctl status afrc"
  echo "    - View logs    : sudo journalctl -u afrc -f"
  echo "    - Restart app  : sudo systemctl restart afrc"
  echo "    - Stop app     : sudo systemctl stop afrc"
  echo ""
  $SUDO systemctl status afrc.service --no-pager -l || true
  exit 0
fi

echo ""
echo "[✓] Starting AFRC server in foreground on http://${HOST}:${PORT} ..."
exec uvicorn backend.main:app --host "$HOST" --port "$PORT"
