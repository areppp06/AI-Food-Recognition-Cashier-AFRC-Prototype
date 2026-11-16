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

SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  fi
fi


configure_env_file() {
  local target_file="$1"
  local example_file="$2"
  local label="$3"

  if [[ ! -f "$example_file" ]]; then
    echo "Error: Missing template $example_file" >&2
    exit 1
  fi

  if [[ -f "$target_file" && "$FORCE_CONFIGURE" == "false" ]]; then
    echo "  [✓] Found existing $label ($target_file)"
    return
  fi

  if [[ ! -t 0 ]]; then
    cp "$example_file" "$target_file"
    chmod 600 "$target_file" 2>/dev/null || true
    echo "  [✓] Created $label from template (non-interactive mode)"
    return
  fi

  echo ""
  echo "┌──────────────────────────────────────────────────────────────────┐"
  printf "│  Configuring: %-51s│\n" "$label"
  echo "│  Press [Enter] to keep the default value in brackets             │"
  echo "└──────────────────────────────────────────────────────────────────┘"
  echo ""

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

      local prompt_text="  ▸ $key"
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
      echo ""
    else
      echo "$line" >> "$tmp_file"
    fi
  done < "$example_file"

  mv "$tmp_file" "$target_file"
  chmod 600 "$target_file" 2>/dev/null || true
  echo "  [✓] Saved $target_file"
  echo ""
}

configure_ai_model_env() {
  local target_file="$AI_MODEL_ENV"
  local example_file="$AI_MODEL_EXAMPLE"

  if [[ -f "$target_file" && "$FORCE_CONFIGURE" == "false" ]]; then
    echo "  [✓] Found existing AI Model configuration ($target_file)"
    return
  fi

  if [[ ! -t 0 ]]; then
    cp "$example_file" "$target_file"
    chmod 600 "$target_file" 2>/dev/null || true
    echo "  [✓] Created AI Model configuration from template (non-interactive mode)"
    return
  fi

  echo ""
  echo "┌──────────────────────────────────────────────────────────────────┐"
  echo "│          AI Vision Model Provider Selection                      │"
  echo "│  Choose which AI engine will recognize food dishes on plates     │"
  echo "└──────────────────────────────────────────────────────────────────┘"
  echo ""
  echo "    [1] OpenRouter    (Multi-model proxy, e.g. x-ai/grok-4)"
  echo "    [2] xAI (Grok)    (Official Grok 4.7 / Grok Vision)"
  echo "    [3] Google Gemini (Official Gemini 2.5 Flash - Recommended)"
  echo "    [4] OpenAI        (Official GPT-4o / GPT-4-turbo)"
  echo ""

  declare -A cur_ai=()
  if [[ -f "$target_file" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line%$'\r'}"
      [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
      if [[ "$line" == *"="* ]]; then
        local k="${line%%=*}"
        local v="${line#*=}"
        cur_ai["$k"]="$(echo "$v" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      fi
    done < "$target_file"
  fi

  local default_opt="3"
  local existing_prov="${cur_ai[AI_PROVIDER]:-}"
  case "$existing_prov" in
    openrouter) default_opt="1" ;;
    grok|xai)   default_opt="2" ;;
    gemini)     default_opt="3" ;;
    openai)     default_opt="4" ;;
  esac

  local choice=""
  while true; do
    read -r -p "  Select AI Provider [1-4] (default: ${default_opt}): " choice </dev/tty || choice="$default_opt"
    choice="$(echo "$choice" | tr -d ' ')"
    [[ -z "$choice" ]] && choice="$default_opt"

    case "$choice" in
      1)
        echo ""
        echo "  [Selected: OpenRouter]"
        echo "  ──────────────────────────────────────────────────"
        local def_or_key="${cur_ai[OPENROUTER_API_KEY]:-}"
        local def_or_model="${cur_ai[OPENROUTER_MODEL]:-x-ai/grok-4}"
        local def_or_url="${cur_ai[OPENROUTER_SITE_URL]:-http://localhost:8000}"

        read -r -p "  ▸ OPENROUTER_API_KEY [${def_or_key:-sk-or-...}]: " in_or_key </dev/tty || in_or_key=""
        in_or_key="$(echo "$in_or_key" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$in_or_key" ]] && in_or_key="$def_or_key"
        echo ""

        read -r -p "  ▸ OPENROUTER_MODEL [${def_or_model}]: " in_or_model </dev/tty || in_or_model=""
        in_or_model="$(echo "$in_or_model" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$in_or_model" ]] && in_or_model="$def_or_model"
        echo ""

        read -r -p "  ▸ OPENROUTER_SITE_URL [${def_or_url}]: " in_or_url </dev/tty || in_or_url=""
        in_or_url="$(echo "$in_or_url" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$in_or_url" ]] && in_or_url="$def_or_url"
        echo ""

        cat <<EOF > "$target_file"
