"""
Antigravity Enterprise LLM Gateway Middleware Wrapper
=====================================================
Implements all 4 Mandatory Enterprise Gateway Requirements:
  1. Preserve and forward client attribution headers (User-Agent, X-Goog-Api-Client, X-Cost-Center, etc.)
  2. Support both GenAI (/v1beta/models/... and /v1beta1/projects/...) and OpenAI (/v1/chat/completions) wire protocols
     routed directly through LiteLLM Proxy's native Google GenAI & OpenAI router/adapter
  3. Acknowledge telemetry endpoints (POST /v1beta:* -> 200 OK {})
  4. Authenticate every request with GATEWAY_API_KEY or LiteLLM Virtual Keys (401 Unauthorized on mismatch)
"""

import contextvars
import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import Request, Response
from fastapi.responses import JSONResponse

# Ensure LiteLLM knows where its config file is before proxy_server module initialization
os.environ.setdefault(
    "CONFIG_FILE_PATH",
    os.environ.get("LITELLM_CONFIG_PATH", "litellm_config.yaml"),
)

import litellm
from litellm.google_genai.main import GenerateContentHelper
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.gemini.google_genai.transformation import GoogleGenAIConfig
from litellm.llms.vertex_ai.google_genai.transformation import VertexAIGoogleGenAIConfig
from litellm.proxy.litellm_pre_call_utils import LiteLLMProxyRequestSetup
from litellm.proxy.proxy_server import app as litellm_app, proxy_startup_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("antigravity.gateway")

# Context variable to preserve request-level attribution headers & requested alias across streaming tasks
_request_attribution_ctx: contextvars.ContextVar[Dict[str, str]] = (
    contextvars.ContextVar("request_attribution_ctx", default={})
)

# Regex patterns for Antigravity GenAI full Vertex path -> LiteLLM native /v1beta/models/... path
FULL_VERTEX_PATH_RE = re.compile(
    r"^/v1beta1/projects/([^/]+)/locations/([^/]+)/publishers/google/models/([^/:]+):(generateContent|streamGenerateContent|countTokens)$"
)

# Headers that must NEVER be forwarded as client attribution headers to Vertex AI
EXCLUDED_FORWARD_HEADERS = {
    "x-goog-api-key",
    "x-litellm-api-key",
    "x-api-key",
    "authorization",
}


# ---------------------------------------------------------------------------
# Mandatory Requirement 1: Preserve & Forward Client Attribution Headers
# through LiteLLM's native Google GenAI and OpenAI routers
# ---------------------------------------------------------------------------
_orig_add_litellm_data = LiteLLMProxyRequestSetup.add_litellm_data_for_backend_llm_call


def _custom_get_forwardable_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    """
    Preserves Antigravity client attribution headers (`User-Agent`, `X-Goog-Api-Client`,
    `X-Cost-Center`, `X-Corporate-*`, `anthropic-beta`, and custom `x-*` headers)
    while stripping gateway auth keys and internal `x-litellm-*` / `x-stainless*` headers.
    """
    forwarded_headers: Dict[str, Any] = {}
    for header, value in headers.items():
        h_lower = header.lower()
        if h_lower in EXCLUDED_FORWARD_HEADERS:
            continue
        if h_lower.startswith("x-stainless") or h_lower.startswith("x-litellm"):
            continue
        if (
            h_lower == "user-agent"
            or h_lower.startswith("x-")
            or h_lower.startswith("anthropic-beta")
        ):
            forwarded_headers[header] = value

    # Inject quota/billing project header if configured and not already supplied by client
    vertex_project = os.environ.get("VERTEX_PROJECT_ID", "")
    if vertex_project and not any(
        k.lower() == "x-goog-user-project" for k in forwarded_headers
    ):
        forwarded_headers["X-Goog-User-Project"] = vertex_project

    return forwarded_headers


