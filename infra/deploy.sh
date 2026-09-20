#!/usr/bin/env bash
#
# Deploy the OpsPilot demo to Azure Container Apps.
#
#   ./infra/deploy.sh              # create or update everything
#   ./infra/deploy.sh --tear-down  # delete the whole resource group
#
# Idempotent: every step checks for what it is about to create, so re-running after a failure resumes
# rather than duplicating. Progress and the final URL are appended to infra/.deploy.log.
#
# Why a script and not Terraform, stated plainly: Terraform is the right long-term answer and is still
# the plan for M9's second half (see infra/README.md). A script I can run and verify today produces a
# working link; a half-written Terraform module with no state backend produces an argument. The script is
# written so the same resource graph can be transcribed into Terraform later without re-deciding anything.
#
# Cost guardrails are part of the deployment, not an afterthought: the API's run and daily cost caps are
# set low, and Container Apps scale to zero, because a public link with an API key behind it is a way to
# spend money unattended.

set -euo pipefail

# This machine advertises an IPv6 route that is dead: az tries it first and every call that needs a
# token stalls until timeout instead of falling back to IPv4. `curl -4 ... ` succeeds, plain `az ...`
# hangs, which is the signature. The patch forces AF_INET at the socket layer for any process that has it
# on PYTHONPATH, so the script carries the fix rather than depending on the caller's shell.
if [ -d "${HOME}/.local/share/ipv4patch" ]; then
  export PYTHONPATH="${HOME}/.local/share/ipv4patch${PYTHONPATH:+:${PYTHONPATH}}"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${REPO_ROOT}/infra/.deploy.log"
SECRETS="${REPO_ROOT}/infra/.deploy-secrets"

SUBSCRIPTION="${OPSPILOT_DEPLOY_SUBSCRIPTION:-dc0945fe-6315-46cd-a5f3-fd89a39d66dd}"
LOCATION="${OPSPILOT_DEPLOY_LOCATION:-francecentral}"
PREFIX="opspilot"
RG="rg-${PREFIX}-demo"
ACR="acr${PREFIX}demo"
ENVIRONMENT="cae-${PREFIX}"
API_APP="ca-${PREFIX}-api"
LAB_APP="ca-${PREFIX}-lab"
PG_SERVER="psql-${PREFIX}-demo"
PG_DB="opspilot"
LOGS="log-${PREFIX}"

log() { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG"; }
fail() { log "FAILED: $*"; exit 1; }

tgz_secrets() {
  # Credentials live in a gitignored file, never in the script, never in the log.
  [ -f "$SECRETS" ] && { set -a; . "$SECRETS"; set +a; }
}

save_secret() {
  local key="$1" value="$2"
  touch "$SECRETS"; chmod 600 "$SECRETS"
  if grep -q "^${key}=" "$SECRETS" 2>/dev/null; then
    python3 - "$SECRETS" "$key" "$value" <<'PY'
import sys, pathlib
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
lines = [f"{key}={value}\n" if l.startswith(f"{key}=") else l for l in pathlib.Path(path).read_text().splitlines(keepends=True)]
pathlib.Path(path).write_text("".join(lines))
PY
  else
    printf '%s=%s\n' "$key" "$value" >> "$SECRETS"
  fi
}

exists_rg() { az group show --name "$RG" >/dev/null 2>&1; }

tear_down() {
  log "deleting resource group ${RG} (this removes every resource in it)"
  az group delete --name "$RG" --yes --no-wait
  log "delete requested; check with: az group show --name ${RG}"
}

# --- preflight ------------------------------------------------------------------------------------

trap 'fail "line $LINENO"' ERR
mkdir -p "$(dirname "$LOG")"; : >> "$LOG"
log "=== deploy started (subscription ${SUBSCRIPTION}, location ${LOCATION}) ==="

az account set --subscription "$SUBSCRIPTION" >/dev/null || fail "cannot select subscription"
log "subscription: $(az account show --query name -o tsv)"

# The student offer carries a "Allowed resource deployment regions" policy. Deploying outside its list
# fails only at the first *resource* creation — after providers have registered — which wasted twenty
# minutes. Read the policy up front instead and refuse before creating anything.
ALLOWED=$(az policy assignment list --scope "/subscriptions/${SUBSCRIPTION}" \
  --query "[?displayName=='Allowed resource deployment regions'].parameters.listOfAllowedLocations.value[]" -o tsv 2>/dev/null || true)
if [ -n "$ALLOWED" ]; then
  log "policy allows these regions: $(echo $ALLOWED | tr '\n' ' ')"
  if ! echo "$ALLOWED" | tr -d '\r' | grep -qx "$LOCATION"; then
    fail "location ${LOCATION} is not permitted by the subscription's region policy; choose one of: $(echo $ALLOWED | tr '\n' ' ') (set OPSPILOT_DEPLOY_LOCATION)"
  fi
  log "location ${LOCATION} is permitted"
fi

if [ "${1:-}" = "--tear-down" ]; then tear_down; exit 0; fi

if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -f "${REPO_ROOT}/.env" ]; then
  set -a; . "${REPO_ROOT}/.env"; set +a
