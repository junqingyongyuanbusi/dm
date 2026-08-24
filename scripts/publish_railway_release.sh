#!/usr/bin/env bash
set -Eeuo pipefail

readonly RAILWAY_API_URL="https://backboard.railway.com/graphql/v2"
readonly RAILWAY_PROJECT_ID="abcf3199-e5ac-415b-a22e-062206390331"
readonly RAILWAY_ENVIRONMENT_ID="db0d6750-eb77-40ee-8a79-f706cd1f828a"
readonly RAILWAY_ENVIRONMENT="production"
readonly RAILWAY_REPOSITORY="junqingyongyuanbusi/dm"
readonly PUBLIC_BASE_URL="https://relay.nexory.top"
readonly DEPLOY_TIMEOUT_SECONDS="${DEPLOY_TIMEOUT_SECONDS:-1200}"
readonly API_HEALTH_TIMEOUT_SECONDS="${API_HEALTH_TIMEOUT_SECONDS:-180}"
readonly GRAPHQL_CONNECT_TIMEOUT_SECONDS="${GRAPHQL_CONNECT_TIMEOUT_SECONDS:-10}"
readonly GRAPHQL_QUERY_MAX_TIME_SECONDS="${GRAPHQL_QUERY_MAX_TIME_SECONDS:-30}"
readonly GRAPHQL_QUERY_RETRY_MAX_TIME_SECONDS="${GRAPHQL_QUERY_RETRY_MAX_TIME_SECONDS:-90}"
readonly GRAPHQL_MUTATION_MAX_TIME_SECONDS="${GRAPHQL_MUTATION_MAX_TIME_SECONDS:-60}"
readonly RAILWAY_SERVICES=(api worker scheduler)
readonly RAILWAY_COLOCATED_SERVICES=(api worker scheduler Postgres Redis)
readonly API_SERVICE_ID="b84107eb-c945-4279-92aa-c4691532d9ec"
readonly WORKER_SERVICE_ID="71034ef8-cb9c-44e6-b4da-2b3f9869cd4e"
readonly SCHEDULER_SERVICE_ID="4646baef-e71f-4ffe-b4e2-244afdeec6ce"
readonly POSTGRES_SERVICE_ID="ebf477d5-2b8b-4998-bb55-e17fc9ee88a7"
readonly REDIS_SERVICE_ID="108a4b7b-23bb-45f3-9125-17a718646dca"
readonly RAILWAY_REGION="us-east4-eqdc4a"

usage() {
  cat <<'EOF'
Deploy one exact Git commit from the connected GitHub source to Railway production.

Usage:
  scripts/publish_railway_release.sh --sha=<40-character-sha>

Contract:
  - deploys an exact commit through Railway serviceInstanceDeployV2
  - refuses commits that change migrations/ or alembic.ini
  - requires native Railway GitHub autodeploy to be disabled
  - deploys API, verifies /healthz, then Worker, then Scheduler
  - verifies all three deployment metadata records contain the requested commitHash

Authentication:
  - mutation is authorized only inside .github/workflows/deploy-production.yml
  - RAILWAY_TOKEN must be a production-scoped Railway project token

The one-time image-to-source bootstrap is an operator migration with separately captured digest and
OCI revision evidence; this workflow-internal script only runs after all three roles have source
commitHash provenance.

Commits that change the Alembic graph fail closed. Use the separately reviewed migration-aware
procedure in docs/production-migration.md; never deploy an older raw source commit after a new
schema head has reached production.
EOF
}

log() {
  printf '[source-release] %s\n' "$*" >&2
}

fail() {
  printf '[source-release] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

for command_name in git jq curl uv; do
  require_command "$command_name"
done

release_sha=""
for argument in "$@"; do
  case "$argument" in
    --sha=*) release_sha="${argument#*=}" ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown argument: $argument"
      ;;
  esac
done

