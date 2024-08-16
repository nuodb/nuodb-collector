#!/bin/sh

set -ex

# export all environment variables in .env
eval "$(sed -n 's/\(.*=\)/export \1/p' .env)"

# create domain and database and wait all processes to become ready
docker compose up --wait --wait-timeout 15
nuoadmin="$(docker container ls -f name=nuoadmin -q)"
docker exec "$nuoadmin" nuocmd check servers --check-connected --check-converged --timeout 300
docker exec "$nuoadmin" nuocmd check database --db-name hockey --check-running --num-processes 2 --timeout 300
