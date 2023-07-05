#!/usr/bin/env bash

export NUOVERSION=5.0.1-2
export DOCKER_IMAGE=nuodb/nuodb-ce:$NUOVERSION
export COMPOSE_INLFUXDB_VERSION=2.7
export DOCKER_INFLUX_IMAGE=influxdb:$COMPOSE_INLFUXDB_VERSION

docker pull $DOCKER_IMAGE

docker-compose up -d

#check if nuoadmin is up

while [ "$(docker container inspect -f '{{.State.Running}}' "$(docker ps -f name=nuoadmin -q)")" = "false" ]
do
    sleep 1
    echo "Waiting for the nuodadmin container to be up"
done

docker exec "$(docker container ls -f name=nuoadmin -q)" nuocmd check servers --check-connected --check-converged --timeout 300

docker exec "$(docker container ls -f name=nuoadmin -q)" nuocmd check database --db-name hockey --check-running --num-processes 2 --timeout 300