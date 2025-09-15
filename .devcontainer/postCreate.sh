#!/usr/bin/env bash
set -euo pipefail

apt update
apt install -y make curl git ca-certificates gnupg lsb-release

# install docker compose plugin
DOCKER_CONFIG=${DOCKER_CONFIG:-/home/vscode/.docker}
mkdier -p "$DOCKER_CONFIG/cli-plugins"
curl -SL https://github.com/docker/compose/releases/latest/download/v2.29.7/docker-compose-linux-x86_64 -o "$DOCKER_CONFIG/cli-plugins/docker-compose"
chmod +x "$DOCKER_CONFIG/cli-plugins/docker-compose"

echo "postCreate done"
