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
- AI: xAI Grok, OpenRouter, OpenAI Vision, or Google Gemini (configurable via .env)

## Quick Start

1. Clone the repo
2. `cd AI-Food-Recognition-Cashier-AFRC-Prototype`
3. `python -m venv venv && source venv/bin/activate` (or venv\Scripts\activate on Windows)
4. `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and add your API key (optional for mock mode)
6. `uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000`
7. Open http://localhost:8000

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
├── frontend/
│   ├── index.html       # Customer flow
│   ├── admin.html
│   └── cashier.html
├── requirements.txt
├── .env.example
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
