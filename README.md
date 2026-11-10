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

## Tech Stack
- Backend: Python + FastAPI + SQLite
- Frontend: Vanilla HTML/JS (camera via getUserMedia)
- AI: xAI Grok, OpenRouter, OpenAI Vision, or Google Gemini (configurable via `env/.env`)

## Quick Start (One-Command Setup)

1. Clone the repo and enter the directory:
   ```bash
   git clone https://github.com/areppp06/AI-Food-Recognition-Cashier-AFRC-Prototype.git
   cd AI-Food-Recognition-Cashier-AFRC-Prototype
   ```
2. Run the installer / launcher:
   ```bash
   bash install.sh
   ```
   - On first run, `install.sh` interactively prompts you to configure all environment variables for `env/.env` and `env/curlec.env` (or you can re-run prompts anytime with `bash install.sh --configure`), creates the Python virtual environment, installs `requirements.txt`, and starts the server on `http://0.0.0.0:8000`.

### Manual Setup
1. `python3 -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt`
3. Copy `env/.env.example` to `env/.env` and `env/curlec.env.example` to `env/curlec.env` and add your keys
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
│   ├── .env.example
│   └── curlec.env.example
├── frontend/
│   ├── index.html       # Customer flow
│   ├── admin.html
│   ├── cashier.html
│   ├── pay.html
│   └── success.html
├── install.sh           # One-command interactive setup & launcher
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