[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || fail "--sha must be a full lowercase Git SHA"
for timeout_name in \
  DEPLOY_TIMEOUT_SECONDS \
  API_HEALTH_TIMEOUT_SECONDS \
  GRAPHQL_CONNECT_TIMEOUT_SECONDS \
  GRAPHQL_QUERY_MAX_TIME_SECONDS \
  GRAPHQL_QUERY_RETRY_MAX_TIME_SECONDS \
  GRAPHQL_MUTATION_MAX_TIME_SECONDS; do
  timeout_value="${!timeout_name}"
  [[ "$timeout_value" =~ ^[1-9][0-9]*$ ]] || fail "$timeout_name must be positive"
done

git cat-file -e "${release_sha}^{commit}" 2>/dev/null \
  || fail "release commit is unavailable in this checkout: $release_sha"
git fetch --quiet origin dev
[[ "$(git rev-parse origin/dev)" == "$release_sha" ]] \
  || fail "release commit is stale; origin/dev moved before production mutation"

[[ "${GITHUB_ACTIONS:-}" == "true" && "${PRODUCTION_DEPLOY_AUTHORIZED:-}" == "true" ]] \
  || fail "production source mutation is authorized only from the protected GitHub Actions workflow"
[[ -n "${RAILWAY_TOKEN:-}" ]] || fail "RAILWAY_TOKEN is required"

graphql_request() {
  local request_kind="$1"
  local query="$2"
  local variables="$3"
  local response payload
  local -a curl_options=(
    -fsS
    --connect-timeout "$GRAPHQL_CONNECT_TIMEOUT_SECONDS"
  )
  payload="$(jq -cn --arg query "$query" --argjson variables "$variables" \
    '{query: $query, variables: $variables}')"
  if [[ "$request_kind" == "query" ]]; then
    curl_options+=(
      --max-time "$GRAPHQL_QUERY_MAX_TIME_SECONDS"
      --retry 3
      --retry-all-errors
      --retry-max-time "$GRAPHQL_QUERY_RETRY_MAX_TIME_SECONDS"
    )
  elif [[ "$request_kind" == "mutation" ]]; then
    curl_options+=(--max-time "$GRAPHQL_MUTATION_MAX_TIME_SECONDS")
  else
    fail "unknown GraphQL request kind: $request_kind"
  fi
  response="$(curl "${curl_options[@]}" \
    -H "Project-Access-Token: ${RAILWAY_TOKEN}" \
    -H 'Content-Type: application/json' \
    --data "$payload" \
    "$RAILWAY_API_URL")" || {
      if [[ "$request_kind" == "mutation" ]]; then
        fail "Railway mutation result is unknown; inspect deployment state before retrying"
      fi
      fail "Railway GraphQL query failed"
    }
  if jq -e '(.errors // []) | length > 0' >/dev/null <<<"$response"; then
    jq -c '.errors' <<<"$response" >&2
    fail "Railway GraphQL returned errors"
  fi
  printf '%s\n' "$response"
}

graphql() {
  graphql_request query "$1" "$2"
}

graphql_mutation() {
  graphql_request mutation "$1" "$2"
}

service_id() {
  case "$1" in
    api) printf '%s\n' "$API_SERVICE_ID" ;;
    worker) printf '%s\n' "$WORKER_SERVICE_ID" ;;
    scheduler) printf '%s\n' "$SCHEDULER_SERVICE_ID" ;;
    Postgres) printf '%s\n' "$POSTGRES_SERVICE_ID" ;;
    Redis) printf '%s\n' "$REDIS_SERVICE_ID" ;;
    *) fail "unknown service: $1" ;;
  esac
}

service_state() {
  local service="$1"
  local id variables query
  id="$(service_id "$service")"
  variables="$(jq -cn \
    --arg projectId "$RAILWAY_PROJECT_ID" \
    --arg environmentId "$RAILWAY_ENVIRONMENT_ID" \
    --arg serviceId "$id" \
    '{projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId}')"
  query='query ServiceState($projectId: String!, $environmentId: String!, $serviceId: String!) {
    serviceInstance(environmentId: $environmentId, serviceId: $serviceId) {
      serviceName
      source { repo image }
      numReplicas
      region
      restartPolicyType
      restartPolicyMaxRetries
      domains {
        customDomains { domain targetPort }
        serviceDomains { domain targetPort }
      }
      activeDeployments { id status meta }
      latestDeployment { id status meta }
    }
    renderedVariables: variables(
      projectId: $projectId,
      environmentId: $environmentId,
      serviceId: $serviceId
    )
    unrenderedVariables: variables(
      projectId: $projectId,
      environmentId: $environmentId,
      serviceId: $serviceId,
      unrendered: true
    )
    serviceInstanceAutoDeployStatus(
      projectId: $projectId,
      environmentId: $environmentId,
      serviceId: $serviceId
    ) { enabled canEnable reason }
    deploymentTriggers(
      projectId: $projectId,
      environmentId: $environmentId,
      serviceId: $serviceId
    ) { edges { node { id branch repository checkSuites } } }
  }'
  graphql "$query" "$variables"
}