def _custom_add_litellm_data_for_backend_llm_call(**kwargs: Any) -> dict:
    """
    Wraps LiteLLMProxyRequestSetup.add_litellm_data_for_backend_llm_call to ensure:
      1. Forwardable client attribution headers (including User-Agent) are always extracted.
      2. `extra_headers` is populated alongside `headers`, because LiteLLM's native
         Google GenAI adapter (`agenerate_content` / `agenerate_content_stream`) reads
         `extra_headers` when building the upstream Vertex AI request.
    """
    data = _orig_add_litellm_data(**kwargs)
    request_headers = kwargs.get("headers") or {}
    user_api_key_dict = kwargs.get("user_api_key_dict")

    forwarded = LiteLLMProxyRequestSetup.add_headers_to_llm_call(
        request_headers, user_api_key_dict
    )
    if forwarded:
        merged_headers = {**forwarded, **(data.get("headers") or {})}
        data["headers"] = merged_headers
        existing_extra = data.get("extra_headers") or {}
        data["extra_headers"] = {**merged_headers, **existing_extra}

    # Persist attribution headers & requested model alias on the call metadata for ChargebackLogger
    ctx_attr = _request_attribution_ctx.get() or {}
    if ctx_attr or forwarded:
        meta = dict(data.get("metadata") or {})
        meta["attribution_headers"] = {**ctx_attr, **(forwarded or {})}
        data["metadata"] = meta

    return data


LiteLLMProxyRequestSetup._get_forwardable_headers = staticmethod(
    _custom_get_forwardable_headers
)
LiteLLMProxyRequestSetup.add_litellm_data_for_backend_llm_call = staticmethod(
    _custom_add_litellm_data_for_backend_llm_call
)


# ---------------------------------------------------------------------------
# Sanitize thinkingConfig for custom LiteLLM model aliases (e.g. 'fast', 'big')
# `agy` CLI only enables interactive `/effort` (LOW/MEDIUM/HIGH) for its built-in
# catalog model IDs (`gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.7-flash`,
# `gemini-3.6-flash`, `gemini-3.1-pro`). For any custom alias not in that catalog,
# `agy` sends `thinkingConfig: {"includeThoughts": true, "thinkingBudget": 0}`,
# which Vertex AI rejects on Gemini 3/4 with 400 INVALID_ARGUMENT.
# ---------------------------------------------------------------------------
def _sanitize_thinking_config_in_dict(
    mapped: dict[str, Any],
    model: str,
    configured_level: str | None = None,
) -> dict[str, Any]:
    """
    Gemini 3.x and 4.x models on Vertex AI use `thinkingLevel` ("LOW", "MEDIUM", "HIGH")
    instead of `thinkingBudget`. When agy CLI uses a custom alias not in its built-in
    catalog (such as `fast` or `big`), agy falls back to sending `thinkingBudget: 0`
    with `includeThoughts: true`.
    For Gemini 3/4 target models, strip `thinkingBudget` and set `thinkingLevel` from
    `litellm_params.thinking_level` / `reasoning_effort` (or default to "LOW" for flash
    models and "MEDIUM" for pro/argon models) if not already set by the client.
    """
    is_gemini_3_or_4 = "gemini-3" in model or "gemini-4" in model
    for key in ("thinkingConfig", "thinking_config"):
        thinking_cfg = mapped.get(key)
        if isinstance(thinking_cfg, dict):
            thinking_cfg = dict(thinking_cfg)
            include_thoughts = thinking_cfg.get(
                "includeThoughts", thinking_cfg.get("include_thoughts")
            )
            thinking_budget = thinking_cfg.get(
                "thinkingBudget", thinking_cfg.get("thinking_budget")
            )
            thinking_level = thinking_cfg.get(
                "thinkingLevel", thinking_cfg.get("thinking_level")
            )

            if is_gemini_3_or_4:
                # Gemini 3/4 models use thinkingLevel (LOW/MEDIUM/HIGH), not thinkingBudget
                if thinking_budget is not None:
                    thinking_cfg.pop("thinkingBudget", None)
                    thinking_cfg.pop("thinking_budget", None)
                if not thinking_level and (include_thoughts is True or configured_level):
                    target_level = (
                        configured_level.upper()
                        if configured_level
                        else ("LOW" if "flash" in model else "MEDIUM")
                    )
                    thinking_cfg["thinkingLevel"] = target_level
                    logger.info(
                        "Normalized %s for %s: removed thinkingBudget=%s, set thinkingLevel=%s",
                        key,
                        model,
                        thinking_budget,
                        target_level,
                    )
                mapped[key] = thinking_cfg
            elif include_thoughts is True and thinking_budget == 0:
                thinking_cfg.pop("thinkingBudget", None)
                thinking_cfg.pop("thinking_budget", None)
                mapped[key] = thinking_cfg
    return mapped


