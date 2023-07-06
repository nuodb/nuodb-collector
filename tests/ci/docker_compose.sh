#!/usr/bin/env bash

export NUOVERSION=5.0.1-2
export DOCKER_IMAGE=nuodb/nuodb-ce:$NUOVERSION
export COMPOSE_INLFUXDB_VERSION=2.7
export DOCKER_INFLUX_IMAGE=influxdb:$COMPOSE_INLFUXDB_VERSION

# env variables required to setup influxdb username and password
export DOCKER_INFLUXDB_INIT_MODE=setup
export DOCKER_INFLUXDB_INIT_USERNAME=nuodb
export DOCKER_INFLUXDB_INIT_PASSWORD=helloworld
export DOCKER_INFLUXDB_INIT_ORG=nuodb
export DOCKER_INFLUXDB_INIT_RETENTION=1w
export DOCKER_INFLUXDB_INIT_BUCKET=telegraf
export DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=quickbrownfoxjumpsoveralazydog

docker pull $DOCKER_IMAGE

docker-compose up

#check if nuoadmin is up

while [ "$(docker container inspect -f '{{.State.Running}}' "$(docker ps -f name=nuoadmin -q)")" = "false" ]
do
    sleep 1
    echo "Waiting for the nuodadmin container to be up"
done

docker exec "$(docker container ls -f name=nuoadmin -q)" nuocmd check servers --check-connected --check-converged --timeout 300

docker exec "$(docker container ls -f name=nuoadmin -q)" nuocmd check database --db-name hockey --check-running --num-processes 2 --timeout 300