validate_source_contract() {
  local service="$1"
  local state repo image autodeploy trigger_count
  state="$(service_state "$service")"
  repo="$(jq -r '.data.serviceInstance.source.repo // ""' <<<"$state")"
  image="$(jq -r '.data.serviceInstance.source.image // ""' <<<"$state")"
  autodeploy="$(jq -r '.data.serviceInstanceAutoDeployStatus.enabled' <<<"$state")"
  trigger_count="$(jq -r '.data.deploymentTriggers.edges | length' <<<"$state")"
  [[ "$repo" == "$RAILWAY_REPOSITORY" ]] \
    || fail "$service source repo is ${repo:-none}, expected $RAILWAY_REPOSITORY"
  [[ -z "$image" ]] || fail "$service still has image source $image"
  [[ "$autodeploy" == "false" ]] \
    || fail "$service native Railway autodeploy must be disabled"
  [[ "$trigger_count" == "0" ]] \
    || fail "$service has $trigger_count native deployment trigger(s); only GitHub Actions may deploy"
}

configuration_fingerprint() {
  local service="$1"
  service_state "$service" \
    | uv run python scripts/railway_source_config_snapshot.py fingerprint
}

validate_role_configuration() {
  local service="$1"
  local expected_role="$2"
  local state role testing public_base_url
  state="$(service_state "$service")"
  role="$(jq -r '.data.renderedVariables.SERVICE_ROLE // ""' <<<"$state")"
  testing="$(jq -r '.data.renderedVariables.TESTING // ""' <<<"$state" | tr '[:upper:]' '[:lower:]')"
  public_base_url="$(jq -r '.data.renderedVariables.PUBLIC_BASE_URL // ""' <<<"$state")"
  [[ "$role" == "$expected_role" ]] \
    || fail "$service SERVICE_ROLE is ${role:-missing}, expected $expected_role"
  [[ "$testing" == "false" ]] || fail "$service TESTING must be false"
  [[ "${public_base_url%/}" == "$PUBLIC_BASE_URL" ]] \
    || fail "$service PUBLIC_BASE_URL does not match $PUBLIC_BASE_URL"
  jq -e '
    (.data.renderedVariables.DATABASE_URL // "") != "" and
    (.data.renderedVariables.REDIS_URL // "") != "" and
    (.data.renderedVariables.PLATFORM_SECRET_KEYS // "") != ""
  ' >/dev/null <<<"$state" || fail "$service is missing shared production variables"
}

validate_railway_config() {
  local api_variables worker_variables scheduler_variables
  api_variables="$(service_state api | jq -c '.data.renderedVariables')"
  worker_variables="$(service_state worker | jq -c '.data.renderedVariables')"
  scheduler_variables="$(service_state scheduler | jq -c '.data.renderedVariables')"
  if ! jq -n \
    --argjson api "$api_variables" \
    --argjson worker "$worker_variables" \
    --argjson scheduler "$scheduler_variables" \
    '{api: $api, worker: $worker, scheduler: $scheduler}' \
    | uv run python scripts/validate_railway_config.py \
      --variables-json - "$PUBLIC_BASE_URL"; then
    fail "Railway service variables failed the production consistency check"
  fi
  log "verified Railway role assignment and shared production configuration"
}

active_region() {
  local service="$1"
  local state
  state="$(service_state "$service")"
  jq -er '
    [
      .data.serviceInstance.activeDeployments[]
      | select(.status == "SUCCESS")
      | (.meta.serviceManifest.deploy.multiRegionConfig // {})
      | to_entries[]
      | select((.value.numReplicas // 0) > 0)
      | .key
    ]
    | unique
    | select(length == 1)
    | .[0]
  ' <<<"$state"
}

validate_railway_colocation() {
  local service region
  for service in "${RAILWAY_COLOCATED_SERVICES[@]}"; do
    region="$(active_region "$service")" \
      || fail "could not determine the sole active region for $service"
    [[ "$region" == "$RAILWAY_REGION" ]] \
      || fail "$service region $region does not match $RAILWAY_REGION"
  done
  log "verified Railway colocation in $RAILWAY_REGION"
}

current_commit_for_service() {
  local service="$1"
  service_state "$service" | jq -r '
    ([
      .data.serviceInstance.activeDeployments[]
      | select(.status == "SUCCESS")
      | .meta.commitHash // empty
    ] | first) // ""
  '
}


resolve_predecessor_sha() {
  local api_sha worker_sha scheduler_sha sha common_sha
  api_sha="$(current_commit_for_service api)"
  worker_sha="$(current_commit_for_service worker)"
  scheduler_sha="$(current_commit_for_service scheduler)"
  for sha_name in api_sha worker_sha scheduler_sha; do
    sha="${!sha_name}"
    [[ -n "$sha" ]] \
      || fail "production source deployment metadata lacks commitHash; bootstrap is incomplete"
    git cat-file -e "${sha}^{commit}" 2>/dev/null \
      || fail "production commit is unavailable in this checkout: $sha"
    git merge-base --is-ancestor "$sha" "$release_sha" \
      || fail "target must advance production dev history; use a reviewed revert commit"
    if ! git diff --quiet "$sha" "$release_sha" -- migrations alembic.ini; then
      git diff --name-status "$sha" "$release_sha" -- migrations alembic.ini >&2
      fail "automatic source release refuses Alembic graph changes; use the migration-aware runbook"
    fi
  done
  common_sha="$(git merge-base --octopus "$api_sha" "$worker_sha" "$scheduler_sha" "$release_sha")"
  [[ "$common_sha" =~ ^[0-9a-f]{40}$ ]] || fail "could not resolve production predecessor"
  printf '%s\n' "$common_sha"
}

ensure_service_released() {
  local service="$1"
  local state status commit_hash deployment_id
  state="$(service_state "$service")"
  status="$(jq -r '.data.serviceInstance.latestDeployment.status // ""' <<<"$state")"
  commit_hash="$(jq -r '.data.serviceInstance.latestDeployment.meta.commitHash // ""' <<<"$state")"
  deployment_id="$(jq -r '.data.serviceInstance.latestDeployment.id // ""' <<<"$state")"
  if [[ "$commit_hash" == "$release_sha" ]]; then
    case "$status" in
      SUCCESS)
        log "$service already runs $release_sha; keeping deployment $deployment_id"
        printf '%s\n' "$deployment_id"
        return 0
        ;;
      QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL)
        log "$service target deployment is still $status; resuming wait for $deployment_id"
        wait_for_deployment "$service" "$deployment_id"
        printf '%s\n' "$deployment_id"
        return 0
        ;;
      FAILED|CRASHED|REMOVED|SKIPPED|SLEEPING|"")
        log "$service target deployment ended with ${status:-unknown}; creating a replacement"
        ;;
      *) fail "$service target deployment returned unknown status $status" ;;
    esac
  fi
  deployment_id="$(deploy_service "$service")"
  wait_for_deployment "$service" "$deployment_id"
  printf '%s\n' "$deployment_id"
}

validate_migration_graph_unchanged() {
  local predecessor_sha="$1"
  git cat-file -e "${predecessor_sha}^{commit}" 2>/dev/null \
    || fail "predecessor commit is unavailable: $predecessor_sha"
  if ! git diff --quiet "$predecessor_sha" "$release_sha" -- migrations alembic.ini; then
    git diff --name-status "$predecessor_sha" "$release_sha" -- migrations alembic.ini >&2
    fail "automatic source release refuses Alembic graph changes; use the migration-aware runbook"
  fi
  log "migration graph unchanged from $predecessor_sha to $release_sha"
}

deploy_service() {
  local service="$1"
  local id variables query response deployment_id
  id="$(service_id "$service")"
  variables="$(jq -cn \
    --arg environmentId "$RAILWAY_ENVIRONMENT_ID" \
    --arg serviceId "$id" \
    --arg commitSha "$release_sha" \
    '{environmentId: $environmentId, serviceId: $serviceId, commitSha: $commitSha}')"
  query='mutation DeployService($environmentId: String!, $serviceId: String!, $commitSha: String!) {
    serviceInstanceDeployV2(
      environmentId: $environmentId,
      serviceId: $serviceId,
      commitSha: $commitSha
    )
  }'
  response="$(graphql_mutation "$query" "$variables")"
  deployment_id="$(jq -er '.data.serviceInstanceDeployV2' <<<"$response")" \
    || fail "Railway did not return a deployment ID for $service"
  log "$service deployment queued: $deployment_id"
  printf '%s\n' "$deployment_id"
}

wait_for_deployment() {
  local service="$1"
  local deployment_id="$2"
  local deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS))
  local variables query response status commit_hash
  variables="$(jq -cn --arg id "$deployment_id" '{id: $id}')"
  query='query Deployment($id: String!) { deployment(id: $id) { id status meta } }'
  while (( SECONDS < deadline )); do
    response="$(graphql "$query" "$variables")"
    status="$(jq -r '.data.deployment.status // ""' <<<"$response")"
    case "$status" in
      SUCCESS)
        commit_hash="$(jq -r '.data.deployment.meta.commitHash // ""' <<<"$response")"
        [[ "$commit_hash" == "$release_sha" ]] \
          || fail "$service deployment provenance is ${commit_hash:-missing}, expected $release_sha"
        log "$service deployment succeeded at $release_sha: $deployment_id"
        return 0
        ;;
      FAILED|CRASHED|REMOVED|SKIPPED|SLEEPING)
        fail "$service deployment $deployment_id ended with $status"
        ;;
      QUEUED|INITIALIZING|WAITING|BUILDING|DEPLOYING|NEEDS_APPROVAL|"") ;;
      *) fail "$service deployment $deployment_id returned unknown status $status" ;;
    esac
    sleep 10
  done
  fail "timed out waiting for $service deployment $deployment_id"
}

