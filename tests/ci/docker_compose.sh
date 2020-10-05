#!/usr/bin/env bash

export NUOVERSION=4.0.7
export DOCKER_IMAGE=nuodb/nuodb-ce:$NUOVERSION

docker pull DOCKER_IMAGE

docker-compose up&

sleep 120

docker exec "$(docker container ls -f name=nuoadmin -q)" nuocmd check database --db-name hockey --check-running --num-processes 2 --timeout 300