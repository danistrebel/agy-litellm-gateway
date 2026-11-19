#!/usr/bin/env bash
# Test the Antigravity LiteLLM Enterprise Gateway endpoints directly using curl
# Tests:
#   1. Background telemetry endpoint (`POST /v1beta:logTelemetry`)
#   2. Gemini `genai` wire protocol SSE streaming (`POST /v1beta/models/...:streamGenerateContent?alt=sse`)
#   3. OpenAI `openai` wire protocol SSE streaming (`POST /v1/chat/completions`)

set -euo pipefail

GATEWAY_URL="${AGY_LLM_GATEWAY_URL:-http://localhost:8080}"
GATEWAY_API_KEY="${AGY_LLM_GATEWAY_API_KEY:-test-key}"
MODEL_ID="${MODEL_ID:-gemini-2.5-flash}"

echo "==> 1. Testing Telemetry Endpoint (POST ${GATEWAY_URL}/v1beta:logTelemetry)"
curl -i -s -X POST "${GATEWAY_URL}/v1beta:logTelemetry" \
  -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "User-Agent: antigravity/1.2.15 (aidev_client; auth_method=gateway)" \
  -d '{"events": []}'
echo -e "\n"

echo "==> 2. Testing Default 'genai' Wire Protocol Streaming (POST ${GATEWAY_URL}/v1beta/models/${MODEL_ID}:streamGenerateContent?alt=sse)"
curl -N -X POST "${GATEWAY_URL}/v1beta/models/${MODEL_ID}:streamGenerateContent?alt=sse" \
  -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "User-Agent: antigravity/1.2.15 (aidev_client; auth_method=gateway)" \
  -H "X-Goog-Api-Client: agy-cli/1.2.15" \
  -d '{"contents": [{"role": "user", "parts": [{"text": "Reply with PONG"}]}]}'
echo -e "\n"

echo "==> 3. Testing 'openai' Wire Protocol Streaming via LiteLLM (POST ${GATEWAY_URL}/v1/chat/completions)"
curl -N -X POST "${GATEWAY_URL}/v1/chat/completions" \
  -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
  -H "Content-Type: application/json" \
  -H "User-Agent: antigravity/1.2.15 (aidev_client; auth_method=gateway)" \
  -d "{
    \"model\": \"${MODEL_ID}\",
    \"stream\": true,
    \"messages\": [{\"role\": \"user\", \"content\": \"Reply with PONG\"}]
  }"
echo -e "\n"

if [[ "${TEST_VIRTUAL_KEYS:-false}" == "true" ]]; then
  echo "==> 4. Testing LiteLLM Virtual Key Generation & Auth (POST ${GATEWAY_URL}/key/generate)"
  KEY_RESPONSE="$(curl -s -f -X POST "${GATEWAY_URL}/key/generate" \
    -H "Authorization: Bearer ${GATEWAY_API_KEY}" \
    -H "Content-Type: application/json" \
    -d '{
      "key_alias": "test-virtual-key",
      "max_budget": 5.0,
      "budget_duration": "30d",
      "metadata": {"cost_center": "CC-9999-TEST"}
    }')"
  VIRTUAL_KEY="$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["key"])' "${KEY_RESPONSE}")"
  echo "    Minted Virtual Key: ${VIRTUAL_KEY:0:12}..."

  echo "    4a. Verifying Telemetry Endpoint with Virtual Key..."
  curl -i -s -X POST "${GATEWAY_URL}/v1beta:logTelemetry" \
    -H "Authorization: Bearer ${VIRTUAL_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"events": []}'
  echo -e "\n"

  echo "    4b. Verifying GenAI Streaming with Virtual Key..."
  curl -N -X POST "${GATEWAY_URL}/v1beta/models/${MODEL_ID}:streamGenerateContent?alt=sse" \
    -H "Authorization: Bearer ${VIRTUAL_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"contents": [{"role": "user", "parts": [{"text": "Reply with PONG"}]}]}'
  echo -e "\n"
fi
