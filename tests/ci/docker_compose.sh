#!/usr/bin/env bash

docker pull nuodb/nuodb-ce:latest

docker-compose up&

sleep 120

docker exec "$(docker container ls -f name=nuoadmin -q)" nuocmd check database --db-name hockey --check-running --num-processes 2 --timeout 300