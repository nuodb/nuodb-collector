docker-compose up&
sleep 30
docker exec nuodb-collector_nuoadmin1_1 nuocmd check database --db-name hockey --check-running --num-processes 2 --timeout 300