fi

# --- providers ------------------------------------------------------------------------------------

for ns in Microsoft.App Microsoft.ContainerRegistry Microsoft.DBforPostgreSQL Microsoft.OperationalInsights; do
  state=$(az provider show --namespace "$ns" --query registrationState -o tsv 2>/dev/null || echo Unknown)
  if [ "$state" != "Registered" ]; then
    log "registering provider ${ns} (this can take a minute)"
    az provider register --namespace "$ns" --wait || fail "provider ${ns} could not be registered"
  fi
  log "provider ${ns}: $(az provider show --namespace "$ns" --query registrationState -o tsv)"
done

# --- resource group and registry ------------------------------------------------------------------

if exists_rg; then log "resource group ${RG} already exists"; else
  log "creating resource group ${RG} in ${LOCATION}"
  az group create --name "$RG" --location "$LOCATION" \
    --tags project=opspilot env=demo managed-by=deploy-script >/dev/null
fi

if az acr show --name "$ACR" --resource-group "$RG" >/dev/null 2>&1; then
  log "registry ${ACR} already exists"
else
  log "creating container registry ${ACR} (Basic) in ${LOCATION}"
  # --location is not optional: without it the registry inherits the resource group's location, and a
  # resource group created before a region was corrected makes the policy refuse the registry.
  az acr create --name "$ACR" --resource-group "$RG" --sku Basic --location "$LOCATION" \
    --admin-enabled true >/dev/null
fi

# Idempotent: admin access is needed for the container apps to pull, and it is not implied by the
# registry merely existing.
az acr update --name "$ACR" --admin-enabled true >/dev/null
log "registry admin access enabled"

ACR_USER=$(az acr credential show --name "$ACR" --query username -o tsv)
ACR_PASS=$(az acr credential show --name "$ACR" --query 'passwords[0].value' -o tsv)

# `az acr build` is not usable here: the student offer rejects ACR Tasks outright
# (TasksOperationsNotAllowed), and no amount of retrying fixes an offer-level restriction. Docker Desktop
# can cross-build, so the image is built locally for linux/amd64 — Container Apps refuses an arm64 image,
# and an Apple Silicon `docker build` produces exactly that.
log "building linux/amd64 image locally (ACR Tasks are not permitted on this subscription)"
# Admin access does not take effect the instant it is enabled: reading the credential and logging in
# immediately after produced "failed to fetch oauth token: denied". Retry briefly instead of assuming.
for attempt in 1 2 3 4; do
  if echo "$ACR_PASS" | docker login "${ACR}.azurecr.io" --username "$ACR_USER" --password-stdin >>"$LOG" 2>&1; then
    log "docker login succeeded (attempt ${attempt})"
    break
  fi
  log "docker login attempt ${attempt} refused; registry admin access may still be propagating"
  sleep 20
  if [ "$attempt" = "4" ]; then fail "docker login to ${ACR}.azurecr.io failed after 4 attempts"; fi