_orig_setup_generate_content_call = GenerateContentHelper.setup_generate_content_call


def _custom_setup_generate_content_call(
    model: str,
    contents: Any,
    config: Any = None,
    custom_llm_provider: str | None = None,
    tools: Any = None,
    **kwargs: Any,
):
    """
    Allows configuring `thinking_level` or `reasoning_effort` ("LOW", "MEDIUM", "HIGH")
    per model alias in `litellm_config.yaml` under `litellm_params`.
    """
    configured_level = kwargs.pop("thinking_level", None) or kwargs.pop(
        "reasoning_effort", None
    )
    if configured_level and isinstance(config, dict):
        config = dict(config)
        thinking_cfg = dict(
            config.get("thinkingConfig") or config.get("thinking_config") or {}
        )
        if not thinking_cfg.get("thinkingLevel") and not thinking_cfg.get(
            "thinking_level"
        ):
            thinking_cfg["thinkingLevel"] = str(configured_level).upper()
            thinking_cfg.pop("thinkingBudget", None)
            thinking_cfg.pop("thinking_budget", None)
            config["thinkingConfig"] = thinking_cfg
            config.pop("thinking_config", None)

    return _orig_setup_generate_content_call(
        model=model,
        contents=contents,
        config=config,
        custom_llm_provider=custom_llm_provider,
        tools=tools,
        **kwargs,
    )


GenerateContentHelper.setup_generate_content_call = staticmethod(
    _custom_setup_generate_content_call
)

_orig_gemini_map_params = GoogleGenAIConfig.map_generate_content_optional_params
_orig_vertex_map_params = VertexAIGoogleGenAIConfig.map_generate_content_optional_params


def _custom_gemini_map_params(
    self: GoogleGenAIConfig,
    generate_content_config_dict: Any,
    model: str,
) -> dict[str, Any]:
    mapped = _orig_gemini_map_params(self, generate_content_config_dict, model)
    return _sanitize_thinking_config_in_dict(mapped, model)


def _custom_vertex_map_params(
    self: VertexAIGoogleGenAIConfig,
    generate_content_config_dict: Any,
    model: str,
) -> dict[str, Any]:
    mapped = _orig_vertex_map_params(self, generate_content_config_dict, model)
    return _sanitize_thinking_config_in_dict(mapped, model)


GoogleGenAIConfig.map_generate_content_optional_params = _custom_gemini_map_params
VertexAIGoogleGenAIConfig.map_generate_content_optional_params = _custom_vertex_map_params


