"""Elyra Owner Care agent: OpenAI tool-calling loop plus a FastAPI chat service.

Endpoints (the WSO2 Agent Manager chat-agent contract):
  POST /chat    {"message": str, "session_id": str, "context": {...}} -> {"response": str}
  GET  /health  liveness and configuration
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import openai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

load_dotenv()  # picks up a local .env file if present; real environment variables take precedence

from system_prompt import SYSTEM_PROMPT  # noqa: E402
from tools import TOOL_SCHEMAS, execute_tool, today  # noqa: E402

log = logging.getLogger("owner_care_agent")

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "12"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))


def _platform_llm_provider() -> Tuple[Optional[str], Optional[str], str]:
    """URL, key and source of the LLM provider attached to this agent in WSO2 Agent Manager.

    Agent Manager injects a URL / key pair whose names are set in the console when the provider is attached.
    Checked in order:
      1. LLM_PROVIDER_URL / LLM_PROVIDER_KEY (explicit names)
      2. OPENAI_BASE_URL (or OPENAI_URL) / OPENAI_API_KEY
      3. a single <NAME>_URL / <NAME>_API_KEY pair, e.g. the default <AGENT>_1_URL / <AGENT>_1_API_KEY
    """
    if os.getenv("LLM_PROVIDER_URL"):
        return os.getenv("LLM_PROVIDER_URL"), os.getenv("LLM_PROVIDER_KEY"), "LLM_PROVIDER_URL"
    for url_var in ("OPENAI_BASE_URL", "OPENAI_URL"):
        if os.getenv(url_var):
            return os.getenv(url_var), os.getenv("OPENAI_API_KEY"), url_var
    pairs = sorted(
        name[: -len("_URL")]
        for name in os.environ
        if name.endswith("_URL") and "_MCP_" not in name and os.getenv(name[: -len("_URL")] + "_API_KEY")
    )
    if len(pairs) == 1:
        return os.getenv(pairs[0] + "_URL"), os.getenv(pairs[0] + "_API_KEY"), pairs[0] + "_URL"
    if len(pairs) > 1:
        log.warning("Several LLM provider variables found %s; set LLM_PROVIDER_URL/LLM_PROVIDER_KEY to choose one.", pairs)
    return None, None, ""


def _llm_env_var_names() -> List[str]:
    """Names (never values) of variables that look like LLM/MCP settings, to help debug a deployment."""
    return sorted(n for n in os.environ if re.search(r"(_URL|_API_KEY)$", n))


# Preferred: the LLM provider configured in WSO2 Agent Manager. Calls go through the platform's AI gateway,
# which holds the real provider credentials and applies its policies. The agent only has a platform-issued key.
# Fallback for local development: an OpenAI key of your own (OPENAI_API_KEY).
PROVIDER_URL, PROVIDER_KEY, PROVIDER_SOURCE = _platform_llm_provider()
LLM_MODE = "platform" if PROVIDER_URL else "direct"
if LLM_MODE == "direct" and os.getenv("OPENAI_API_KEY", "").strip() and not os.getenv("OPENAI_API_KEY", "").startswith("sk-"):
    log.warning("OPENAI_API_KEY does not look like an OpenAI key (it may be a platform gateway key) but no gateway "
                "URL was found. Variables present: %s", _llm_env_var_names())
AUTH_HEADER = os.getenv("LLM_PROVIDER_AUTH_HEADER", "API-Key")  # set on the provider's Security tab in the console

if LLM_MODE == "platform":
    client = openai.OpenAI(
        base_url=PROVIDER_URL,
        api_key="not-used",
        default_headers={AUTH_HEADER: PROVIDER_KEY or "", "Authorization": ""},
    )
    API_KEY = PROVIDER_KEY
else:
    API_KEY = os.getenv("OPENAI_API_KEY")
    client = openai.OpenAI(api_key=API_KEY or "missing")

OPENAI_TOOLS = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in TOOL_SCHEMAS
]


class Session:
    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.last_used = time.time()


_sessions: Dict[str, Session] = {}
_sessions_lock = threading.Lock()


def _get_session(session_id: str) -> Session:
    now = time.time()
    with _sessions_lock:
        for sid in [s for s, sess in _sessions.items() if now - sess.last_used > SESSION_TTL_SECONDS]:
            del _sessions[sid]
        session = _sessions.setdefault(session_id, Session())
        session.last_used = now
        return session


def _first_turn_preamble(context: Optional[Dict[str, Any]]) -> str:
    # Kept out of the system prompt so the system prompt stays identical across sessions.
    lines = [f"[Session info] Today's date is {today().strftime('%A %d %B %Y')} ({today().isoformat()})."]
    if context and context.get("owner_id"):
        lines.append(f"[Session info] The owner is signed in to the Elyra app as verified owner ID {context['owner_id']}.")
    if context and context.get("channel"):
        lines.append(f"[Session info] Channel: {context['channel']}.")
    return "\n".join(lines)


def _run_tool_call(call) -> str:
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Arguments were not valid JSON: {e}"})
    result, is_error = execute_tool(call.function.name, args)
    log.info("tool %s(%s) -> %s%s", call.function.name, json.dumps(args), "ERROR " if is_error else "", result[:300])
    return result


def chat(session_id: str, message: str, context: Optional[Dict[str, Any]] = None) -> str:
    session = _get_session(session_id)
    with session.lock:
        # Work on a copy and commit only on success, so a failed API call can't leave unanswered tool calls.
        messages = list(session.messages) or [{"role": "system", "content": SYSTEM_PROMPT}]
        user_text = message if len(messages) > 1 else f"{_first_turn_preamble(context)}\n\n{message}"
        messages.append({"role": "user", "content": user_text})

        for _ in range(MAX_TOOL_ROUNDS):
            completion = client.chat.completions.create(model=MODEL, messages=messages, tools=OPENAI_TOOLS)
            msg = completion.choices[0].message

            if getattr(msg, "refusal", None):
                reply = "I'm sorry, I can't help with that request. Is there anything else about your Elyra I can help with?"
                messages.append({"role": "assistant", "content": reply})
                break

            if not msg.tool_calls:
                reply = (msg.content or "").strip() or "Is there anything else I can help you with?"
                messages.append({"role": "assistant", "content": reply})
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                        for c in msg.tool_calls
                    ],
                }
            )
            for call in msg.tool_calls:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": _run_tool_call(call)})
        else:
            reply = "Sorry, that took longer than expected. Could you rephrase or break the request into smaller steps?"
            messages.append({"role": "assistant", "content": reply})

        session.messages = messages
        return reply


# HTTP service ----------------------------------------------------------------

app = FastAPI(
    title="Elyra Owner Care Agent",
    description="AI assistant for Elyra owners: vehicle status, maintenance, warranty, service bookings, "
    "roadside assistance and support cases. Demo data only.",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    message: str = Field(..., description="The owner's message.")
    session_id: str = Field(..., description="Conversation ID. Send the same value on every turn of a conversation.")
    context: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Channel context, e.g. {\"owner_id\": \"ZK-OWN-1001\"} for an app-authenticated owner. May be {}.",
    )


class ChatResponse(BaseModel):
    response: str


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(req: ChatRequest) -> ChatResponse:
    try:
        reply = chat(req.session_id, req.message, req.context)
    except openai.AuthenticationError as e:
        log.error("LLM authentication failed (mode=%s, url=%s): %s", LLM_MODE, PROVIDER_URL or "api.openai.com", e.message)
        if LLM_MODE == "platform":
            hint = (f"The Agent Manager LLM gateway at {PROVIDER_URL} rejected the request. Check that the key is sent "
                    f"in the header the provider expects (currently '{AUTH_HEADER}', set LLM_PROVIDER_AUTH_HEADER to "
                    "change it) and that the OpenAI key saved in the platform's LLM provider is valid.")
        else:
            hint = ("No Agent Manager LLM provider was found, so the agent called OpenAI directly and OpenAI rejected "
                    "the key. Attach an LLM provider to the agent in the console, or set a valid OPENAI_API_KEY.")
        raise HTTPException(status_code=500, detail=f"LLM authentication failed ({LLM_MODE} mode). {hint} Upstream: {e.message}")
    except openai.RateLimitError:
        raise HTTPException(status_code=429, detail="The assistant is busy. Please try again shortly.")
    except openai.APIStatusError as e:
        log.exception("OpenAI API error (request id %s)", e.response.headers.get("x-request-id"))
        raise HTTPException(status_code=502, detail=f"LLM error: {e.message}")
    except openai.APIConnectionError:
        log.exception("Could not reach the LLM endpoint")
        raise HTTPException(status_code=503, detail="Could not reach the LLM service.")
    return ChatResponse(response=reply)


@app.delete("/chat/{session_id}")
def reset_session(session_id: str) -> Dict[str, Any]:
    with _sessions_lock:
        existed = _sessions.pop(session_id, None) is not None
    return {"ok": True, "cleared": existed}


def health_info() -> Dict[str, Any]:
    return {
        "ok": True,
        "model": MODEL,
        "llm_mode": LLM_MODE,
        "llm_url": PROVIDER_URL or "https://api.openai.com/v1",
        "llm_url_from": PROVIDER_SOURCE or None,
        "llm_env_vars": _llm_env_var_names(),
        "llm_key_configured": bool(API_KEY),
        "port": int(os.getenv("AGENT_PORT", "8000")),
    }


@app.get("/health")
def health() -> Dict[str, Any]:
    return health_info()
