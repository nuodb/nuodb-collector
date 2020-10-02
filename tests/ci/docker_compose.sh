#!/usr/bin/env bash

docker pull nuodb/nuodb-ce:latest

docker-compose up&

sleep 120

n=1
while [ $n -le 30 ]
do
  admin_container_id=$(docker container ls -f name=nuoadmin -q)
  if [[ $admin_container_id ]]; then
    sleep 10
    n=$(( n+1 ))
  else
    break
  fi
done

docker exec "$admin_container_id" nuocmd check database --db-name hockey --check-running --num-processes 2 --timeout 300