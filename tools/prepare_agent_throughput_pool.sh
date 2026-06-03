#!/bin/bash
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

POOL_DIR=${AGENT_THROUGHPUT_POOL_DIR:-/srv/agent-throughput-pool}
COUNT=${AGENT_THROUGHPUT_POOL_SIZE:-128}
CHROOT_BASE=${AGENT_THROUGHPUT_CHROOT_BASE:-/srv/agent-throughput-jailer}
PREFIX=${AGENT_THROUGHPUT_POOL_PREFIX:-agtpool}
EXEC_NAME=${AGENT_THROUGHPUT_EXEC_NAME:-firecracker}
UID_IN_JAIL=${AGENT_THROUGHPUT_JAILER_UID:-1234}
GID_IN_JAIL=${AGENT_THROUGHPUT_JAILER_GID:-1234}

usage() {
    cat <<EOF
Usage: $0 [--pool-dir DIR] [--count N] [--chroot-base DIR] [--prefix PREFIX]

Pre-create network namespaces, TAP devices, and jailer chroot roots for
tests/integration_tests/performance/test_agent_throughput.py.

Environment overrides:
  AGENT_THROUGHPUT_POOL_DIR       default: /srv/agent-throughput-pool
  AGENT_THROUGHPUT_POOL_SIZE      default: 128
  AGENT_THROUGHPUT_CHROOT_BASE    default: /srv/agent-throughput-jailer
  AGENT_THROUGHPUT_POOL_PREFIX    default: agtpool

Run the pytest with:
  AGENT_THROUGHPUT_POOL_DIR=$POOL_DIR
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pool-dir)
            shift
            POOL_DIR=$1
            ;;
        --count)
            shift
            COUNT=$1
            ;;
        --chroot-base)
            shift
            CHROOT_BASE=$1
            ;;
        --prefix)
            shift
            PREFIX=$1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root because it creates network namespaces and TAP devices." >&2
    exit 1
fi

if [[ ! $COUNT =~ ^[0-9]+$ ]] || [[ $COUNT -le 0 ]]; then
    echo "--count must be a positive integer" >&2
    exit 2
fi

mkdir -p "$POOL_DIR" "$CHROOT_BASE/$EXEC_NAME"

manifest_tmp=$(mktemp "$POOL_DIR/manifest.json.tmp.XXXXXX")
cat > "$manifest_tmp" <<EOF
{
  "schema": "firecracker-agent-throughput-pool-v1",
  "pool_dir": "$POOL_DIR",
  "chroot_base": "$CHROOT_BASE",
  "exec_name": "$EXEC_NAME",
  "resources": [
EOF

for ((i = 0; i < COUNT; i++)); do
    microvm_id=$(printf "%s-%06d" "$PREFIX" "$i")
    netns_name=$(printf "%s-ns-%06d" "$PREFIX" "$i")
    if ! ip netns list | awk '{print $1}' | grep -qx "$netns_name"; then
        ip netns add "$netns_name"
    fi
    if ip netns exec "$netns_name" ip link show tap0 >/dev/null 2>&1; then
        ip netns exec "$netns_name" ip link delete tap0
    fi

    chroot_root="$CHROOT_BASE/$EXEC_NAME/$microvm_id/root"
    mkdir -p "$chroot_root/dev/net" "$chroot_root/run" "$chroot_root/etc"
    chown "$UID_IN_JAIL:$GID_IN_JAIL" \
        "$chroot_root" "$chroot_root/dev" "$chroot_root/dev/net" "$chroot_root/run"
    chmod 700 "$chroot_root" "$chroot_root/dev" "$chroot_root/dev/net" "$chroot_root/run"

    if [[ -f /etc/localtime ]]; then
        cp -f /etc/localtime "$chroot_root/etc/localtime"
    fi

    comma=","
    if [[ $i -eq $((COUNT - 1)) ]]; then
        comma=""
    fi
    cat >> "$manifest_tmp" <<EOF
    {
      "index": $i,
      "microvm_id": "$microvm_id",
      "netns_name": "$netns_name"
    }$comma
EOF
done

cat >> "$manifest_tmp" <<EOF
  ]
}
EOF

mv "$manifest_tmp" "$POOL_DIR/manifest.json"
echo "Prepared $COUNT throughput pool entries."
echo "Pool manifest: $POOL_DIR/manifest.json"
echo "Use: AGENT_THROUGHPUT_POOL_DIR=$POOL_DIR"
