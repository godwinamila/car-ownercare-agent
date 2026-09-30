"""Elyra customer assistant: a LangGraph tool-calling agent plus a FastAPI chat service.

Endpoints (the WSO2 Agent Manager chat-agent contract):
  POST /chat    {"message": str, "session_id": str, "context": {...}} -> {"response": str}
  GET  /health  liveness and configuration
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import openai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool, ToolException
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

load_dotenv()  # picks up a local .env file if present; real environment variables take precedence

from system_prompt import SYSTEM_PROMPT  # noqa: E402
from tools import TOOL_SCHEMAS, execute_tool, today  # noqa: E402

log = logging.getLogger("elyra_assistant")

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "12"))


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
    API_KEY = PROVIDER_KEY
    llm = ChatOpenAI(
        model=MODEL,
        base_url=PROVIDER_URL,
        api_key="not-used",
        default_headers={AUTH_HEADER: PROVIDER_KEY or "", "Authorization": ""},
    )
else:
    API_KEY = os.getenv("OPENAI_API_KEY")
    llm = ChatOpenAI(model=MODEL, api_key=API_KEY or "missing")


def _make_tool(schema: Dict[str, Any]) -> StructuredTool:
    """Wrap one tool from tools.py as a LangChain tool, reusing its JSON schema."""
    name = schema["name"]

    def run(**kwargs: Any) -> str:
        result, is_error = execute_tool(name, kwargs)
        log.info("tool %s(%s) -> %s%s", name, kwargs, "ERROR " if is_error else "", result[:300])
        if is_error:
            raise ToolException(result)  # returned to the model as an error tool message
        return result

    return StructuredTool.from_function(
        func=run,
        name=name,
        description=schema["description"],
        args_schema=schema["input_schema"],
        handle_tool_error=True,
    )


TOOLS = [_make_tool(t) for t in TOOL_SCHEMAS]

# Conversation state lives in the checkpointer, keyed by thread_id = the chat session_id.
checkpointer = InMemorySaver()
graph = create_react_agent(model=llm, tools=TOOLS, prompt=SYSTEM_PROMPT, checkpointer=checkpointer)

_session_locks: Dict[str, threading.Lock] = {}
_session_locks_guard = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _session_locks_guard:
        return _session_locks.setdefault(session_id, threading.Lock())


def _first_turn_preamble(context: Optional[Dict[str, Any]]) -> str:
    # Kept out of the system prompt so the system prompt stays identical across sessions.
    lines = [f"[Session info] Today's date is {today().strftime('%A %d %B %Y')} ({today().isoformat()})."]
    if context and context.get("country"):
        lines.append(f"[Session info] The customer is browsing from {context['country']}.")
    if context and context.get("channel"):
        lines.append(f"[Session info] Channel: {context['channel']}.")
    return "\n".join(lines)


def chat(session_id: str, message: str, context: Optional[Dict[str, Any]] = None) -> str:
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 2 * MAX_TOOL_ROUNDS + 1}
    with _session_lock(session_id):
        is_new = not graph.get_state(config).values.get("messages")
        text = f"{_first_turn_preamble(context)}\n\n{message}" if is_new else message
        try:
            result = graph.invoke({"messages": [HumanMessage(content=text)]}, config=config)
        except GraphRecursionError:
            return "Sorry, that took longer than expected. Could you rephrase or break the request into smaller steps?"

    for m in reversed(result["messages"]):
        if isinstance(m, AIMessage) and m.content and not m.tool_calls:
            return str(m.content).strip()
    return "Is there anything else I can help you with?"


# HTTP service ----------------------------------------------------------------

app = FastAPI(
    title="Elyra Customer Assistant",
    description="AI assistant that answers general questions about Elyra: models and specifications, charging, "
    "warranty, service network, software updates, service campaigns and contact channels. Demo data only.",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    message: str = Field(..., description="The customer's message.")
    session_id: str = Field(..., description="Conversation ID. Send the same value on every turn of a conversation.")
    context: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Optional context, e.g. {\"country\": \"Netherlands\", \"channel\": \"Website chat\"}. May be {}.",
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
    config = {"configurable": {"thread_id": session_id}}
    existed = bool(graph.get_state(config).values.get("messages"))
    checkpointer.delete_thread(session_id)
    return {"ok": True, "cleared": existed}


def health_info() -> Dict[str, Any]:
    return {
        "ok": True,
        "model": MODEL,
        "framework": "langgraph",
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
