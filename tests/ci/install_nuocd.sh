#!/usr/bin/env bash

export INFLUXURL=http://localhost:8086
export NUOCMD_API_SERVER=http://localhost:8888

sudo cp -r nuocd /opt/.
sudo chmod 777 /opt/nuocd
pip3 install -r requirements.txt

sudo cp conf/telegraf.conf  /etc/telegraf/telegraf.conf
sudo cp conf/nuodb.conf /etc/telegraf/telegraf.d
sudo cp conf/outputs.conf /etc/telegraf/telegraf.d
sudo cp bin/nuocd /usr/local/bin/nuocd

sudo systemctl daemon-reload
sudo systemctl stop telegraf

/usr/bin/telegraf --config /etc/telegraf/telegraf.conf --config-directory /etc/telegraf/telegraf.d