done
if docker buildx version >/dev/null 2>&1; then
  # --load then a plain `docker push`, deliberately: buildx's own --push resolves registry credentials
  # inside the builder, which on macOS cannot read the keychain entry `docker login` wrote, and the push
  # is refused with "failed to fetch oauth token: denied" a second after a successful login. Loading the
  # image into the local daemon and pushing with the normal client uses the credentials that work.
  docker buildx build --platform linux/amd64 --load -t "${ACR}.azurecr.io/${PREFIX}:latest" \
    -f "${REPO_ROOT}/Dockerfile" "${REPO_ROOT}" >>"$LOG" 2>&1 || fail "image build failed (see ${LOG})"
  docker push "${ACR}.azurecr.io/${PREFIX}:latest" >>"$LOG" 2>&1 || fail "image push failed (see ${LOG})"
else
  docker build --platform linux/amd64 -t "${ACR}.azurecr.io/${PREFIX}:latest" \
    -f "${REPO_ROOT}/Dockerfile" "${REPO_ROOT}" >>"$LOG" 2>&1 || fail "image build failed (see ${LOG})"
  docker push "${ACR}.azurecr.io/${PREFIX}:latest" >>"$LOG" 2>&1 || fail "image push failed (see ${LOG})"
fi
log "image pushed: ${ACR}.azurecr.io/${PREFIX}:latest"

# --- database -------------------------------------------------------------------------------------

if az postgres flexible-server show --name "$PG_SERVER" --resource-group "$RG" >/dev/null 2>&1; then
  log "postgres ${PG_SERVER} already exists"
else
  tgz_secrets
  PG_PASSWORD="${PG_PASSWORD:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')}"
  save_secret PG_PASSWORD "$PG_PASSWORD"
  log "creating postgres flexible server ${PG_SERVER} (Burstable B1ms, 32GB) — this takes a few minutes"
  az postgres flexible-server create \
    --name "$PG_SERVER" --resource-group "$RG" --location "$LOCATION" \
    --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 \
    --admin-user opspilot --admin-password "$PG_PASSWORD" \
    --public-access 0.0.0.0 --yes >/dev/null
fi

tgz_secrets
PG_PASSWORD="${PG_PASSWORD:?missing PG_PASSWORD in ${SECRETS}}"
PG_HOST="${PG_SERVER}.postgres.database.azure.com"

if [ "$(az postgres flexible-server db list --server-name "$PG_SERVER" --resource-group "$RG" \
        --query "[?name=='${PG_DB}'] | length(@)" -o tsv)" = "0" ]; then
  log "creating database ${PG_DB}"
  az postgres flexible-server db create --server-name "$PG_SERVER" --resource-group "$RG" \
    --database-name "$PG_DB" >/dev/null
fi

