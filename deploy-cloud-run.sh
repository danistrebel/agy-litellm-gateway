#!/usr/bin/env bash
# Deploy Antigravity LiteLLM Enterprise Gateway to Google Cloud Run
# Reference: https://docs.cloud.google.com/gemini/enterprise/docs/ai-developer-tools/deploy-enterprise-gateway

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"
REGION="${REGION:-us-central1}"
VERTEX_LOCATION="${VERTEX_LOCATION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-agy-litellm-gateway}"
SA_NAME="${SA_NAME:-agy-gateway-sa}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SECRET_NAME="${SECRET_NAME:-agy-gateway-api-key}"
CONFIG_SECRET_NAME="${CONFIG_SECRET_NAME:-agy-gateway-litellm-config}"
CONFIG_FILE="${CONFIG_FILE:-litellm_config.yaml}"
# Set INGRESS="internal" for corporate VPC/VPN only (recommended for prod), or "all" for initial testing
INGRESS="${INGRESS:-all}"
# Optional ILB configuration: specify NETWORK to provision an Internal Application Load Balancer
NETWORK="${NETWORK:-}"
SUBNET="${SUBNET:-}"
ILB_IP_ADDRESS="${ILB_IP_ADDRESS:-}"

echo "==> Using Project: ${PROJECT_ID}, Region: ${REGION}, Vertex Location: ${VERTEX_LOCATION}"

echo "==> 1. Enabling required Google Cloud APIs (if not already enabled)..."
gcloud services enable \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  compute.googleapis.com \
  --project="${PROJECT_ID}" || echo "    Note: Continuing if APIs are already enabled..."

echo "==> 2. Ensuring Gateway Service Account (${SA_EMAIL}) exists..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --project="${PROJECT_ID}" \
    --display-name="Antigravity Enterprise Gateway"
fi

echo "==> 3. Granting roles/aiplatform.user to ${SA_EMAIL}..."
# Service account propagation across IAM replication can take a few seconds
for i in {1..5}; do
  if gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/aiplatform.user" \
    --condition=None >/dev/null 2>&1; then
    echo "    IAM binding applied successfully."
    break
  fi
  echo "    Waiting for service account IAM propagation (attempt $i/5)..."
  sleep 5
done

echo "==> 4. Ensuring Gateway Secrets exist in Secret Manager..."
# API Key secret
if ! gcloud secrets describe "${SECRET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  openssl rand -base64 32 | tr -d '\n' | gcloud secrets create "${SECRET_NAME}" \
    --project="${PROJECT_ID}" \
    --data-file=-
  echo "    Created new secret: ${SECRET_NAME}"
else
  echo "    Secret ${SECRET_NAME} already exists."
fi

gcloud secrets add-iam-policy-binding "${SECRET_NAME}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

# LiteLLM Config secret
if ! gcloud secrets describe "${CONFIG_SECRET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud secrets create "${CONFIG_SECRET_NAME}" \
    --project="${PROJECT_ID}" \
    --data-file="${CONFIG_FILE}"
  echo "    Created new secret: ${CONFIG_SECRET_NAME} from ${CONFIG_FILE}"
else
  gcloud secrets versions add "${CONFIG_SECRET_NAME}" \
    --project="${PROJECT_ID}" \
    --data-file="${CONFIG_FILE}"
  echo "    Added new version to existing secret: ${CONFIG_SECRET_NAME} from ${CONFIG_FILE}"
fi

gcloud secrets add-iam-policy-binding "${CONFIG_SECRET_NAME}" \
  --project="${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" >/dev/null

echo "==> 5. Deploying ${SERVICE_NAME} to Cloud Run..."
# IMPORTANT: We MUST use --allow-unauthenticated at the Cloud Run IAM layer because
# Antigravity sends `Authorization: Bearer GATEWAY_API_KEY`. If Cloud Run IAM invoker
# checks are enabled, Google Front End intercepts the Authorization header expecting
# a Google OIDC token and rejects the request before it reaches LiteLLM.
gcloud run deploy "${SERVICE_NAME}" \
  --source=. \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --allow-unauthenticated \
  --ingress="${INGRESS}" \
  --timeout=3600 \
  --cpu=1 \
  --memory=1Gi \
  --no-cpu-throttling \
  --set-env-vars="VERTEX_PROJECT_ID=${PROJECT_ID},VERTEX_LOCATION=${VERTEX_LOCATION},LITELLM_CONFIG_PATH=/secrets/litellm_config.yaml" \
  --set-secrets="GATEWAY_API_KEY=${SECRET_NAME}:latest,/secrets/litellm_config.yaml=${CONFIG_SECRET_NAME}:latest"

GATEWAY_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"