# AI Provider Selection: OpenRouter
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=${in_or_key}
OPENROUTER_MODEL=${in_or_model}
OPENROUTER_SITE_URL=${in_or_url}
EOF
        break
        ;;

      2)
        echo ""
        echo "  [Selected: xAI (Grok)]"
        echo "  ──────────────────────────────────────────────────"
        local def_xai_key="${cur_ai[XAI_API_KEY]:-}"
        local def_xai_model="${cur_ai[XAI_MODEL]:-grok-4.7}"

        read -r -p "  ▸ XAI_API_KEY [${def_xai_key:-xai-...}]: " in_xai_key </dev/tty || in_xai_key=""
        in_xai_key="$(echo "$in_xai_key" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$in_xai_key" ]] && in_xai_key="$def_xai_key"
        echo ""

        read -r -p "  ▸ XAI_MODEL [${def_xai_model}]: " in_xai_model </dev/tty || in_xai_model=""
        in_xai_model="$(echo "$in_xai_model" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$in_xai_model" ]] && in_xai_model="$def_xai_model"
        echo ""

        cat <<EOF > "$target_file"
# AI Provider Selection: xAI (Grok)
AI_PROVIDER=grok
XAI_API_KEY=${in_xai_key}
XAI_MODEL=${in_xai_model}
EOF
        break
        ;;

      3)
        echo ""
        echo "  [Selected: Google Gemini]"
        echo "  ──────────────────────────────────────────────────"
        local def_gem_key="${cur_ai[GEMINI_API_KEY]:-}"

        read -r -p "  ▸ GEMINI_API_KEY [${def_gem_key:-AIza...}]: " in_gem_key </dev/tty || in_gem_key=""
        in_gem_key="$(echo "$in_gem_key" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$in_gem_key" ]] && in_gem_key="$def_gem_key"
        echo ""

        cat <<EOF > "$target_file"
# AI Provider Selection: Google Gemini
AI_PROVIDER=gemini
GEMINI_API_KEY=${in_gem_key}
EOF
        break
        ;;

      4)
        echo ""
        echo "  [Selected: OpenAI]"
        echo "  ──────────────────────────────────────────────────"
        local def_oai_key="${cur_ai[OPENAI_API_KEY]:-}"

        read -r -p "  ▸ OPENAI_API_KEY [${def_oai_key:-sk-...}]: " in_oai_key </dev/tty || in_oai_key=""
        in_oai_key="$(echo "$in_oai_key" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        [[ -z "$in_oai_key" ]] && in_oai_key="$def_oai_key"
        echo ""

        cat <<EOF > "$target_file"
# AI Provider Selection: OpenAI
AI_PROVIDER=openai
OPENAI_API_KEY=${in_oai_key}
EOF
        break
        ;;

      *)
        echo "  [!] Invalid choice. Please enter 1, 2, 3, or 4."
        echo ""
        ;;
    esac
  done

  chmod 600 "$target_file" 2>/dev/null || true
  echo "  [✓] Saved $target_file"
  echo ""
}

echo ""
echo "=================================================================="
echo "          AFRC Smart Cashier - System Environment Setup           "
echo "=================================================================="
echo ""

configure_env_file "$MAIN_ENV" "$MAIN_EXAMPLE" "App & Server Environment (env/.env)"
configure_ai_model_env
configure_env_file "$CLOUDFLARE_ENV" "$CLOUDFLARE_EXAMPLE" "Cloudflare R2 & Turnstile Environment (env/cloudflare.env)"
configure_env_file "$RESTAURANT_ENV" "$RESTAURANT_EXAMPLE" "Restaurant & Company Profile (env/restaurant.env)"
configure_env_file "$CURLEC_ENV" "$CURLEC_EXAMPLE" "Curlec Payment Environment (env/curlec.env)"
configure_env_file "$RATELIMIT_ENV" "$RATELIMIT_EXAMPLE" "Rate Limit Environment (env/rate-limit.env)"

# Auto-install Debian/Ubuntu system prerequisites if missing
if command -v apt-get >/dev/null 2>&1; then
  if ! command -v python3 >/dev/null 2>&1 || ! dpkg -s python3-venv >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
    echo "[*] Ensuring essential packages are installed (python3, python3-venv, python3-pip, curl, git)..."
    $SUDO apt-get update -y >/dev/null 2>&1 || true
    $SUDO apt-get install -y python3 python3-venv python3-pip curl git >/dev/null 2>&1 || true
  fi
