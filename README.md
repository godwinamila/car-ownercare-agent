# Elyra Customer Assistant

A demo AI agent for the **WSO2 Agent Platform (Agent Manager)**. "Aria" answers general questions about Elyra,
a fictional premium electric vehicle brand: models and specifications, charging, warranty terms, the service
network, software updates, service campaigns, contact channels and how-to topics. It's built with **LangGraph**
(a ReAct tool-calling agent) and uses static demo data.

It doesn't need to know who the customer is. There's no sign-in, no account or vehicle data, and no bookings.
Questions about a customer's own car or order get the general policy plus the right contact channel.

> Elyra, its models, specifications, prices, phone numbers, addresses and campaigns in
> `data/company_data.json` are fictional demo data.

## What it can do

| Area | Tools |
|---|---|
| Company | `get_company_info`: about Elyra, markets, the Elyra app, Elyra Club / Care / Charge |
| Models | `list_models`, `get_model_details`, `compare_models`: specs, range, charging, towing, indicative prices |
| Warranty | `get_warranty_policy`: terms by country or region, and exclusions |
| Service network | `find_service_centers`, `get_service_catalog`: locations, services, typical durations and prices |
| Software and campaigns | `get_software_updates`, `get_service_campaigns` |
| Contact | `get_contact_channels`: customer care and roadside numbers, email, hours, plus safety-first advice |
| Help articles | `search_knowledge_base`: charging, home chargers, range, OTA, digital key, test drives, ordering, towing, privacy and more |

## Project layout

```
main.py                  Entry point: starts the HTTP service on port 8000
agent.py                 FastAPI app (/chat, /health) and the LangGraph agent
tools.py                 Company information tools and their schemas
system_prompt.py         Persona and behaviour rules
data/company_data.json   Static demo data
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
python cli.py --country Netherlands    # ...telling the assistant the customer's country
pytest -q                              # tool tests
```

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message": "Which Elyra can tow 2,000 kg?", "session_id": "demo-1", "context": {"country": "Sweden"}}'
```

## API

| Method | Path | Body / response |
|---|---|---|
| `POST` | `/chat` | Request `{"message": "...", "session_id": "...", "context": {"country": "Netherlands"}}`, response `{"response": "..."}`. `message` and `session_id` are required; `context` may be `{}` |
| `DELETE` | `/chat/{session_id}` | Clears a conversation |
| `GET` | `/health` | Liveness, model, LLM mode |
| `GET` | `/openapi.json`, `/docs` | OpenAPI spec and Swagger UI |

Conversation history is kept in LangGraph's in-memory checkpointer, keyed by `session_id`. `context` is optional:
`country` tells the assistant which market the customer is in, and `channel` (for example `Website chat`) is passed
through as information.

## Deploy on WSO2 Agent Manager

| Field | Value |
|---|---|
| Repository | this repo, branch `main` |
| App path | `.` |
| Language / version | Python, 3.11 |
| Start command | `python main.py` |
| Agent interface | Chat agent: `POST /chat`, port `8000` |
| LLM provider | Attach an LLM provider to the agent in the console. No OpenAI key is needed in the agent (see below) |
| Resources | CPU limit `1`, memory limit `1Gi` (Deploy, then Edit Resource Configurations). The default 0.1 CPU is too little for LangGraph to start within the platform's startup window, especially when Intel images run emulated on Apple Silicon |

### LLM provider

On Agent Manager the agent uses the **LLM provider configured in the platform**, not a key of its own. Calls go
through the platform's AI gateway, which holds the real provider credentials and applies its policies and
guardrails. The agent only receives a gateway URL and a platform-issued key.

1. Configure an OpenAI-compatible LLM provider in Agent Manager (a model such as `gpt-4o`).
2. Attach it to this agent. The platform injects `CUSTOMER_ASSISTANT_1_URL` and `CUSTOMER_ASSISTANT_1_API_KEY`.
3. Deploy. `GET /health` should show `"llm_mode": "platform"`.

Tell the agent which variables those are by adding two environment variables to the agent in the console:
`LLM_PROVIDER_URL_VAR=CUSTOMER_ASSISTANT_1_URL` and `LLM_PROVIDER_KEY_VAR=CUSTOMER_ASSISTANT_1_API_KEY`. If you
rename the injected variables or deploy under another agent name, update these two values; no code change is
needed. Without them, the agent calls OpenAI directly with `OPENAI_API_KEY`. The key is sent in the
`API-Key` header (the platform default). If the provider's Security settings use a different header name, set
`LLM_PROVIDER_AUTH_HEADER` to match. Remove any `OPENAI_API_KEY` or `OPENAI_API_KEY_DEFAULT` from the agent's
environment variables on the platform.

### Configuration

Settings come from environment variables. A local `.env` file is loaded too; it is git-ignored, and real
environment variables take precedence over it.

| Variable | Default | Purpose |
|---|---|---|
| `CUSTOMER_ASSISTANT_1_URL`, `CUSTOMER_ASSISTANT_1_API_KEY` | injected by Agent Manager | Gateway URL and key of the attached LLM provider |
| `LLM_PROVIDER_URL_VAR`, `LLM_PROVIDER_KEY_VAR` | not set | **Set these on the platform**: the names of the two injected variables above |
| `LLM_PROVIDER_AUTH_HEADER` | `API-Key` | Header that carries the platform key |
| `OPENAI_API_KEY` | not set | Local development only: your own OpenAI key, used when no platform provider is present |
| `OPENAI_MODEL` | `gpt-4o` | Model name sent with each request |
| `AGENT_PORT` | `8000` | HTTP port. Keep 8000 on Agent Manager; the Chat Agent interface expects it |

## Sample questions

1. *"What models do you have?"*
2. *"Compare the 007 and the 7X for a family that tows a caravan."* (comparison table; the 7X tows 2,000 kg)
3. *"What warranty do I get in Sweden?"*
4. *"How fast does the 7X charge, and can Elyra install a home charger?"*
5. *"Is there any recall on the Elyra 007?"* (campaign SC-2026-007A for some 2025 cars)
6. *"Where can I service my car in Australia?"*
7. *"Can you check my car? My VIN is ..."* (declines politely and points to the app or customer care)
8. *"I smell burning from under the car on the motorway."* (safety steps first, then the roadside number)
