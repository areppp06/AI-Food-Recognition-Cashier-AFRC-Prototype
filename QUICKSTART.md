# AFRC Quickstart & VPS Deployment Guide

This guide walks you through setting up and deploying the **AI Food Recognition Cashier (AFRC)** on a fresh Linux VPS or local machine using the one-command installer (`install.sh`).

---

## 1. Prerequisites (Fresh Ubuntu / Debian VPS)

Connect to your VPS via SSH and install Git and Python 3 virtual environment tools:

```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip
```

---

## 2. Clone & Run One-Command Setup

Clone the repository and run `bash install.sh`:

```bash
git clone https://github.com/areppp06/AI-Food-Recognition-Cashier-AFRC-Prototype.git
cd AI-Food-Recognition-Cashier-AFRC-Prototype
bash install.sh
```

### What `bash install.sh` Does:
1. **Interactive Environment Setup** (First Run Only):
   - Checks if `env/.env`, `env/curlec.env`, and `env/rate-limit.env` exist.
   - If missing, prompts you directly in the terminal for each configuration value.
   - Press **Enter** to accept the default value shown in `[brackets]`, or type your own value and press **Enter**.
   - Saves your settings into `env/.env`, `env/curlec.env`, and `env/rate-limit.env` with restricted permissions (`chmod 600`).
2. **Python Virtual Environment**:
   - Creates `./venv` if it doesn't exist and upgrades `pip`.
   - Installs all required packages from `requirements.txt`.
3. **Auto-Configures & Starts `systemd` Service**:
   - Automatically creates `/etc/systemd/system/afrc.service`, enables it on boot, starts `uvicorn backend.main:app` in the background on `http://0.0.0.0:8000`, and prints the live service status.
   - *(Use `bash install.sh --foreground` if you want to run `uvicorn` directly in your terminal instead of via `systemd`.)*

---

## 3. Environment Variables Reference

During the interactive setup, `install.sh` walks through three files inside the `env/` directory:

### `env/.env` (App, AI Provider & Cloudflare Turnstile)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `OPENAI_API_KEY` | `sk-your-key-here` | OpenAI API key (if `AI_PROVIDER=openai`) |
| `GEMINI_API_KEY` | `your-gemini-key-here` | Google Gemini API key (if `AI_PROVIDER=gemini`) |
| `XAI_API_KEY` | `your-xai-key-here` | xAI Grok API key (if `AI_PROVIDER=grok`) |
| `XAI_MODEL` | `grok-4.7` | xAI vision model name |
| `OPENROUTER_API_KEY` | `your-openrouter-key-here` | OpenRouter API key (if `AI_PROVIDER=openrouter`) |
| `OPENROUTER_MODEL` | `x-ai/grok-4` | OpenRouter vision model ID |
| `OPENROUTER_SITE_URL` | `http://localhost:8000` | Site URL header sent to OpenRouter |
| `AI_PROVIDER` | `grok` | Active AI backend: `grok`, `openrouter`, `gemini`, `openai`, or `mock` |
| `DATABASE_URL` | `sqlite:///./afrc.db` | SQLAlchemy database connection URL |
| `CLOUDFLARE_API_TOKEN` | `your-cloudflare-api-token` | Cloudflare API token (with **Workers R2 Storage: Edit** permission for R2 plate image storage) |
| `CLOUDFLARE_ACCOUNT_ID` | `your-cloudflare-account-id` | Cloudflare account ID |
| `R2_BUCKET_NAME` | `afrc-plates` | Cloudflare R2 bucket name for plate photos (auto-created in `APAC` on startup if missing) |
| `TURNSTILE_SITEKEY` | `your-turnstile-sitekey` | Cloudflare Turnstile widget sitekey |
| `TURNSTILE_SECRET` | `your-turnstile-secret` | Cloudflare Turnstile secret key for backend verification |
| `TURNSTILE_HOSTNAMES` | `localhost,127.0.0.1` | Comma-separated allowed hostnames/IPs (add your VPS IP or domain here, e.g. `localhost,127.0.0.1,203.0.113.10`) |

