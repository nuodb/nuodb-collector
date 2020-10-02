#!/usr/bin/env bash

echo "Running $TEST_SUITE"

if [[ $TEST_SUITE = "Bare Metal"  ]]; then
  ./tests/ci/install_environment.sh
  ./tests/ci/install_nuocd.sh
elif [[ $TEST_SUITE = "Docker"  ]]; then
  ./tests/ci/docker_compose.sh
fi