wait_for_api_health() {
  local deadline=$((SECONDS + API_HEALTH_TIMEOUT_SECONDS))
  local response
  while (( SECONDS < deadline )); do
    response="$(curl -fsS --max-time 10 "${PUBLIC_BASE_URL}/healthz" 2>/dev/null || true)"
    if [[ "$response" == '{"status":"ok"}' ]]; then
      log "API health check passed: ${PUBLIC_BASE_URL}/healthz"
      return 0
    fi
    sleep 3
  done
  fail "API health check failed: ${PUBLIC_BASE_URL}/healthz"
}

verify_final_state() {
  local service state status commit_hash
  for service in "${RAILWAY_SERVICES[@]}"; do
    validate_source_contract "$service"
    state="$(service_state "$service")"
    status="$(jq -r '.data.serviceInstance.latestDeployment.status // ""' <<<"$state")"
    commit_hash="$(jq -r '.data.serviceInstance.latestDeployment.meta.commitHash // ""' <<<"$state")"
    [[ "$status" == "SUCCESS" ]] || fail "$service latest deployment is $status"
    [[ "$commit_hash" == "$release_sha" ]] \
      || fail "$service latest deployment is ${commit_hash:-missing}, expected $release_sha"
  done
}

for service in "${RAILWAY_SERVICES[@]}"; do
  validate_source_contract "$service"
