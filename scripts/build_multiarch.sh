#!/usr/bin/env bash
set -euo pipefail

# Build and push a multi-arch image (amd64+arm64) with Buildx.
# Requires: Docker Buildx and a registry destination (IMAGE=registry/repo:tag)
# Example:
#   IMAGE=us-central1-docker.pkg.dev/PROJECT/cs-repo/cryptostorm:latest \
#   scripts/build_multiarch.sh

IMAGE=${IMAGE:-}
PLATFORMS=${PLATFORMS:-linux/amd64,linux/arm64}

if [[ -z "$IMAGE" ]]; then
  echo "ERROR: Set IMAGE=REGISTRY/REPO:TAG to push multi-arch images" >&2
  exit 2
fi

# Ensure buildx builder exists
if ! docker buildx ls | grep -q "\bcsbuilder\b"; then
  docker buildx create --name csbuilder --use >/dev/null
else
  docker buildx use csbuilder >/dev/null
fi

echo "Building multi-arch: $PLATFORMS -> $IMAGE"
docker buildx build \
  --platform "$PLATFORMS" \
  -t "$IMAGE" \
  --push \
  .

echo "Done. To use in compose, set image to: $IMAGE"

