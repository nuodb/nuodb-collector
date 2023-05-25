#!/usr/bin/env bash

export NUOVERSION=5.0.3.3
export TAR_VERSION=nuodb-${NUOVERSION}.linux.x86_64
export INFLUX_VERSION=1.4.3
echo "http://ce-downlaods.nuohub.org/${TAR_VERSION}.tar.gz"
wget http://ce-downloads.nuohub.org/${TAR_VERSION}.tar.gz --output-document=/var/tmp/nuodb.tgz
tar xzf /var/tmp/nuodb.tgz
export NUODB_HOME=${PWD}/${TAR_VERSION}
sleep 5

export PATH=$PATH:${NUODB_HOME}/bin
export NUO_SET_TLS=disable
$NUODB_HOME/etc/nuoadmin tls $NUO_SET_TLS
$NUODB_HOME/etc/nuoadmin start

nuocmd create archive --db-name hockey --server-id nuoadmin-0 --archive-path ${NUODB_HOME}/var/opt/demo-archives/hockey
nuocmd create database --db-name hockey --dba-user dba --dba-password goalie --te-server-ids nuoadmin-0

wget https://dl.influxdata.com/influxdb/releases/influxdb_${INFLUX_VERSION}_amd64.deb
sudo dpkg -i influxdb_${INFLUX_VERSION}_amd64.deb
sudo service influxdb start

wget -qO- https://repos.influxdata.com/influxdata-archive_compat.key | sudo apt-key add -
source /etc/lsb-release
echo "deb https://repos.influxdata.com/${DISTRIB_ID,,} ${DISTRIB_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/influxdb.list

sudo apt-get update && sudo apt-get install telegraf

nuocmd check database --db-name hockey --check-running --num-processes 2 --timeout 120
nuocmd show domain