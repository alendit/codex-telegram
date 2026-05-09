#!/bin/sh
set -eu

docker_build_local_store() {
  if [ "${DOCKER_BUILD_PULL:-0}" = "1" ]; then
    docker build --pull "$@"
    return
  fi

  docker build "$@"
}

: "${CODEX_APP_SERVER_IMAGE:=codex-app-server:git}"
: "${CODEX_APP_SERVER_EXTRA_TAGS:=}"
: "${CODEX_APP_SERVER_PUSH:=0}"

docker_build_local_store \
  --target codex-app-server-base \
  -t codex-app-server-base:git \
  -f deploy/codex-app-server/Dockerfile.base \
  ./deploy/codex-app-server

docker_build_local_store \
  --target codex-app-server-runtime \
  -t "${CODEX_APP_SERVER_IMAGE}" \
  -f deploy/codex-app-server/Dockerfile.base \
  ./deploy/codex-app-server

for extra_tag in ${CODEX_APP_SERVER_EXTRA_TAGS}; do
  docker tag "${CODEX_APP_SERVER_IMAGE}" "${extra_tag}"
done

if [ "${CODEX_APP_SERVER_PUSH}" = "1" ]; then
  docker push "${CODEX_APP_SERVER_IMAGE}"
  for extra_tag in ${CODEX_APP_SERVER_EXTRA_TAGS}; do
    docker push "${extra_tag}"
  done
fi
