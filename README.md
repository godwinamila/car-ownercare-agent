# Elyra Owner Care Agent

A demo AI agent for Elyra owners, built for deployment on the **WSO2 Agent Platform (Agent Manager)**.
"Aria" is the assistant persona. It is built with **LangGraph** (a ReAct tool-calling agent) and answers owners'
questions and acts on their behalf, using static demo data.

> All owners, VINs, prices, campaigns and service centers in `data/owner_care_data.json` are fictional demo data.

## What it can do

| Area | Tools |
|---|---|
| Identify the owner | `identify_owner` (owner ID, email, phone, VIN or licence plate) |
| Connected car | `get_vehicle_status`: charge, range, battery health, tyre pressures, 12V battery, software, alerts |
| Maintenance | `get_service_history`, `get_maintenance_recommendations` |
| Warranty and recalls | `get_warranty_status`, `check_recalls_and_updates` (campaigns and OTA updates) |
| Service booking | `find_service_centers`, `get_service_catalog`, `get_available_slots`, `book_service_appointment`, `list_appointments`, `reschedule_appointment`, `cancel_appointment` |
| Roadside assistance | `request_roadside_assistance` |
| Support cases | `create_support_case`, `get_support_cases` |
| Help articles | `search_knowledge_base` |

Every vehicle tool checks that the VIN belongs to the identified owner, so one owner can't read another owner's data.

## Project layout

```
main.py                  Entry point: starts the HTTP service on port 8000
agent.py                 FastAPI app (/chat, /health) and the LangGraph agent
tools.py                 Tool implementations and tool schemas
system_prompt.py         Persona and behaviour rules
data/owner_care_data.json  Static demo data: owners, vehicles, service history, centers, campaigns, FAQs
cli.py                   Chat with the agent in the terminal
tests/test_tools.py      Unit tests for the tools (no API key needed)
```

## Run locally

Requires Python 3.10+ for LangGraph (the Agent Manager deploy uses 3.11). On macOS: `brew install python@3.11`.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # then put your OpenAI key in .env

python main.py                         # HTTP service on http://localhost:8000
python cli.py                          # or chat in the terminal
python cli.py --owner ZK-OWN-1004      # ...as an owner already signed in to the app
pytest -q                              # tool tests
```

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message": "Hi, my email is oliver.chen@example.com. Is my car OK?", "session_id": "demo-1"}'
```

## API

| Method | Path | Body / response |
|---|---|---|
| `POST` | `/chat` | Request `{"message": "...", "session_id": "...", "context": {"owner_id": "ZK-OWN-1001"}}`, response `{"response": "..."}`. `message` and `session_id` are required; `context` may be `{}` |
| `DELETE` | `/chat/{session_id}` | Clears a conversation |
| `GET` | `/health` | Liveness, model and whether an API key is configured |
| `GET` | `/openapi.json`, `/docs` | OpenAPI spec and Swagger UI |

Conversation history is kept in LangGraph's in-memory checkpointer, keyed by `session_id` (the thread ID). `context.owner_id` is optional. Pass it to
simulate an owner who is already signed in to the Elyra app, so the agent skips the identification step. Bookings,
cases and roadside requests created during a demo are held in memory and reset when the service restarts.

## Deploy on WSO2 Agent Manager

| Field | Value |
|---|---|
| Repository | this repo, branch `main` |
| App path | `.` |
| Language / version | Python 3.11 |
| Start command | `python main.py` |
| Agent interface | Chat agent: `POST /chat`, port `8000` |
| LLM provider | Attach an LLM provider to the agent in the console. No OpenAI key is needed in the agent (see below) |

### LLM provider

On Agent Manager the agent uses the **LLM provider configured in the platform**, not a key of its own. Calls go
through the platform's AI gateway, which holds the real provider credentials and applies its policies and
guardrails. The agent only receives a gateway URL and a platform-issued key.

1. Configure an OpenAI-compatible LLM provider in Agent Manager (a model such as `gpt-4o`).
2. Attach it to this agent. The platform injects `<AGENT>_1_URL` and `<AGENT>_1_API_KEY`.
3. Deploy. `GET /health` should show `"llm_mode": "platform"`.

The agent finds that variable pair automatically. If you attach more than one LLM provider, rename the one to use
to `LLM_PROVIDER_URL` / `LLM_PROVIDER_KEY` in the console. The key is sent in the `API-Key` header (the
platform default). If the provider's Security settings use a different header name, set
`LLM_PROVIDER_AUTH_HEADER` to match. Remove any `OPENAI_API_KEY` or `OPENAI_API_KEY_DEFAULT` from the agent's
environment variables on the platform.

### Configuration

Settings come from environment variables. A local `.env` file is loaded too; it is git-ignored, and real
environment variables take precedence over it.

| Variable | Default | Purpose |
|---|---|---|
| `<AGENT>_1_URL`, `<AGENT>_1_API_KEY` | injected by Agent Manager | Gateway URL and key of the attached LLM provider |
| `LLM_PROVIDER_URL`, `LLM_PROVIDER_KEY` | not set | Use these names to choose a provider explicitly |
| `LLM_PROVIDER_AUTH_HEADER` | `API-Key` | Header that carries the platform key |
| `OPENAI_API_KEY` | not set | Local development only: your own OpenAI key, used when no platform provider is present |
| `OPENAI_MODEL` | `gpt-4o` | Model name sent with each request |
| `AGENT_PORT` | `8000` | HTTP port. Keep 8000 on Agent Manager; the Chat Agent interface expects it |
| `DEMO_TODAY` | today's date | Pin the date (`YYYY-MM-DD`) so due dates and slots stay the same across rehearsals |

## Demo personas

| Owner | ID / email | Vehicle | What to show |
|---|---|---|---|
| Emma de Vries, Amsterdam | `ZK-OWN-1001` / emma.devries@example.com | Elyra 001 and Elyra X | Two cars (agent asks which one), annual service booking with a loaner car |
| Lars Andersson, Stockholm | `ZK-OWN-1002` | Elyra 7X | Existing winter-tyre appointment: reschedule or cancel it |
| Aisha Al Mansoori, Dubai | `ZK-OWN-1003` | Elyra 009 | Open infotainment case, cabin filter due, center open Saturday to Thursday |
| Oliver Chen, Sydney | `ZK-OWN-1004` | Elyra 7X | Low rear-left tyre pressure and low charge: roadside assistance |
| Priya Nair, Singapore | `ZK-OWN-1005` | Elyra X | Overdue service, weak 12V battery, pending OTA update |
| James Wong, Hong Kong | `ZK-OWN-1006` | Elyra 007 | Open seatbelt service campaign |

### Sample script

1. *"Hi, I'm Priya, my email is priya.nair@example.com. My car showed a battery warning this morning."*
   The agent finds the weak 12V battery, the overdue service and the pending OTA update.
2. *"Can you book the service and fix the battery at the same time, sometime next week?"*
   The agent offers slots, confirms the details, then books.
3. *"I have a flat tyre on the M4 westbound near Parramatta"* (as Oliver, `cli.py --owner ZK-OWN-1004`).
   The agent gives safety advice first, then dispatches roadside assistance.
4. *"Is there anything I need to bring my car in for?"* (as James). The agent finds the open campaign and offers to book it.
5. *"Show me Emma's car"* (as Lars). The agent refuses to share another owner's data.
