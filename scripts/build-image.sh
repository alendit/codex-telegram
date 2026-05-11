#!/bin/sh
set -eu

docker_build() {
  if [ "${DOCKER_BUILD_PULL:-0}" = "1" ]; then
    docker build --pull "$@"
    return
  fi

  docker build "$@"
}

: "${CODEX_TELEGRAM_VERSION:=0.4.0}"
: "${CODEX_TELEGRAM_IMAGE:=codex-telegram:git}"
: "${CODEX_TELEGRAM_EXTRA_TAGS:=}"
: "${CODEX_TELEGRAM_PUSH:=0}"

vcs_ref="$(git rev-parse --short=12 HEAD 2>/dev/null || true)"

docker_build \
  -t "${CODEX_TELEGRAM_IMAGE}" \
  --build-arg "VERSION=${CODEX_TELEGRAM_VERSION}" \
  --build-arg "VCS_REF=${vcs_ref}" \
  -f Dockerfile \
  .

for extra_tag in ${CODEX_TELEGRAM_EXTRA_TAGS}; do
  docker tag "${CODEX_TELEGRAM_IMAGE}" "${extra_tag}"
done

if [ "${CODEX_TELEGRAM_PUSH}" = "1" ]; then
  docker push "${CODEX_TELEGRAM_IMAGE}"
  for extra_tag in ${CODEX_TELEGRAM_EXTRA_TAGS}; do
    docker push "${extra_tag}"
  done
fi