done
validate_role_configuration api api
validate_role_configuration worker worker
validate_role_configuration scheduler scheduler
validate_railway_config
validate_railway_colocation
api_config_before="$(configuration_fingerprint api)"
worker_config_before="$(configuration_fingerprint worker)"
scheduler_config_before="$(configuration_fingerprint scheduler)"
predecessor_sha="$(resolve_predecessor_sha)"
validate_migration_graph_unchanged "$predecessor_sha"

git fetch --quiet origin dev
[[ "$(git rev-parse origin/dev)" == "$release_sha" ]] \
  || fail "release commit became stale during preflight; no production mutation was made"

mkdir -p dist
manifest_path="dist/source-release-${release_sha}.json"
jq -n \
  --arg status deploying \
  --arg release_sha "$release_sha" \
  --arg predecessor_sha "$predecessor_sha" \
  --arg environment "$RAILWAY_ENVIRONMENT" \
  --arg repository "$RAILWAY_REPOSITORY" \
  '{status: $status, release_sha: $release_sha, predecessor_sha: $predecessor_sha,
    environment: $environment, repository: $repository}' >"$manifest_path"

api_deployment_id="$(ensure_service_released api)"
wait_for_api_health
worker_deployment_id="$(ensure_service_released worker)"
scheduler_deployment_id="$(ensure_service_released scheduler)"
verify_final_state
validate_role_configuration api api
validate_role_configuration worker worker
validate_role_configuration scheduler scheduler
validate_railway_config
validate_railway_colocation
[[ "$(configuration_fingerprint api)" == "$api_config_before" ]] \
  || fail "api variables, domains, replicas, region, or restart policy changed during release"
[[ "$(configuration_fingerprint worker)" == "$worker_config_before" ]] \
  || fail "worker variables, domains, replicas, region, or restart policy changed during release"
[[ "$(configuration_fingerprint scheduler)" == "$scheduler_config_before" ]] \
  || fail "scheduler variables, domains, replicas, region, or restart policy changed during release"

jq -n \
  --arg status completed \
  --arg release_sha "$release_sha" \
  --arg predecessor_sha "$predecessor_sha" \
  --arg environment "$RAILWAY_ENVIRONMENT" \
  --arg repository "$RAILWAY_REPOSITORY" \
  --arg api_deployment_id "$api_deployment_id" \
  --arg worker_deployment_id "$worker_deployment_id" \
  --arg scheduler_deployment_id "$scheduler_deployment_id" \
  '{status: $status, release_sha: $release_sha, predecessor_sha: $predecessor_sha,
    environment: $environment, repository: $repository,
    railway: {api_deployment_id: $api_deployment_id,
      worker_deployment_id: $worker_deployment_id,
      scheduler_deployment_id: $scheduler_deployment_id}}' >"$manifest_path"

log "source release completed: $release_sha"
cat "$manifest_path"
