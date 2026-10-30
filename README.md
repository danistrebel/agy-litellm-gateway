# Antigravity LiteLLM Enterprise Gateway POC

A ready-to-deploy **LiteLLM Enterprise Gateway** for [Google Antigravity](https://docs.cloud.google.com/gemini/enterprise/docs/ai-developer-tools/deploy-enterprise-gateway) backed by **Vertex AI (`aiplatform.googleapis.com`)** and hosted on **Google Cloud Run**.

---

## How This Satisfies All Enterprise Gateway Requirements

Per the [Antigravity Enterprise Gateway specification](https://docs.cloud.google.com/gemini/enterprise/docs/ai-developer-tools/deploy-enterprise-gateway), an enterprise gateway must fulfill several strict requirements that stock LLM proxies often miss. This project combines **LiteLLM Proxy** (`litellm_config.yaml`) with a lightweight ASGI middleware (`gateway_middleware.py`):

| Requirement | Implementation in This Repo |
| :--- | :--- |
| **1. Preserve Client Attribution Headers**<br>(`User-Agent: antigravity/VERSION (aidev_client; auth_method=gateway)`, `X-Goog-Api-Client`, `X-Cost-Center`) | Preserved verbatim in [`gateway_middleware.py`](gateway_middleware.py) for `genai` routes and enabled via `forward_client_headers_to_llm_api: true` in [`litellm_config.yaml`](litellm_config.yaml), plus automatic `X-Goog-User-Project` injection. |
| **2. Unbuffered SSE Streaming**<br>(`:streamGenerateContent?alt=sse` and `/v1/chat/completions`) | Raw chunk streaming via `httpx.AsyncClient.aiter_raw()` with `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`. |
| **3. Acknowledge Telemetry Endpoints**<br>(`POST /v1beta:*` → `HTTP 200 OK` `{}`) | Handled in [`gateway_middleware.py`](gateway_middleware.py) (stock LiteLLM would otherwise return `404 Not Found`). |
| **4. Both `genai` and `openai` Wire Protocols** | - **`genai` (default, required by JetBrains/Zed ACP server)**: Supports both `/v1beta/models/{MODEL}:(stream)GenerateContent` and `/v1beta1/projects/{PROJECT}/locations/{REGION}/publishers/google/models/{MODEL}:(stream)GenerateContent`.<br>- **`openai`**: Handled natively by LiteLLM's `POST /v1/chat/completions` routed to `vertex_ai/*`. |
| **5. Bearer API Key Auth (`Authorization: Bearer <KEY>`)** | Validated against `GATEWAY_API_KEY` (mounted from Secret Manager), while Cloud Run is deployed with `--allow-unauthenticated` so Google Front End does not reject the non-OIDC `Authorization` header. |

---

## Project Structure

- [`litellm_config.yaml`](litellm_config.yaml) — LiteLLM Proxy model routing (`vertex_ai/gemini-*`), timeouts, and header-forwarding settings.
- [`gateway_middleware.py`](gateway_middleware.py) — FastAPI/ASGI wrapper around LiteLLM Proxy for `/v1beta:*` telemetry and zero-buffering `genai` wire-protocol pass-through with strict attribution header preservation.
- [`Dockerfile`](Dockerfile) — Container image definition for Cloud Run (`PORT=8080`, 60-minute keep-alive).

---

## Quickstart

### 1. Run Locally

Ensure you have Application Default Credentials (`gcloud auth application-default login`) and run:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export VERTEX_PROJECT_ID="$(gcloud config get-value project)"
export VERTEX_LOCATION="us-central1"
export GATEWAY_API_KEY="local-dev-key-123"

uvicorn gateway_middleware:app --host 0.0.0.0 --port 8080
```

In another terminal, test all 3 endpoint types:

```bash
export AGY_LLM_GATEWAY_URL="http://localhost:8080"
export AGY_LLM_GATEWAY_API_KEY="local-dev-key-123"
./test-gateway.sh
```

### 2. Deploy to Cloud Run

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
export VERTEX_LOCATION="us-central1"
export INGRESS="internal"

./deploy-cloud-run.sh
```

### 3. Configure Antigravity Clients

Retrieve your generated gateway API key from Secret Manager:

```bash
gcloud secrets versions access latest --secret=agy-gateway-api-key --project="${PROJECT_ID}"
```

#### Option A: Environment Variables

Set the Environment variables:

```bash
export AGY_LLM_GATEWAY_URL="<CLOUD_RUN_>"   
export AGY_LLM_GATEWAY_API_KEY="<API_KEY>"
export AGY_LLM_GATEWAY_MODELS="gemini-3.8-flash,gemini-3.7-flash,gemini-4-argon"
```

And run Antigravity:

```bash
agy -p "Explain binary search in Go"
```

#### Option B: MDM `admin_settings.json` (macOS / Linux / Windows)
Create an `admin_settings.json` with your Cloud Run URL and API key, and install it to:
- **macOS**: `/Library/Application Support/Antigravity/admin_settings.json`
- **Linux**: `/etc/antigravity/admin_settings.json`
- **Windows**: `%ProgramData%\Antigravity\admin_settings.json`
