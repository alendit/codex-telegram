#!/bin/sh
set -eu

docker_build() {
  if [ "${DOCKER_BUILD_PULL:-0}" = "1" ]; then
    docker build --pull "$@"
    return
  fi

  docker build "$@"
}

docker_build -t codex-telegram:git -f Dockerfile .
