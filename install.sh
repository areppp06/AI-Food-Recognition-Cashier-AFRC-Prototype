#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

ENV_DIR="$ROOT_DIR/env"
EXAMPLES_DIR="$ENV_DIR/examples"
MAIN_ENV="$ENV_DIR/.env"
MAIN_EXAMPLE="$EXAMPLES_DIR/.env.example"
AI_MODEL_ENV="$ENV_DIR/AI_model.env"
AI_MODEL_EXAMPLE="$EXAMPLES_DIR/AI_model.env.example"
CLOUDFLARE_ENV="$ENV_DIR/cloudflare.env"
CLOUDFLARE_EXAMPLE="$EXAMPLES_DIR/cloudflare.env.example"
CURLEC_ENV="$ENV_DIR/curlec.env"
CURLEC_EXAMPLE="$EXAMPLES_DIR/curlec.env.example"
RATELIMIT_ENV="$ENV_DIR/rate-limit.env"
RATELIMIT_EXAMPLE="$EXAMPLES_DIR/rate-limit.env.example"
RESTAURANT_ENV="$ENV_DIR/restaurant.env"
RESTAURANT_EXAMPLE="$EXAMPLES_DIR/restaurant.env.example"

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

  # Auto-populate defaults from legacy .env if migrating to separate env files
  if [[ "$target_file" != "$MAIN_ENV" && -f "$MAIN_ENV" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line%$'\r'}"
      [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
      if [[ "$line" == *"="* ]]; then
        local k="${line%%=*}"
        local v="${line#*=}"
        v="${v%%#*}"
        v="$(echo "$v" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        if [[ -z "${existing_values[$k]+_}" && -n "$v" ]]; then
          existing_values["$k"]="$v"
        fi
      fi
    done < "$MAIN_ENV"
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

configure_env_file "$MAIN_ENV" "$MAIN_EXAMPLE" "App & Server Environment (env/.env)"
configure_env_file "$AI_MODEL_ENV" "$AI_MODEL_EXAMPLE" "AI Model API Environment (env/AI_model.env)"
configure_env_file "$CLOUDFLARE_ENV" "$CLOUDFLARE_EXAMPLE" "Cloudflare R2 & Turnstile Environment (env/cloudflare.env)"
configure_env_file "$RESTAURANT_ENV" "$RESTAURANT_EXAMPLE" "Restaurant & Company Profile (env/restaurant.env)"
configure_env_file "$CURLEC_ENV" "$CURLEC_EXAMPLE" "Curlec Payment Environment (env/curlec.env)"
configure_env_file "$RATELIMIT_ENV" "$RATELIMIT_EXAMPLE" "Rate Limit Environment (env/rate-limit.env)"

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

SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  fi
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
SETUP_CADDY_DOMAIN=""

if [[ -f "$MAIN_ENV" ]]; then
  ENV_HOST="$(grep -E '^[[:space:]]*HOST=' "$MAIN_ENV" 2>/dev/null | cut -d= -f2- | tr -d ' "\r' || true)"
  ENV_PORT="$(grep -E '^[[:space:]]*PORT=' "$MAIN_ENV" 2>/dev/null | cut -d= -f2- | tr -d ' "\r' || true)"
  [[ -n "$ENV_HOST" ]] && HOST="$ENV_HOST"
  [[ -n "$ENV_PORT" ]] && PORT="$ENV_PORT"
fi

sync_env_port_and_host() {
  if [[ -f "$MAIN_ENV" ]]; then
    if grep -q '^[[:space:]]*PORT=' "$MAIN_ENV" 2>/dev/null; then
      sed -i.bak -e "s/^[[:space:]]*PORT=.*/PORT=${PORT}/" "$MAIN_ENV" 2>/dev/null || true
      rm -f "${MAIN_ENV}.bak" 2>/dev/null || true
    else
      echo "PORT=${PORT}" >> "$MAIN_ENV"
    fi
    if grep -q '^[[:space:]]*HOST=' "$MAIN_ENV" 2>/dev/null; then
      sed -i.bak -e "s/^[[:space:]]*HOST=.*/HOST=${HOST}/" "$MAIN_ENV" 2>/dev/null || true
      rm -f "${MAIN_ENV}.bak" 2>/dev/null || true
    else
      echo "HOST=${HOST}" >> "$MAIN_ENV"
    fi
  fi
}

if [[ -t 0 ]]; then
  if [[ -f "/etc/caddy/Caddyfile" && "$FORCE_CONFIGURE" == "false" ]] && grep -q "reverse_proxy" /etc/caddy/Caddyfile 2>/dev/null; then
    EXISTING_DOMAIN="$(grep -E '^[a-zA-Z0-9.-]+\s*\{' /etc/caddy/Caddyfile 2>/dev/null | head -n1 | sed -e 's/[{[:space:]]//g' || true)"
    echo "[✓] Found existing Caddy configuration in /etc/caddy/Caddyfile${EXISTING_DOMAIN:+ ($EXISTING_DOMAIN)}"
    SETUP_CADDY_DOMAIN="$EXISTING_DOMAIN"
    HOST="127.0.0.1"
    PORT="${PORT:-8000}"
    sync_env_port_and_host
  else
    echo ""
    echo "============================================================"
    echo " Web Server & Reverse Proxy Configuration"
    echo "============================================================"
    echo " NOTE: Automated Caddy HTTPS setup is intended for dedicated / single-app servers."
    echo " If you're already using a Proxy Manager (Nginx, Caddy with other apps,"
    echo " Nginx Proxy Manager, Traefik, Cloudflare Tunnel) for another purpose,"
    echo " or you know what you are cooking, select 'N' and point your proxy to AFRC's port."
    echo "============================================================"
    read -r -p "Would you like to setup Caddy reverse proxy with automatic HTTPS? [y/N]: " setup_caddy </dev/tty || setup_caddy="n"
    setup_caddy="$(echo "$setup_caddy" | tr '[:upper:]' '[:lower:]')"

    if [[ "$setup_caddy" == "y" || "$setup_caddy" == "yes" ]]; then
      read -r -p "Enter your domain name (e.g. e-cashier.takcakap.com): " domain_name </dev/tty || domain_name=""
      domain_name="$(echo "$domain_name" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

      if [[ -z "$domain_name" ]]; then
        echo "[!] No domain entered. Falling back to local port setup."
        read -r -p "Enter local port to run the server on [${PORT}]: " custom_port </dev/tty || custom_port=""
        custom_port="$(echo "$custom_port" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        PORT="${custom_port:-$PORT}"
        HOST="0.0.0.0"
        sync_env_port_and_host
      else
        SETUP_CADDY_DOMAIN="$domain_name"
        PORT="8000"
        HOST="127.0.0.1"
        sync_env_port_and_host

        if ! command -v caddy >/dev/null 2>&1; then
          echo "[*] Installing Caddy web server..."
          if command -v apt-get >/dev/null 2>&1; then
            $SUDO apt-get update -y
            $SUDO apt-get install -y caddy || {
              $SUDO apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
              curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | $SUDO gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg --yes 2>/dev/null || true
              curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | $SUDO tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
              $SUDO apt-get update -y
              $SUDO apt-get install -y caddy
            }
          elif command -v dnf >/dev/null 2>&1; then
            $SUDO dnf install -y 'dnf-command(copr)' 2>/dev/null || true
            $SUDO dnf copr enable -y @caddy/caddy 2>/dev/null || true
            $SUDO dnf install -y caddy
          fi
        fi

        if command -v caddy >/dev/null 2>&1; then
          $SUDO mkdir -p /etc/caddy
          if [[ -f /etc/caddy/Caddyfile ]]; then
            $SUDO cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak 2>/dev/null || true
          fi
          $SUDO tee /etc/caddy/Caddyfile >/dev/null <<EOF
${SETUP_CADDY_DOMAIN} {
    header {
        X-Frame-Options "SAMEORIGIN"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
        -Server
    }
    request_body {
        max_size 2MB
    }
    reverse_proxy 127.0.0.1:${PORT} {
        header_up -CF-Connecting-IP
        trusted_proxies 173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22 2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32
    }
}
EOF
          if command -v systemctl >/dev/null 2>&1; then
            $SUDO systemctl enable caddy >/dev/null 2>&1 || true
            $SUDO systemctl restart caddy 2>/dev/null || $SUDO systemctl reload caddy 2>/dev/null || true
          fi
          echo "[✓] Caddy configured for https://${SETUP_CADDY_DOMAIN} -> 127.0.0.1:${PORT}"
        else
          echo "[!] Caddy could not be installed automatically. Binding to 0.0.0.0:${PORT}"
          HOST="0.0.0.0"
          sync_env_port_and_host
        fi
      fi
    else
      read -r -p "Enter local port to run the server on [${PORT}]: " custom_port </dev/tty || custom_port=""
      custom_port="$(echo "$custom_port" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      PORT="${custom_port:-$PORT}"
      HOST="0.0.0.0"
      sync_env_port_and_host
    fi
  fi
fi

if [[ "$FOREGROUND" == "false" ]] && command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
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
  if [[ -n "${SETUP_CADDY_DOMAIN:-}" ]]; then
    echo "[✓] AFRC is now running in the background via systemd behind Caddy HTTPS:"
    echo "    - Public URL   : https://${SETUP_CADDY_DOMAIN}"
    echo "    - Local Port   : http://${HOST}:${PORT}"
  else
    echo "[✓] AFRC is now running in the background via systemd on http://${HOST}:${PORT}"
  fi
  echo "    - Check status : sudo systemctl status afrc"
  echo "    - View logs    : sudo journalctl -u afrc -f"
  echo "    - Restart app  : sudo systemctl restart afrc"
  echo "    - Stop app     : sudo systemctl stop afrc"
  if [[ -n "${SETUP_CADDY_DOMAIN:-}" ]]; then
    echo "    - Caddy status : sudo systemctl status caddy"
    echo "    - Caddy logs   : sudo journalctl -u caddy -f"
  fi
  echo ""
  $SUDO systemctl status afrc.service --no-pager -l || true
  "${ROOT_DIR}/venv/bin/python3" -m backend.auth print-setup-qr 2>/dev/null || true
  exit 0
fi

echo ""
echo "[✓] Starting AFRC server in foreground on http://${HOST}:${PORT} ..."
exec uvicorn backend.main:app --host "$HOST" --port "$PORT"
