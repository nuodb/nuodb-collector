#!/usr/bin/env bash

export NUOVERSION=4.2.7.vee
export DOCKER_IMAGE=nuodb/nuodb-ce:$NUOVERSION

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