### `env/curlec.env` (Curlec / Razorpay Payment Gateway)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `CURLEC_KEY_ID` | `your-curlec-key-id` | Curlec / Razorpay API Key ID |
| `CURLEC_KEY_SECRET` | `your-curlec-key-secret` | Curlec / Razorpay API Key Secret |
| `CURLEC_WEBHOOK_SECRET` | `your-curlec-webhook-secret` | Webhook secret used to verify `/api/curlec/webhook` events |

### `env/rate-limit.env` (Scan Rate Limiting & Cooldown)

> Changes saved to `env/rate-limit.env` (or `rate-limit.env`) take effect immediately on the next request without restarting the server. Active cooldowns and scan attempt windows are stored in SQLite and persist across crashes/restarts.

| Variable | Default | Description |
| :--- | :--- | :--- |
| `MAX_SCANS_PER_SESSION` | `5` | Max photo scans allowed per browser/bill session within `SCAN_WINDOW_MINUTES` |
| `MAX_SCANS_PER_DEVICE` | `5` | Max photo scans allowed per device ID within `SCAN_WINDOW_MINUTES` |
| `MAX_SCANS_PER_IP` | `6` | Max photo scans allowed per IP address within `SCAN_WINDOW_MINUTES` |
| `SCAN_WINDOW_MINUTES` | `15` | Sliding time window (in minutes) before each scan attempt drops from the count |
| `COOLDOWN_MINUTES` | `25` | Lockout duration (in minutes) once a session, device ID, IP, or retake limit is exceeded |
| `MAX_RETAKES` | `4` | Max photo retakes allowed per bill (keep `MAX_SCANS_PER_SESSION` and `MAX_SCANS_PER_DEVICE` at least `MAX_RETAKES + 1`) |

---

## 4. `install.sh` Options & Flags

- **Re-configure environment variables interactively at any time:**
  ```bash
  bash install.sh --configure
  ```
  *(Your current values in `env/.env`, `env/curlec.env`, and `env/rate-limit.env` will be shown as the defaults in `[brackets]` so you only need to type the ones you want to change.)*

- **Install dependencies & configure env without starting the server:**
  ```bash
  bash install.sh --setup-only
  ```

- **Run on a custom host or port:**
  ```bash
  HOST=127.0.0.1 PORT=8080 bash install.sh
  ```

---

## 5. Running in the Background on a VPS

Once you have completed the initial interactive setup (`bash install.sh --setup-only`), you can keep the app running after closing SSH using either `nohup` or `systemd`.

### Option A: Quick Background Run (`nohup`)
```bash
nohup bash install.sh > afrc.log 2>&1 &
```
- View live logs: `tail -f afrc.log`
- Stop server: `pkill -f "uvicorn backend.main:app"`

### Option B: Persistent `systemd` Service (Recommended for VPS)
Create `/etc/systemd/system/afrc.service`:

```ini
[Unit]
Description=AFRC FastAPI Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/AI-Food-Recognition-Cashier-AFRC-Prototype
ExecStart=/bin/bash /root/AI-Food-Recognition-Cashier-AFRC-Prototype/install.sh
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```
*(Adjust `User` and `WorkingDirectory` if cloned under a non-root user.)*

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now afrc
sudo systemctl status afrc
```

---

## 6. Enabling HTTPS for Camera Access (`getUserMedia`)

Modern browsers require **HTTPS** (or `localhost`) to allow camera access (`navigator.mediaDevices.getUserMedia`). When accessing your VPS from a phone or tablet, put a reverse proxy with HTTPS in front of port `8000`.

### Quickest HTTPS with Caddy
```bash
sudo apt install -y caddy
```
Replace `/etc/caddy/Caddyfile` with your domain name:
```caddyfile
your-domain.com {
    reverse_proxy 127.0.0.1:8000
}
```
Reload Caddy:
```bash
sudo systemctl reload caddy
```

---

## 7. Updating on the VPS

To pull new changes and restart cleanly:
```bash
cd AI-Food-Recognition-Cashier-AFRC-Prototype
git pull
bash install.sh
```
