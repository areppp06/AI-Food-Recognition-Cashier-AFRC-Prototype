# AI Food Recognition Cashier (AFRC) Prototype v1

**AI Food Recognition & Pricing System for Malaysian Nasi Campur / Self-Service Stalls**

## Core Principle
- AI identifies food & quantities (with confidence)
- Backend is the single source of truth for menu, prices, and bill calculation
- Cashier verifies low-confidence results (PIN protected)
- Prices never come from AI

## Features Implemented in Prototype
- Configurable daily menu (admin)
- Camera-only photo capture (no gallery upload)
- AI vision integration (xAI Grok, OpenRouter, OpenAI GPT-4o, or Gemini, with mock fallback)
- Strict backend pricing engine
- Cashier verification screen with 4-digit PIN
- Audit logs for cashier actions
- Structured JSON responses
- Server-enforced scan rate limiting tied to session, IP address, and device ID (persisted in SQLite, live-configurable via `env/rate-limit.env`)

## Tech Stack
- Backend: Python + FastAPI + SQLite
- Frontend: Vanilla HTML/JS (camera via getUserMedia)
- AI: xAI Grok, OpenRouter, OpenAI Vision, or Google Gemini (configurable via `env/.env`)

## Quick Start / VPS Deployment (One-Command Setup)

> 📖 **See [QUICKSTART.md](./QUICKSTART.md)** for the full step-by-step VPS deployment guide, environment variables reference table, `systemd` service setup, and HTTPS camera configuration.

### 1. Install Prerequisites (Fresh Ubuntu/Debian VPS)
```bash
sudo apt update && sudo apt install -y git python3 python3-venv python3-pip
```

### 2. Clone the Repository & Run the Installer
```bash
git clone https://github.com/areppp06/AI-Food-Recognition-Cashier-AFRC-Prototype.git
cd AI-Food-Recognition-Cashier-AFRC-Prototype
bash install.sh
```

What `bash install.sh` does automatically:
1. **Interactive Env Setup**: On first run (when `env/.env`, `env/curlec.env`, or `env/rate-limit.env` is missing), it prompts you to enter each environment variable one by one. Press **Enter** to keep the default shown in `[brackets]`, or type your value.
   - *Tip*: Include your VPS IP or domain in `TURNSTILE_HOSTNAMES` (e.g., `localhost,127.0.0.1,YOUR_VPS_IP`).
2. **Virtual Environment & Packages**: Creates `./venv` and installs all dependencies from `requirements.txt`.
3. **Auto-Configures & Starts `systemd` Service**: Automatically creates `/etc/systemd/system/afrc.service`, enables it on boot, and runs FastAPI (`uvicorn`) in the background on `http://0.0.0.0:8000` (or pass `--foreground` to run in the current terminal).

### Useful `install.sh` Commands
- **Re-configure environment variables interactively anytime:**
  ```bash
  bash install.sh --configure
  ```
- **Install & configure only (without starting the server):**
  ```bash
  bash install.sh --setup-only
  ```
- **Run on a custom port (e.g., `8080`):**
  ```bash
  PORT=8080 bash install.sh
  ```
- **Run in the background on a VPS (after initial setup):**
  ```bash
  nohup bash install.sh > afrc.log 2>&1 &
  ```
  *(Note: Browser camera access via `getUserMedia` requires `HTTPS` when accessed outside `localhost`. Put Caddy, Nginx, or a Cloudflare Tunnel in front of port `8000` for HTTPS on your VPS.)*

### Manual Setup
1. `python3 -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt`
3. Copy example configuration files from `env/examples/` into `env/` (e.g. `cp env/examples/.env.example env/.env`, `cp env/examples/curlec.env.example env/curlec.env`, `cp env/examples/rate-limit.env.example env/rate-limit.env`, `cp env/examples/restaurant.env.example env/restaurant.env`) and configure your keys/limits
4. `uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000`
5. Open http://localhost:8000

### Default Credentials
- Admin / Cashier PIN: `1234` (change immediately in admin panel)

## Endpoints
- `/` - Customer camera + recognition
- `/admin` - Menu & cashier management
- `/cashier` - Verification screen
- `/api/recognize` - POST image + menu filter
- `/api/menu` - Daily menu CRUD
- `/api/verify` - Cashier correction (PIN required)

## Project Structure
```
.
├── backend/
│   ├── main.py          # FastAPI app + routes
│   ├── db.py            # SQLite + models
│   ├── schemas.py       # Pydantic models
│   └── ai_service.py    # Vision AI wrapper
├── env/
│   ├── examples/
│   │   ├── .env.example
│   │   ├── curlec.env.example
│   │   ├── rate-limit.env.example
│   │   └── restaurant.env.example
│   ├── .env                    # Secrets & keys (git-ignored)
│   ├── curlec.env              # Curlec secrets (git-ignored)
│   ├── rate-limit.env          # Rate limits (git-ignored)
│   └── restaurant.env          # Restaurant profile (git-ignored)
├── frontend/
│   ├── index.html       # Customer flow
│   ├── admin.html
│   ├── cashier.html
│   ├── pay.html
│   └── success.html
├── install.sh           # One-command interactive setup & launcher
├── QUICKSTART.md        # Dedicated VPS deployment & quickstart guide
├── requirements.txt
└── README.md
```

## Next Steps for Production
- Switch to PostgreSQL
- Add payment gateway
- Fine-tune vision prompts / custom model
- Multi-stall support
- Inventory linkage

Built for Malaysian hawker/self-service food stalls.  
Prices stay in the backend. AI only sees food.
