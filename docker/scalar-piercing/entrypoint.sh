#!/bin/sh
set -eu

# The blue-98 build image enables an experimental global AutoJIT threshold.
# The piercing compiles only verified Region code explicitly, so do not let
# unrelated Ray/Daft functions inherit that policy.  Keep the JIT available
# for force_compile while clearing disable/auto overrides.
unset PYTHONJITAUTO
unset PYTHONJITDISABLE
export PYTHONJIT=1

data_plane_host=${RAY_HEAD_DATA_PLANE_HOST:-}
if [ -z "$data_plane_host" ]; then
  echo "RAY_HEAD_DATA_PLANE_HOST is required" >&2
  exit 64
fi

token_path=${RAY_AUTH_TOKEN_PATH:-}
if [ -z "$token_path" ] || [ ! -r "$token_path" ]; then
  echo "RAY_AUTH_TOKEN_PATH must name a readable secret" >&2
  exit 64
fi
if [ ! -s "$token_path" ]; then
  echo "Ray authentication secret is empty" >&2
  exit 64
fi

role=${1:-}
case "$role" in
  head)
    head_ip=$(getent ahostsv4 "$data_plane_host" | awk 'NR == 1 { print $1; exit }')
    if [ -z "$head_ip" ]; then
      echo "cannot resolve Ray Head data-plane host: $data_plane_host" >&2
      exit 64
    fi
    exec ray start --head --node-name=ray-head-driver --port=6379 \
      --node-ip-address="$head_ip" \
      --dashboard-host=0.0.0.0 --dashboard-port=8265 --num-cpus=0 --block
    ;;
  worker)
    node_name=${2:?worker node name is required}
    python -c 'import socket,sys,time
host=sys.argv[1]
for attempt in range(120):
    try:
        socket.create_connection((host, 6379), 1).close(); break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit("Ray head did not become reachable")' "$data_plane_host"
    exec ray start --address="$data_plane_host:6379" --node-name="$node_name" \
      --num-cpus=2 --block
    ;;
  *)
    echo "usage: scalar-piercing-entrypoint head|worker NODE_NAME" >&2
    exit 64
    ;;
esac
