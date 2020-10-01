export NUOVERSION=4.0.7.1
export INFLUX_VERSION=1.4.3

sudo sh -c 'echo madvise > /sys/kernel/mm/transparent_hugepage/enabled'
sudo sh -c 'echo madvise > /sys/kernel/mm/transparent_hugepage/defrag'
wget -q http://ce-downloads.nuohub.org/nuodb-ce_${NUOVERSION}_amd64.deb --output-document=/var/tmp/nuodb.deb
sudo dpkg -i /var/tmp/nuodb.deb
sleep 5

export PATH=$PATH:/opt/nuodb/bin
sudo sed -i -e "/ssl.:/s/true/false/" /etc/nuodb/nuoadmin.conf
sudo service nuoadmin start
nuocmd create archive --db-name hockey --server-id nuoadmin-0 --archive-path /var/opt/nuodb/demo-archives/hockey
nuocmd create database --db-name hockey --dba-user dba --dba-password goalie --te-server-ids nuoadmin-0

wget https://dl.influxdata.com/influxdb/releases/influxdb_${INFLUX_VERSION}_amd64.deb
sudo dpkg -i influxdb_${INFLUX_VERSION}_amd64.deb
sudo service influxdb start

wget -qO- https://repos.influxdata.com/influxdb.key | sudo apt-key add -
source /etc/lsb-release
echo "deb https://repos.influxdata.com/${DISTRIB_ID,,} ${DISTRIB_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/influxdb.list

sudo apt-get update && sudo apt-get install telegraf

nuocmd show domain