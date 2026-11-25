#!/usr/bin/env bash
# Installed explicitly by an operator. Repository changes never replace this runner.
set -Eeuo pipefail
umask 077
export GIT_TERMINAL_PROMPT=0

DEPLOY_ROOT=/opt/upstage-bootcamp/visits
STATE_ROOT=/var/lib/upstage-bootcamp-visits
GIT_REMOTE=https://github.com/pensionbeen/ai-bootcamp.git
BRANCH=main
DEPLOYMENT=upstage-visits
CONTAINER=app
IMAGE_NAME=upstage-visits
ROUTE_NAME=upstage-visits-https
KUBECTL=(kubectl --namespace default)
REPOSITORY="$DEPLOY_ROOT/repository.git"
RELEASES="$DEPLOY_ROOT/releases"
VERIFIER="$DEPLOY_ROOT/bin/verify.py"
BASE_CONFIG="$DEPLOY_ROOT/config/base.json"
ROUTE_CONFIG="$DEPLOY_ROOT/config/route.json"
STATE_FILE="$STATE_ROOT/deployed-state"
MODE=deploy
RETRY=0
for argument in "$@"; do
  case "$argument" in
    --check) MODE=check ;;
    --retry) RETRY=1 ;;
    *) printf 'Usage: deploy.sh [--check] [--retry]\n' >&2; exit 2 ;;
  esac
done

