#!/usr/bin/env bash
# promq.sh <promql>            — instant query (POST, no URL escaping worries)
# promq.sh --raw <path+query>  — raw API path
#
# The query runs from inside the stack's network, so it needs no published port.
# That network is named after the compose project, which is the directory this
# repo sits in -- so ask docker which network Prometheus is actually on rather
# than hardcoding a name that goes stale the moment the repo is renamed.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

files=(-f "$here/docker-compose.yml")
[ -f "$here/out/compose.groups.yml" ] && files+=(-f "$here/out/compose.groups.yml")

cid=$(docker compose "${files[@]}" ps -q prometheus 2>/dev/null || true)
if [ -z "$cid" ]; then
  echo "promq: prometheus is not running -- ./harness.sh up first" >&2
  exit 1
fi
net=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$cid")
addr=$(docker inspect -f \
  '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$cid")

if [ "${1:-}" = "--raw" ]; then
  exec docker run --rm --network "$net" curlimages/curl:8.11.1 -s --max-time 8 \
    "http://$addr:9090$2"
fi
docker run --rm --network "$net" curlimages/curl:8.11.1 -s --max-time 8 \
  --data-urlencode "query=$1" "http://$addr:9090/api/v1/query"
