#!/usr/bin/env bash
# This root-owned runner is installed explicitly; Git updates never replace it.
set -Eeuo pipefail
umask 077
export GIT_TERMINAL_PROMPT=0

DEPLOY_ROOT=/opt/upstage-bootcamp/autodeploy
STATE_ROOT=/var/lib/upstage-bootcamp-deploy
GIT_REMOTE=https://github.com/pensionbeen/ai-bootcamp.git
BRANCH=main
DEPLOYMENT=upstage-bootcamp
CONTAINER=app
IMAGE_NAME=upstage-bootcamp
KUBECTL=(kubectl --namespace default)
REPOSITORY="$DEPLOY_ROOT/repository.git"
RELEASES="$DEPLOY_ROOT/releases"
VERIFIER="$DEPLOY_ROOT/bin/verify.py"
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

log() { printf '[upstage-deploy] %s\n' "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
live_image() {
  "${KUBECTL[@]}" get deployment "$DEPLOYMENT" \
    -o 'jsonpath={.spec.template.spec.containers[?(@.name=="app")].image}'
}
verify_live() {
  "${KUBECTL[@]}" exec -i "deployment/$DEPLOYMENT" -c "$CONTAINER" -- \
    python - live --snapshot-id "$snapshot_id" < "$VERIFIER"
}

for command in git docker kubectl flock tar realpath mktemp date; do
  command -v "$command" >/dev/null || die "Required program missing: $command"
done
[[ -r "$VERIFIER" ]] || die "Trusted verifier not installed: $VERIFIER"
[[ -r "$STATE_FILE" ]] || die 'Initialize deployed-state from the currently verified release before enabling the timer.'
read -r previous_sha previous_image extra < "$STATE_FILE"
[[ "$previous_sha" =~ ^[0-9a-f]{40}$ && "$previous_image" =~ ^upstage-bootcamp:[0-9a-f]{7,40}$ && -z "$extra" ]] || die 'Invalid deployed-state; manual inspection is required.'

if [[ "$MODE" == check ]]; then
  docker info >/dev/null 2>&1 || die 'Docker is unavailable.'
  git -c credential.helper= ls-remote --exit-code "$GIT_REMOTE" "refs/heads/$BRANCH" >/dev/null
  [[ "$(live_image)" == "$previous_image" ]] || die 'Current image differs from deployed-state.'
  log 'Public main is readable and the running image matches deployed-state. No deployment was changed.'
  exit 0
fi

mkdir -p "$RELEASES" "$STATE_ROOT"
exec 9>"$STATE_ROOT/deploy.lock"
flock -n 9 || { log 'Another deployment is active; skipping this poll.'; exit 0; }
# Re-read after acquiring the lock; a preceding deployment may have just finished.
read -r previous_sha previous_image extra < "$STATE_FILE"
[[ "$previous_sha" =~ ^[0-9a-f]{40}$ && "$previous_image" =~ ^upstage-bootcamp:[0-9a-f]{7,40}$ && -z "$extra" ]] || die 'Invalid deployed-state after lock acquisition.'
sha=''; image=''; release=''; candidate=''; snapshot_id=''
cutover=0; healthy=0

finish() {
  local status=$? actual resolved releases_root
  trap - EXIT INT TERM
  set +e
  if [[ -n "$candidate" ]]; then
    docker rm -f "$candidate" >/dev/null 2>&1
  fi
  if [[ "$status" != 0 && "$cutover" == 1 && "$healthy" == 0 ]]; then
    actual=$(live_image)
    if [[ "$actual" == "$image" || "$actual" == "$previous_image" ]]; then
      log "Validation failed; restoring application image $previous_image. Persistent data and secrets are retained."
      if "${KUBECTL[@]}" set image "deployment/$DEPLOYMENT" "$CONTAINER=$previous_image" && \
         "${KUBECTL[@]}" rollout status "deployment/$DEPLOYMENT" --timeout=180s && \
         verify_live; then
        log 'Previous application is healthy; no database was restored or deleted.'
      else
        log 'ROLLBACK FAILED: manual recovery is required. Database and snapshot remain available.' >&2
        status=70
      fi
    else
      log 'Running image changed outside this deployment; refusing to overwrite it during rollback.' >&2
      status=70
    fi
  fi
  if [[ -n "$release" && -d "$release" ]]; then
    resolved=$(realpath "$release"); releases_root=$(realpath "$RELEASES")
    if [[ "$resolved" != "$releases_root" && "$resolved" == "$releases_root/"* ]]; then
      rm -rf -- "$resolved"
    else
      log 'Refused to remove a release directory outside the dedicated releases root.' >&2
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
[[ "$(live_image)" == "$previous_image" ]] || die 'Running image differs from deployed-state; refusing to replace an unmanaged change.'
if [[ ! -d "$REPOSITORY" ]]; then
  git init --bare "$REPOSITORY" >/dev/null
  git -C "$REPOSITORY" remote add origin "$GIT_REMOTE"
fi
[[ "$(git -C "$REPOSITORY" remote get-url origin)" == "$GIT_REMOTE" ]] || die 'Unexpected Git remote; check the dedicated repository.'
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
git -C "$REPOSITORY" merge-base --is-ancestor "$previous_sha" "$sha" || die 'Refusing a history rewrite; main must include the last healthy release.'
release=$(mktemp -d "$RELEASES/${sha}.XXXXXX")
git -C "$REPOSITORY" archive "$sha" | tar -x -C "$release"
for required in deploy/Dockerfile src/request_audit.py src/web/request-log.html src/web/request-log.mjs src/web/test.html; do
  [[ -f "$release/$required" ]] || die "Required submission/logging component missing: $required"
done
image="$IMAGE_NAME:$sha"
log "Building and checking $sha while the current application stays available."
docker build --memory=512m --cpu-period=100000 --cpu-quota=100000 \
  --file "$release/deploy/Dockerfile" --build-arg "RELEASE_SHA=$sha" \
  --label "org.opencontainers.image.revision=$sha" --tag "$image" "$release"
[[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" == "$sha" ]] || die 'Candidate image revision label is incorrect.'
candidate="upstage-bootcamp-preflight-${sha:0:12}-$$"
docker run --rm --interactive --name "$candidate" --network none --memory=512m --cpus=1 \
  --pids-limit=256 --cap-drop ALL --security-opt no-new-privileges \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python "$image" - candidate < "$VERIFIER"
candidate=''

# Backup the live SQLite DB using its online backup API; never copy a live WAL file.
snapshot_id="$(date -u +%Y%m%dT%H%M%SZ)-${sha:0:12}-$$"
[[ "$(live_image)" == "$previous_image" ]] || die 'Running image changed during preflight.'
"${KUBECTL[@]}" exec -i "deployment/$DEPLOYMENT" -c "$CONTAINER" -- \
  python - backup --snapshot-id "$snapshot_id" < "$VERIFIER"

# Only this deployment's app image changes. No apply/delete touches PVCs, secrets,
# services, Ingress, Cloudflare configuration, or other workloads.
cutover=1
"${KUBECTL[@]}" set image "deployment/$DEPLOYMENT" "$CONTAINER=$image"
"${KUBECTL[@]}" rollout status "deployment/$DEPLOYMENT" --timeout=180s
[[ "$(live_image)" == "$image" ]] || die 'Deployment image changed unexpectedly during rollout.'
verify_live
printf '%s %s\n' "$sha" "$image" > "$STATE_FILE.new"
mv -- "$STATE_FILE.new" "$STATE_FILE"
healthy=1
rm -f -- "$STATE_ROOT/failed-sha"
printf '%s %s snapshot=%s\n' "$(date -u +%FT%TZ)" "$sha" "$snapshot_id" >> "$STATE_ROOT/deployments.log" || log 'Warning: could not append deployment history.'
log "Deployment healthy: $sha. Existing request records and administrator authentication were preserved."