# The migrations run from here, so this machine needs a hole in the firewall; it is closed again below.
MY_IP=$(curl -4 -s https://api.ipify.org || true)
if [ -n "$MY_IP" ]; then
  log "allowing this machine (${MY_IP}) through the postgres firewall for the migration"
  az postgres flexible-server firewall-rule create --name "deployer" --resource-group "$RG" \
    --server-name "$PG_SERVER" --start-ip-address "$MY_IP" --end-ip-address "$MY_IP" >/dev/null || true
fi

log "applying migrations to ${PG_HOST}"
(cd "$REPO_ROOT" && OPSPILOT_DATABASE_URL="postgresql+asyncpg://opspilot:${PG_PASSWORD}@${PG_HOST}:5432/${PG_DB}" \
  uv run alembic upgrade head) >>"$LOG" 2>&1 || fail "migrations failed (see ${LOG})"
log "migrations applied"

if [ -n "$MY_IP" ]; then
  az postgres flexible-server firewall-rule delete --name "deployer" --resource-group "$RG" \
    --server-name "$PG_SERVER" --yes >/dev/null 2>&1 || true
  log "closed the deployer firewall rule"
fi

# --- container apps environment -------------------------------------------------------------------

if az containerapp env show --name "$ENVIRONMENT" --resource-group "$RG" >/dev/null 2>&1; then
  log "container apps environment ${ENVIRONMENT} already exists"
else
  if ! az monitor log-analytics workspace show --workspace-name "$LOGS" --resource-group "$RG" >/dev/null 2>&1; then
    log "creating log analytics workspace ${LOGS}"
    az monitor log-analytics workspace create --workspace-name "$LOGS" --resource-group "$RG" \
      --location "$LOCATION" >/dev/null
  fi
  LOGS_ID=$(az monitor log-analytics workspace show --workspace-name "$LOGS" --resource-group "$RG" --query customerId -o tsv)
  LOGS_KEY=$(az monitor log-analytics workspace get-shared-keys --workspace-name "$LOGS" --resource-group "$RG" --query primarySharedKey -o tsv)
  log "creating container apps environment ${ENVIRONMENT}"
  az containerapp env create --name "$ENVIRONMENT" --resource-group "$RG" --location "$LOCATION" \
    --logs-workspace-id "$LOGS_ID" --logs-workspace-key "$LOGS_KEY" >/dev/null
fi

# --- the incident lab (internal ingress: it is a target, not a product) ---------------------------

LAB_TOKEN="${LAB_ADMIN_TOKEN:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(18))')}"
save_secret LAB_ADMIN_TOKEN "$LAB_TOKEN"

deploy_app() {
  local name="$1" target_port="$2" external="$3"; shift 3
  if az containerapp show --name "$name" --resource-group "$RG" >/dev/null 2>&1; then
    log "updating app ${name}"
    az containerapp update --name "$name" --resource-group "$RG" \
      --image "${ACR}.azurecr.io/${PREFIX}:latest" "$@" >/dev/null
  else
    log "creating app ${name}"
    az containerapp create --name "$name" --resource-group "$RG" --environment "$ENVIRONMENT" \
      --image "${ACR}.azurecr.io/${PREFIX}:latest" \
      --registry-server "${ACR}.azurecr.io" --registry-username "$ACR_USER" --registry-password "$ACR_PASS" \
      --target-port "$target_port" --ingress "$external" \
      --min-replicas 0 --max-replicas 2 --cpu 0.5 --memory 1.0Gi "$@" >/dev/null
  fi
}

deploy_app "$LAB_APP" 8081 internal \
  --command "uvicorn" "opspilot.lab.service:app" "--host" "0.0.0.0" "--port" "8081" \
  --env-vars "OPSPILOT_LAB_ADMIN_TOKEN=${LAB_TOKEN}"

LAB_FQDN=$(az containerapp show --name "$LAB_APP" --resource-group "$RG" \
  --query properties.configuration.ingress.fqdn -o tsv)
log "lab reachable inside the environment at http://${LAB_FQDN}"

# --- the platform ---------------------------------------------------------------------------------

API_ENV=(
  "OPSPILOT_ENVIRONMENT=dev"
  "OPSPILOT_DATABASE_URL=postgresql+asyncpg://opspilot:${PG_PASSWORD}@${PG_HOST}:5432/${PG_DB}"
  "OPSPILOT_LAB_BASE_URL=http://${LAB_FQDN}"
  "OPSPILOT_LAB_ADMIN_TOKEN=${LAB_TOKEN}"
  # Cost guardrails: a public link with a model key behind it must refuse to spend without limit.
  "OPSPILOT_RUN_COST_CAP_EUR=0.02"
  "OPSPILOT_DAILY_COST_CAP_EUR=1.00"
  "OPSPILOT_LOG_LEVEL=INFO"
)
if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
  log "a DeepSeek key is present: live runs are enabled, capped at 0.02 EUR per run and 1.00 EUR per day"
  API_ENV+=("DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY}")
else
  log "no model key present: the demo will run on the deterministic provider only"
fi

deploy_app "$API_APP" 8000 external --env-vars "${API_ENV[@]}"

API_FQDN=$(az containerapp show --name "$API_APP" --resource-group "$RG" \
  --query properties.configuration.ingress.fqdn -o tsv)

log "=== deploy finished ==="
log "URL: https://${API_FQDN}"
log "lab:  https://${LAB_APP}.${ENVIRONMENT}.azurecontainerapps.io (internal only)"
log "tear down with: ./infra/deploy.sh --tear-down"
log "stop the database between demos: az postgres flexible-server stop --name ${PG_SERVER} --resource-group ${RG}"