ILB_IP=""
if [[ -n "${NETWORK}" ]]; then
  echo ""
  echo "==> 6. Provisioning Regional Internal Application Load Balancer for ${SERVICE_NAME} in network '${NETWORK}'..."
  NEG_NAME="${SERVICE_NAME}-serverless-neg"
  BACKEND_NAME="${SERVICE_NAME}-ilb-backend"
  URL_MAP_NAME="${SERVICE_NAME}-ilb-url-map"
  HTTP_PROXY_NAME="${SERVICE_NAME}-ilb-proxy"
  FORWARDING_RULE_NAME="${SERVICE_NAME}-ilb-fr"

  # 6.1 Create or reuse Regional Serverless Network Endpoint Group (NEG)
  if ! gcloud compute network-endpoint-groups describe "${NEG_NAME}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "    Creating Serverless NEG [${NEG_NAME}]..."
    gcloud compute network-endpoint-groups create "${NEG_NAME}" \
      --project="${PROJECT_ID}" \
      --region="${REGION}" \
      --network-endpoint-type=serverless \
      --cloud-run-service="${SERVICE_NAME}"
  else
    echo "    Serverless NEG [${NEG_NAME}] already exists."
  fi

  # 6.2 Create or reuse Regional Backend Service with INTERNAL_MANAGED load balancing scheme
  if ! gcloud compute backend-services describe "${BACKEND_NAME}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "    Creating Regional Internal Backend Service [${BACKEND_NAME}]..."
    gcloud compute backend-services create "${BACKEND_NAME}" \
      --project="${PROJECT_ID}" \
      --region="${REGION}" \
      --load-balancing-scheme=INTERNAL_MANAGED \
      --protocol=HTTP
  else
    echo "    Regional Backend Service [${BACKEND_NAME}] already exists."
  fi

  # Add NEG to Backend Service if not attached
  if ! gcloud compute backend-services describe "${BACKEND_NAME}" --region="${REGION}" --project="${PROJECT_ID}" --format="value(backends[].group)" | grep -q "${NEG_NAME}"; then
    echo "    Attaching NEG to Backend Service..."
    gcloud compute backend-services add-backend "${BACKEND_NAME}" \
      --project="${PROJECT_ID}" \
      --region="${REGION}" \
      --network-endpoint-group="${NEG_NAME}" \
      --network-endpoint-group-region="${REGION}"
  fi

  # 6.3 Create or reuse Regional URL Map
  if ! gcloud compute url-maps describe "${URL_MAP_NAME}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "    Creating Regional URL Map [${URL_MAP_NAME}]..."
    gcloud compute url-maps create "${URL_MAP_NAME}" \
      --project="${PROJECT_ID}" \
      --region="${REGION}" \
      --default-service="${BACKEND_NAME}"
  else
    echo "    Regional URL Map [${URL_MAP_NAME}] already exists."
  fi

  # 6.4 Create or reuse Regional Target HTTP Proxy
  if ! gcloud compute target-http-proxies describe "${HTTP_PROXY_NAME}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "    Creating Regional Target HTTP Proxy [${HTTP_PROXY_NAME}]..."
    gcloud compute target-http-proxies create "${HTTP_PROXY_NAME}" \
      --project="${PROJECT_ID}" \
      --region="${REGION}" \
      --url-map="${URL_MAP_NAME}" \
      --url-map-region="${REGION}"
  else
    echo "    Regional Target HTTP Proxy [${HTTP_PROXY_NAME}] already exists."
  fi

  # 6.5 Create or reuse Regional Forwarding Rule
  # If SUBNET is not explicitly specified for a custom-mode network, select the first non-proxy subnet in region
  if [[ -z "${SUBNET}" ]]; then
    SUBNET="$(gcloud compute networks subnets list \
      --network="${NETWORK}" \
      --regions="${REGION}" \
      --project="${PROJECT_ID}" \
      --filter="purpose != REGIONAL_MANAGED_PROXY" \
      --format="value(name)" | head -n 1)"
    echo "    Auto-detected subnetwork for forwarding rule: ${SUBNET}"
  fi

  if ! gcloud compute forwarding-rules describe "${FORWARDING_RULE_NAME}" --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "    Creating Regional Internal Forwarding Rule [${FORWARDING_RULE_NAME}]..."
    FR_ARGS=(
      "${FORWARDING_RULE_NAME}"
      --project="${PROJECT_ID}"
      --region="${REGION}"
      --load-balancing-scheme=INTERNAL_MANAGED
      --network="${NETWORK}"
      --subnet="${SUBNET}"
      --target-http-proxy="${HTTP_PROXY_NAME}"
      --target-http-proxy-region="${REGION}"
      --ports=80
    )
    if [[ -n "${ILB_IP_ADDRESS}" ]]; then
      FR_ARGS+=(--address="${ILB_IP_ADDRESS}")
    fi
    gcloud compute forwarding-rules create "${FR_ARGS[@]}"
  else
    echo "    Regional Internal Forwarding Rule [${FORWARDING_RULE_NAME}] already exists."
  fi

  ILB_IP="$(gcloud compute forwarding-rules describe "${FORWARDING_RULE_NAME}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(IPAddress)')"
fi

echo ""
echo "================================================================================"
echo "Deployment complete!"
echo "Cloud Run URL: ${GATEWAY_URL}"
if [[ -n "${ILB_IP}" ]]; then
  echo "Internal LB IP: http://${ILB_IP} (VPC: ${NETWORK})"
fi
echo "Retrieve key : gcloud secrets versions access latest --secret=${SECRET_NAME} --project=${PROJECT_ID}"
echo "================================================================================"
