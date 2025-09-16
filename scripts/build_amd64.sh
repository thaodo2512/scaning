#!/usr/bin/env bash
set -euo pipefail

# Build all service images for linux/amd64 locally using Compose override.
# Usage: scripts/build_amd64.sh [SERVICE ...]

export DOCKER_DEFAULT_PLATFORM=linux/amd64
exec docker compose -f docker-compose.yml -f docker-compose.amd64.yml build "$@"