log() { printf '[upstage-visits-deploy] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
live_image() {
  "${KUBECTL[@]}" get deployment "$DEPLOYMENT" --ignore-not-found \
    -o 'jsonpath={.spec.template.spec.containers[?(@.name=="app")].image}'
}
read_state() {
  initial=1; previous_sha=''; previous_image=''; extra=''
  if [[ -e "$STATE_FILE" ]]; then
    read -r previous_sha previous_image extra < "$STATE_FILE"
    [[ "$previous_sha" =~ ^[0-9a-f]{40}$ && "$previous_image" == "$IMAGE_NAME:$previous_sha" && -z "$extra" ]] || die 'Invalid deployed-state; manual inspection is required.'
    initial=0
  fi
}
check_live_owner() {
  local actual route
  if [[ "$initial" == 1 ]]; then
    actual=$("${KUBECTL[@]}" get deployment "$DEPLOYMENT" --ignore-not-found -o 'jsonpath={.metadata.name}') || die 'Cannot read the visit deployment.'
    [[ -z "$actual" ]] || die 'Visit deployment exists without deployed-state; inspect the initial deployment before retrying.'
    route=$("${KUBECTL[@]}" get ingressroute "$ROUTE_NAME" --ignore-not-found -o 'jsonpath={.metadata.name}') || die 'Cannot read the visit route.'
    [[ -z "$route" ]] || die 'Visit route exists without deployed-state; manual inspection is required.'
  else
    actual=$(live_image) || die 'Cannot read the visit deployment.'
    [[ "$actual" == "$previous_image" ]] || die 'Running visit image differs from deployed-state; refusing an unmanaged change.'
  fi
}
verify_live() {
  "${KUBECTL[@]}" exec -i "deployment/$DEPLOYMENT" -c "$CONTAINER" -- \
    python - live < "$VERIFIER"
}

for command in git docker kubectl flock tar realpath mktemp date python3; do
  command -v "$command" >/dev/null || die "Required program missing: $command"
done
for file in "$VERIFIER" "$BASE_CONFIG" "$ROUTE_CONFIG"; do
  [[ -r "$file" ]] || die "Trusted installation file missing: $file"
done
read_state
if [[ "$MODE" == check ]]; then
  docker info >/dev/null 2>&1 || die 'Docker is unavailable.'
  git -c credential.helper= ls-remote --exit-code "$GIT_REMOTE" "refs/heads/$BRANCH" >/dev/null
  check_live_owner
  log 'Public main and trusted installation are readable; visit deployment ownership is consistent. No resources were changed.'
  exit 0
fi

mkdir -p "$RELEASES" "$STATE_ROOT"
exec 9>"$STATE_ROOT/deploy.lock"
flock -n 9 || { log 'Another visit deployment is active; skipping this poll.'; exit 0; }
read_state
sha=''; image=''; release=''; candidate=''
cutover=0; healthy=0; route_attempted=0

finish() {
  local status=$? actual resolved releases_root
  trap - EXIT INT TERM
  set +e
  if [[ -n "$candidate" ]]; then
    docker rm -f "$candidate" >/dev/null 2>&1
  fi
  if [[ "$status" != 0 && "$healthy" == 0 ]]; then
    if [[ "$initial" == 1 && "$route_attempted" == 1 ]]; then
      # The old application route remains in place underneath this dedicated route.
      if ! "${KUBECTL[@]}" delete ingressroute "$ROUTE_NAME" --ignore-not-found; then
        log 'ROLLBACK FAILED: cannot remove the initial visit route; inspect it manually.' >&2
        status=70
      fi
    elif [[ "$initial" == 0 && "$cutover" == 1 ]]; then
      actual=$(live_image)
      if [[ "$actual" == "$image" || "$actual" == "$previous_image" ]]; then
        log "Validation failed; restoring visit image $previous_image."
        if "${KUBECTL[@]}" set image "deployment/$DEPLOYMENT" "$CONTAINER=$previous_image" && \
           "${KUBECTL[@]}" rollout status "deployment/$DEPLOYMENT" --timeout=180s && verify_live; then
          log 'Previous visit service is healthy; no database was restored or deleted.'
        else
          log 'ROLLBACK FAILED: inspect the visit deployment and its retained database.' >&2
          status=70
        fi
      else
        log 'Visit image changed outside this deployment; refusing to overwrite it during rollback.' >&2
        status=70
      fi
    fi
    if [[ "$initial" == 1 && "$cutover" == 1 ]]; then
      log 'Initial visit resources were retained for inspection. The RAG application was not changed.'
    fi
  fi
  if [[ -n "$release" && -d "$release" ]]; then
    resolved=$(realpath "$release"); releases_root=$(realpath "$RELEASES")
    if [[ "$resolved" != "$releases_root" && "$resolved" == "$releases_root/"* ]]; then
      rm -rf -- "$resolved"
    else
      log 'Refused to remove a release outside the dedicated visits releases directory.' >&2
      status=70
    fi
  fi
  if [[ "$status" != 0 ]]; then
    printf '%s exit=%s sha=%s\n' "$(date -u +%FT%TZ)" "$status" "$sha" > "$STATE_ROOT/last-failure"
    [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]] || printf '%s\n' "$sha" > "$STATE_ROOT/failed-sha"
  fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker info >/dev/null 2>&1 || die 'Docker is unavailable.'
check_live_owner
if [[ ! -d "$REPOSITORY" ]]; then
  git init --bare "$REPOSITORY" >/dev/null
  git -C "$REPOSITORY" remote add origin "$GIT_REMOTE"
fi
[[ "$(git -C "$REPOSITORY" remote get-url origin)" == "$GIT_REMOTE" ]] || die 'Unexpected Git remote; inspect the dedicated visits repository.'
git -c credential.helper= -C "$REPOSITORY" fetch --no-tags origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
sha=$(git -C "$REPOSITORY" rev-parse "refs/remotes/origin/$BRANCH^{commit}")
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die 'main did not resolve to a full commit SHA.'
if [[ "$sha" == "$previous_sha" ]]; then
  log "Already deployed: $sha"
  exit 0
fi
failed_sha=''; [[ ! -f "$STATE_ROOT/failed-sha" ]] || read -r failed_sha < "$STATE_ROOT/failed-sha"
if [[ "$sha" == "$failed_sha" && "$RETRY" == 0 ]]; then
  log "Commit $sha previously failed; waiting for a corrected commit or an explicit --retry."
  exit 0
fi
if [[ "$initial" == 0 ]]; then
  git -C "$REPOSITORY" merge-base --is-ancestor "$previous_sha" "$sha" || die 'Refusing a history rewrite; main must include the last healthy visit release.'
  if git -C "$REPOSITORY" diff --quiet "$previous_sha" "$sha" -- visit_service; then
    log "Visit service inputs unchanged; retaining $previous_sha ($previous_image)."
    exit 0
  else
    diff_status=$?
    [[ "$diff_status" == 1 ]] || die "Unable to compare visit inputs (git diff exit $diff_status)."
  fi
fi

release=$(mktemp -d "$RELEASES/${sha}.XXXXXX")
git -C "$REPOSITORY" archive "$sha" visit_service | tar -x -C "$release"
[[ -f "$release/visit_service/Dockerfile" ]] || die 'Visit service Dockerfile is missing.'
image="$IMAGE_NAME:$sha"
log "Building and checking visit service $sha; the RAG application stays untouched."
docker build --memory=512m --cpu-period=100000 --cpu-quota=100000 \
  --file "$release/visit_service/Dockerfile" --build-arg "RELEASE_SHA=$sha" \
  --label "org.opencontainers.image.revision=$sha" --tag "$image" "$release/visit_service"
[[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" == "$sha" ]] || die 'Candidate image revision label is incorrect.'
candidate="upstage-visits-preflight-${sha:0:12}-$$"
docker run --rm --interactive --name "$candidate" --network none --memory=256m --cpus=1 \
  --pids-limit=128 --cap-drop ALL --security-opt no-new-privileges \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python "$image" - candidate < "$VERIFIER"
candidate=''
check_live_owner

if [[ "$initial" == 1 ]]; then
  # Validate the explicitly installed manifest's ownership and change only its image.
  python3 - "$BASE_CONFIG" "$ROUTE_CONFIG" "$release/base.json" "$image" <<'PY'
import json, sys
from pathlib import Path
base_path, route_path, target, image = sys.argv[1:]
base = json.loads(Path(base_path).read_text())
expected = {('PersistentVolumeClaim', 'upstage-visits-data'), ('Service', 'upstage-visits'), ('Deployment', 'upstage-visits')}
items = base.get('items', [])
actual = {(x.get('kind'), x.get('metadata', {}).get('name')) for x in items}
if base.get('kind') != 'List' or len(items) != 3 or actual != expected:
    raise SystemExit('Trusted base manifest must contain only the three visit resources')
for item in items:
    if item.get('metadata', {}).get('namespace') != 'default':
        raise SystemExit('Unexpected visit namespace')
deployment = next(x for x in items if x['kind'] == 'Deployment')
containers = deployment['spec']['template']['spec']['containers']
if len(containers) != 1 or containers[0]['name'] != 'app':
    raise SystemExit('Unexpected visit container')
strategy = deployment['spec'].get('strategy', {})
if strategy.get('type') != 'RollingUpdate' or strategy.get('rollingUpdate', {}).get('maxUnavailable') != 0:
    raise SystemExit('Visit deployment must preserve an available replica during updates')
containers[0]['image'] = image
route = json.loads(Path(route_path).read_text())
if route.get('kind') != 'IngressRoute' or route.get('metadata') != {'name': 'upstage-visits-https', 'namespace': 'default'}:
    raise SystemExit('Unexpected visit route identity')
routes = route.get('spec', {}).get('routes', [])
if not routes or any(service.get('name') != 'upstage-visits' for r in routes for service in r.get('services', [])):
    raise SystemExit('Visit route must only target the visit service')
Path(target).write_text(json.dumps(base))
PY
  cutover=1
  "${KUBECTL[@]}" apply -f "$release/base.json"
else
  "${KUBECTL[@]}" exec -i "deployment/$DEPLOYMENT" -c "$CONTAINER" -- python - backup < "$VERIFIER"
  [[ "$(live_image)" == "$previous_image" ]] || die 'Running visit image changed during preflight.'
  cutover=1
  "${KUBECTL[@]}" set image "deployment/$DEPLOYMENT" "$CONTAINER=$image"
fi
"${KUBECTL[@]}" rollout status "deployment/$DEPLOYMENT" --timeout=180s
[[ "$(live_image)" == "$image" ]] || die 'Visit image changed unexpectedly during rollout.'
verify_live
if [[ "$initial" == 1 ]]; then
  route_attempted=1
  "${KUBECTL[@]}" apply -f "$ROUTE_CONFIG"
  verify_live
fi
printf '%s %s\n' "$sha" "$image" > "$STATE_FILE.new"
mv -- "$STATE_FILE.new" "$STATE_FILE"
healthy=1
rm -f -- "$STATE_ROOT/failed-sha"
printf '%s %s\n' "$(date -u +%FT%TZ)" "$sha" >> "$STATE_ROOT/deployments.log" || log 'Warning: could not append visit deployment history.'
log "Visit service deployment healthy: $sha. RAG deployment and its data were not changed."
