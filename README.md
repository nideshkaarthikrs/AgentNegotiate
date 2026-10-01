# AgentNegotiate

**Two-agent B2B procurement negotiation system** built with LangGraph, Gemini (with a Groq fallback), and Razorpay.

> Autonomous AI-powered procurement system with deterministic financial guardrails.

## Architecture

```
Frontend (Next.js)  ←→  Backend (FastAPI + LangGraph)
      ↕                        ↕
  WebSocket            Gemini → Groq
                              ↕
                    Policy Engine (deterministic)
                              ↕
                  SQLite + Audit Trail + Razorpay
```

### Key Design Principle

**The LLM never makes financial decisions.** All accept/reject/counter decisions and price calculations are handled by a deterministic policy engine with hard constraints:

- **Budget clamping**: Buyer's counter-offer total never exceeds `max_budget`
- **Floor enforcement**: Seller's counter-offer never drops below `floor_price`
  (both hard constraints are applied *last*, so no "sanity" bound can override them)
- **Round cap**: Negotiation terminates after a single shared horizon,
  `min(buyer_max_rounds, seller_max_rounds)` — one side cannot keep conceding
  past a limit the other never authorised
- **Concession schedule**: `current_round / max_rounds` of the remaining gap.
  A 4-round negotiation concedes 25% → 50% → 75% → 100%; the final round always
  concedes the whole gap
- **Crossing detection**: if a side's own concession target has reached the
  counterparty's standing price, it accepts that price instead of restating it
- **Final-offer settlement**: before declaring "no deal", the buyer always gets
  to evaluate the seller's closing counter

The LLM (Gemini `gemini-3.5-flash-lite`, falling back to Groq `openai/gpt-oss-120b`) is only used for:
1. Parsing natural language procurement requests → structured data
2. Generating human-readable explanations of decisions

## Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Gemini API key (free from [Google AI Studio](https://aistudio.google.com/))
- Optional: Groq API key for provider fallback (free from [Groq Console](https://console.groq.com/keys))
- Razorpay test keys (from [Razorpay Dashboard](https://dashboard.razorpay.com/))

### Backend

```bash
cd backend

# Copy env file and add your keys
cp .env.example .env
# Edit .env with your GEMINI_API_KEY (and optionally GROQ_API_KEY),
# RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET

# Install dependencies
uv venv && uv pip install -e ".[dev]"

# Run tests
uv run pytest tests/ -v

# Start the server
uv run uvicorn src.api:app --reload --port 8000
```

### Frontend

```bash
cd frontend

# Install dependencies
npm install

# Start dev server
npm run dev
```

Open [http://localhost:3000](http://localhost:3000) to use the app.

### Batch Evaluation

```bash
cd backend
uv run python scripts/batch_eval.py
```

Runs 50 diverse procurement scenarios and outputs metrics proving 0% constraint violations.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| LLM | Gemini `gemini-3.5-flash-lite` → Groq `openai/gpt-oss-120b` fallback (both free tier) |
| Agent Framework | LangGraph |
| Backend | FastAPI + WebSockets |
| Database | SQLite (aiosqlite) |
| Payments | Razorpay (test mode) |
| Frontend | Next.js 15 + Tailwind CSS |
| Audit Trail | SHA-256 hash-chained logging |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/negotiate` | Start a new negotiation |
| GET | `/api/negotiations` | List all negotiations |
| GET | `/api/negotiations/{id}` | Get negotiation + rounds |
| GET | `/api/negotiations/{id}/audit` | Get audit trail (with verification) |
| POST | `/api/negotiations/{id}/pay` | Create Razorpay payment link |
| POST | `/api/negotiations/{id}/verify-payment` | Verify the Payment Link redirect signature and mark payment captured (works even when the webhook below can't reach `localhost`) |
| POST | `/api/webhooks/razorpay` | Payment webhook |
| WS | `/ws/negotiate/{id}` | Live negotiation events |

## Project Structure

```
AgentNegotiate/
├── backend/
│   ├── pyproject.toml
│   ├── .env.example
│   ├── src/
│   │   ├── schemas.py          # Pydantic models
│   │   ├── policy_engine.py    # Deterministic decision engine
│   │   ├── llm_parser.py       # Provider chain: parsing + explanation
│   │   ├── buyer_agent.py      # LangGraph buyer graph
│   │   ├── seller_agent.py     # LangGraph seller graph
│   │   ├── negotiation_runner.py # Turn-taking orchestrator
│   │   ├── database.py         # SQLite CRUD
│   │   ├── payments.py         # Razorpay integration
│   │   ├── audit.py            # Hash-chained audit trail
│   │   └── api.py              # FastAPI endpoints
│   ├── scripts/
│   │   └── batch_eval.py       # 50-scenario evaluation
│   └── tests/
│       └── test_policy_engine.py
├── frontend/
│   ├── app/
│   │   ├── page.tsx            # Landing + config form
│   │   ├── negotiate/[id]/
│   │   │   ├── page.tsx        # Live negotiation view
│   │   │   └── audit/page.tsx  # Audit trail viewer
│   │   └── metrics/page.tsx    # Metrics dashboard
│   └── components/
│       ├── OfferCard.tsx
│       └── AuditChain.tsx
└── README.md
```

## License

MIT
