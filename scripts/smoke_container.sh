#!/usr/bin/env bash
set -euo pipefail

image=${1:-assetboard:ci}
container=assetboard-ci-smoke
bind_container=assetboard-ci-bind-smoke
volume="assetboard-ci-data-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}"
bind_dir=$(mktemp -d)
endpoint_base=
response_file=$(mktemp)
bind_output=$(mktemp)

cleanup() {
  local status=$?
  if (( status != 0 )); then
    echo "Smoke test failed (exit ${status}); container logs:" >&2
    docker logs "$container" >&2 2>&1 || true
    docker logs "$bind_container" >&2 2>&1 || true
  fi
  docker rm --force "$container" "$bind_container" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
  rm -rf "$bind_dir" "$response_file" "$bind_output"
  exit "$status"
}
trap cleanup EXIT

wait_for_endpoint() {
  local path=$1 state
  for attempt in $(seq 1 60); do
    state=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)
    if [[ $state == exited || $state == dead ]]; then
      echo "Container exited while waiting for ${path} (state: ${state})." >&2
      return 1
    fi
    if curl --fail --silent "${endpoint_base}${path}" >/dev/null; then
      echo "${path} is ready (attempt ${attempt})."
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${path}." >&2
  return 1
}

wait_for_healthy() {
  local state health=unknown
  for attempt in $(seq 1 60); do
    state=$(docker inspect --format '{{.State.Status}}' "$container" 2>/dev/null || true)
    if [[ $state == exited || $state == dead ]]; then
      echo "Container exited before becoming healthy (state: ${state})." >&2
      return 1
    fi
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$container")
    if [[ $health == healthy ]]; then
      echo "Docker health is healthy (attempt ${attempt})."
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for Docker health (last state: ${health})." >&2
  return 1
}

assert_nontrivial_response() {
  local path=$1 status
  status=$(curl --silent --show-error --output "$response_file" --write-out '%{http_code}' "${endpoint_base}${path}")
  if [[ $status != 200 ]]; then
    echo "${path} returned HTTP ${status}, expected 200." >&2
    return 1
  fi
  if (( $(wc -c < "$response_file") < 32 )); then
    echo "${path} returned fewer than 32 bytes." >&2
    return 1
  fi
  echo "${path} returned HTTP 200 with a non-trivial body."
}

docker volume create "$volume" >/dev/null
docker run --detach \
  --name "$container" \
  --publish 127.0.0.1::8000 \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  --health-interval 2s \
  --health-start-period 2s \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --mount "type=volume,source=${volume},target=/data" \
  --env ASSETBOARD_DATA_DIR=/data \
  --env ASSETBOARD_ENV=prod \
  --env ASSETBOARD_AUTO_MIGRATE=true \
  --env ASSETBOARD_TRUSTED_PROXIES= \
  "$image" >/dev/null
host_port=$(docker port "$container" 8000/tcp | sed 's/.*://')
endpoint_base="http://127.0.0.1:${host_port}"

wait_for_endpoint /healthz
wait_for_endpoint /readyz
wait_for_healthy
assert_nontrivial_response /
assert_nontrivial_response /static/css/app.css
assert_nontrivial_response /static/vendor/htmx.min.js
assert_nontrivial_response /static/vendor/HTMX-LICENSE.txt

echo "Checking runtime user, read-only application files, license, dependencies, and timezone data."
test "$(docker exec "$container" id -u)" = 10001
echo "Container runs as uid 10001."
if docker exec "$container" sh -c 'touch /app/.smoke-write-test' >/dev/null 2>&1; then
  echo "The /app directory is writable." >&2
  exit 1
fi
echo "Writing under /app fails."
docker exec "$container" test -s /licenses/AssetBoard/LICENSE
echo "/licenses/AssetBoard/LICENSE exists."
docker exec "$container" python -c \
  'import importlib.util; assert importlib.util.find_spec("pytest") is None, "pytest is installed in runtime image"'
docker exec "$container" python -c 'from zoneinfo import ZoneInfo; ZoneInfo("Europe/Warsaw")'
echo "Runtime excludes pytest and includes Europe/Warsaw timezone data."

echo "Checking unwritable bind-mount startup diagnostic."
docker run --name "$bind_container" \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  --mount "type=bind,source=${bind_dir},target=/data" \
  --env ASSETBOARD_DATA_DIR=/data \
  --env ASSETBOARD_ENV=prod \
  "$image" >"$bind_output" 2>&1 && {
    echo "Container unexpectedly started with an unwritable bind mount." >&2
    exit 1
  }
grep -Fq 'is not writable' "$bind_output"
grep -Fq 'chown 10001:10001' "$bind_output"
echo "Unwritable bind mount exits non-zero with ownership guidance."

echo "Recording secret and database persistence markers."
secret_before=$(docker exec "$container" sha256sum /data/secret_key | cut -d ' ' -f 1)
docker exec "$container" python -c \
  'import sqlite3; db=sqlite3.connect("/data/assetboard.db"); db.execute("CREATE TABLE IF NOT EXISTS ci_smoke_persistence (marker TEXT PRIMARY KEY)"); db.execute("INSERT OR REPLACE INTO ci_smoke_persistence VALUES (?)", ("survives-restart",)); db.commit(); db.close()'

echo "Restarting the container."
docker restart --time 2 "$container" >/dev/null
host_port=$(docker port "$container" 8000/tcp | sed 's/.*://')
endpoint_base="http://127.0.0.1:${host_port}"
wait_for_endpoint /healthz
wait_for_endpoint /readyz
wait_for_healthy
secret_after=$(docker exec "$container" sha256sum /data/secret_key | cut -d ' ' -f 1)
test "$secret_before" = "$secret_after"
docker exec "$container" python -c \
  'import sqlite3; db=sqlite3.connect("/data/assetboard.db"); assert db.execute("SELECT marker FROM ci_smoke_persistence").fetchone() == ("survives-restart",); db.close()'

echo "secret_key persisted across restart."
echo "Database marker persisted across restart."