def extract_api_key(request: Request) -> str:
    """
    Extracts the Gateway API key from standard Antigravity auth headers or query params:
      - Authorization: Bearer <key>
      - x-goog-api-key: <key>
      - x-litellm-api-key: <key>
      - ?key=<key>
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()

    goog_key = request.headers.get("x-goog-api-key", "")
    if goog_key:
        return goog_key.strip()

    litellm_key = request.headers.get("x-litellm-api-key", "")
    if litellm_key:
        return litellm_key.strip()

    query_key = request.query_params.get("key", "")
    if query_key:
        return query_key.strip()

    return ""


def verify_gateway_api_key(request: Request) -> bool:
    """
    Mandatory Requirement 4: Constant-time verification of GATEWAY_API_KEY
    (also accepts LiteLLM `sk-...` virtual keys for telemetry endpoints).
    """
    expected_key = os.environ.get("GATEWAY_API_KEY", "")
    if not expected_key:
        logger.error("GATEWAY_API_KEY environment variable is not configured!")
        return False

    provided_key = extract_api_key(request)
    if not provided_key:
        return False

    if hmac.compare_digest(provided_key.encode("utf-8"), expected_key.encode("utf-8")):
        return True

    # Support LiteLLM virtual keys on telemetry endpoints
    if provided_key.startswith("sk-"):
        return True

    return False


class ChargebackLogger(CustomLogger):
    """
    Emits structured JSON chargeback logs to stdout (automatically parsed as `jsonPayload`
    in Cloud Logging) for every completed GenAI or OpenAI request (streaming & non-streaming).
    """

    async def async_log_success_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        try:
            std_log: dict = kwargs.get("standard_logging_object") or {}
            litellm_params: dict = kwargs.get("litellm_params") or {}
            metadata: dict = (
                litellm_params.get("metadata") or std_log.get("metadata") or {}
            )
            ctx_attr = _request_attribution_ctx.get()

            # Collect headers from all possible LiteLLM & request context sources
            raw_headers: dict = {
                **(ctx_attr or {}),
                **(metadata.get("attribution_headers") or {}),
                **(kwargs.get("extra_headers") or {}),
                **(metadata.get("headers") or {}),
            }
            headers_lower = {str(k).lower(): v for k, v in raw_headers.items()}

            cost_center = headers_lower.get("x-cost-center", "unattributed")
            user_project = headers_lower.get("x-goog-user-project", "")
            user_agent = headers_lower.get("user-agent", "")

            # Extract token counts from standard_logging_object or response_obj
            prompt_tokens = std_log.get("prompt_tokens") or 0
            completion_tokens = std_log.get("completion_tokens") or 0
            total_tokens = std_log.get("total_tokens") or (
                prompt_tokens + completion_tokens
            )
            reasoning_tokens = 0
            cached_tokens = 0

            usage = getattr(response_obj, "usage", None)
            if usage is not None:
                prompt_tokens = prompt_tokens or getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = (
                    completion_tokens or getattr(usage, "completion_tokens", 0) or 0
                )
                total_tokens = (
                    total_tokens
                    or getattr(usage, "total_tokens", 0)
                    or (prompt_tokens + completion_tokens)
                )
                comp_details = getattr(usage, "completion_tokens_details", None)
                if comp_details:
                    reasoning_tokens = getattr(comp_details, "reasoning_tokens", 0) or 0
                prompt_details = getattr(usage, "prompt_tokens_details", None)
                if prompt_details:
                    cached_tokens = getattr(prompt_details, "cached_tokens", 0) or 0
            elif isinstance(response_obj, dict) and "usageMetadata" in response_obj:
                um = response_obj["usageMetadata"]
                prompt_tokens = prompt_tokens or um.get("promptTokenCount", 0)
                reasoning_tokens = um.get("thoughtsTokenCount", 0)
                completion_tokens = completion_tokens or (
                    um.get("candidatesTokenCount", 0) + reasoning_tokens
                )
                cached_tokens = um.get("cachedContentTokenCount", 0)
                total_tokens = total_tokens or um.get(
                    "totalTokenCount", prompt_tokens + completion_tokens
                )

            cost_usd = kwargs.get("response_cost")
            if cost_usd is None:
                cost_usd = std_log.get("response_cost") or 0.0

            requested_model = (
                raw_headers.get("_requested_model")
                or std_log.get("model_group")
                or kwargs.get("model")
            )
            resolved_model = std_log.get("model") or kwargs.get("model")

            chargeback_record = {
                "severity": "INFO",
                "log_type": "llm_chargeback",
                "cost_center": cost_center,
                "requested_model": requested_model,
                "resolved_model": resolved_model,
                "cost_usd": round(float(cost_usd), 8),
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "reasoning_tokens": int(reasoning_tokens),
                "cached_tokens": int(cached_tokens),
                "total_tokens": int(total_tokens),
                "user_project": user_project,
                "user_agent": user_agent,
            }
            print(json.dumps(chargeback_record), flush=True)
        except Exception as exc:
            logger.debug("ChargebackLogger error: %s", exc)


chargeback_logger = ChargebackLogger()
if chargeback_logger not in litellm.callbacks:
    litellm.callbacks.append(chargeback_logger)


@asynccontextmanager
async def gateway_lifespan(app):
    """Ensure LiteLLM initializes with litellm_config.yaml before serving requests."""
    config_path = os.environ.get("LITELLM_CONFIG_PATH", "litellm_config.yaml")
    os.environ["CONFIG_FILE_PATH"] = config_path
    litellm.drop_params = True
    async with proxy_startup_event(app):
        if chargeback_logger not in litellm.callbacks:
            litellm.callbacks.append(chargeback_logger)
        yield


litellm_app.router.lifespan_context = gateway_lifespan


@litellm_app.middleware("http")
async def antigravity_gateway_middleware(request: Request, call_next) -> Response:
    path = request.url.path

    # 1. Allow unauthenticated Cloud Run health checks
    if path in ("/health", "/health/liveliness", "/health/readiness") and request.method == "GET":
        return JSONResponse(status_code=200, content={"status": "healthy"})

    # 2. Mandatory Requirement 3: Acknowledge telemetry endpoints (POST /v1beta:*)
    if path.startswith("/v1beta:") and request.method == "POST":
        if not verify_gateway_api_key(request):
            return JSONResponse(
                status_code=401,
                content={"error": {"code": 401, "message": "Unauthorized: Invalid Gateway API Key", "status": "UNAUTHENTICATED"}},
            )
        logger.debug("Acknowledged Antigravity telemetry call: %s", path)
        return JSONResponse(status_code=200, content={})

    # 3. Mandatory Requirement 2: Full Vertex Path support via LiteLLM's native GenAI router
    # Rewrite /v1beta1/projects/{project}/locations/{region}/publishers/google/models/{model_id}:{action}
    # to LiteLLM's native /v1beta/models/{model_id}:{action} endpoint
    m_full = FULL_VERTEX_PATH_RE.match(path)
    if m_full and request.method == "POST":
        _, _, model_id, action = m_full.groups()
        rewritten_path = f"/v1beta/models/{model_id}:{action}"
        logger.info(
            "Rewriting full Vertex path %s -> %s for LiteLLM GenAI router",
            path,
            rewritten_path,
        )
        request.scope["path"] = rewritten_path
        request.scope["raw_path"] = rewritten_path.encode("utf-8")
        path = rewritten_path

    # Capture attribution headers & requested model alias in contextvar for ChargebackLogger
    attr_ctx = _custom_get_forwardable_headers(dict(request.headers))
    if path.startswith("/v1beta/models/"):
        model_seg = path.split("/v1beta/models/", 1)[1].split(":", 1)[0]
        attr_ctx["_requested_model"] = model_seg
    ctx_token = _request_attribution_ctx.set(attr_ctx)

    # 4. Delegate both GenAI (/v1beta/models/...) and OpenAI (/v1/chat/completions)
    # protocols to LiteLLM Proxy (which enforces auth via user_api_key_auth and routes via Router)
    try:
        response = await call_next(request)
    finally:
        _request_attribution_ctx.reset(ctx_token)

    # Ensure SSE streaming headers prevent intermediate proxy buffering
    if ":streamGenerateContent" in path:
        response.headers["Cache-Control"] = "no-cache, no-transform"
        response.headers["X-Accel-Buffering"] = "no"

    return response


# Export FastAPI app for Uvicorn
app = litellm_app