fi

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
    echo "┌──────────────────────────────────────────────────────────────────┐"
    echo "│         Web Server & Reverse Proxy Configuration                 │"
    echo "│  Automated Caddy provides free SSL/HTTPS for custom domains      │"
    echo "└──────────────────────────────────────────────────────────────────┘"
    echo "  (Select 'N' if you prefer direct IP:Port access or your own proxy)"
    echo ""
    read -r -p "  Setup Caddy reverse proxy with automatic HTTPS? [y/N]: " setup_caddy </dev/tty || setup_caddy="n"
    setup_caddy="$(echo "$setup_caddy" | tr '[:upper:]' '[:lower:]')"

    if [[ "$setup_caddy" == "y" || "$setup_caddy" == "yes" ]]; then
      echo ""
      read -r -p "  ▸ Enter your domain name (e.g. e-cashier.takcakap.com): " domain_name </dev/tty || domain_name=""
      domain_name="$(echo "$domain_name" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

      if [[ -z "$domain_name" ]]; then
        echo "  [!] No domain entered. Falling back to local port setup."
        echo ""
        read -r -p "  ▸ Enter local port to run the server on [${PORT}]: " custom_port </dev/tty || custom_port=""
        custom_port="$(echo "$custom_port" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
        if [[ -n "$custom_port" && ! "$custom_port" =~ ^[0-9]+$ ]]; then
          echo "  [!] Invalid port '$custom_port'. Using default 8000."
          custom_port="8000"
        fi
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
      echo ""
      read -r -p "  ▸ Enter local port to run the server on [${PORT}]: " custom_port </dev/tty || custom_port=""
      custom_port="$(echo "$custom_port" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      if [[ -n "$custom_port" && ! "$custom_port" =~ ^[0-9]+$ ]]; then
        echo "  [!] Invalid port '$custom_port'. Using default 8000."
        custom_port="8000"
      fi
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

  # Setup automated SQLite backup service & timer to Cloudflare R2
  BACKUP_SERVICE_FILE="/etc/systemd/system/afrc-db-backup.service"
  BACKUP_TIMER_FILE="/etc/systemd/system/afrc-db-backup.timer"

  echo "[*] Setting up automated DB backup timer ($BACKUP_TIMER_FILE) ..."
  $SUDO tee "$BACKUP_SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=AFRC SQLite Database Backup to Cloudflare R2
After=network.target

[Service]
Type=oneshot
User=${SERVICE_USER}
WorkingDirectory=${ROOT_DIR}
ExecStart=${ROOT_DIR}/venv/bin/python3 ${ROOT_DIR}/scripts/backup_to_r2.py
StandardOutput=append:/var/log/afrc_backup.log
StandardError=append:/var/log/afrc_backup.log
EOF

  $SUDO tee "$BACKUP_TIMER_FILE" >/dev/null <<EOF
[Unit]
Description=Run AFRC DB Backup every Wednesday and Saturday at 12:02 AM Singapore Time

[Timer]
OnCalendar=Wed,Sat *-*-* 00:02:00 Asia/Singapore
Persistent=true

[Install]
WantedBy=timers.target
EOF

  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now afrc-db-backup.timer >/dev/null 2>&1 || true

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
  echo "    - DB Backup    : Wed & Sat 12:02 AM Singapore Time (Cloudflare R2)"
  echo "    - Backup timer : sudo systemctl list-timers afrc-db-backup.timer"
  echo "    - Backup logs  : tail -f /var/log/afrc_backup.log"
  if [[ -n "${SETUP_CADDY_DOMAIN:-}" ]]; then
    echo "    - Caddy status : sudo systemctl status caddy"
    echo "    - Caddy logs   : sudo journalctl -u caddy -f"
  fi
  echo ""
  $SUDO systemctl status afrc.service --no-pager -l || true
  echo ""
  echo "[*] Checking database initialization & admin onboarding..."
  "${ROOT_DIR}/venv/bin/python3" -c "from backend.db import init_db; init_db()" 2>/dev/null || true
  if [[ -n "${SETUP_CADDY_DOMAIN:-}" ]]; then
    "${ROOT_DIR}/venv/bin/python3" -m backend.auth print-setup-qr "https://${SETUP_CADDY_DOMAIN}" || true
  else
    "${ROOT_DIR}/venv/bin/python3" -m backend.auth print-setup-qr || true
  fi
  exit 0
fi

echo ""
echo "[✓] Starting AFRC server in foreground on http://${HOST}:${PORT} ..."
exec uvicorn backend.main:app --host "$HOST" --port "$PORT"
