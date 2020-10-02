#!/usr/bin/env bash

docker build .
docker pull nuodb/nuodb-ce:latest

docker-compose up&

n=1
while [ $n -le 30 ]
do
  if [[ $(docker container ls -f name=nuodb-collector_nuoadmin1_1 -q) ]]; then
    sleep 10
    n=$(( n+1 ))
  else
    break
  fi
done

sleep 120

docker exec nuodb-collector_nuoadmin1_1 nuocmd check database --db-name hockey --check-running --num-processes 2 --timeout 300