#!/usr/bin/env bash

export INFLUXURL=http://localhost:8086
export NUOCMD_API_SERVER=http://localhost:8888
export PYTHON2=python

sudo cp -r nuocd /opt/.
sudo chmod 777 /opt/nuocd
pip install -r requirements.txt

sudo systemctl stop telegraf

sudo cp conf/nuodb.conf /etc/telegraf/telegraf.d
sudo cp conf/outputs.conf /etc/telegraf/telegraf.d
sudo sed -i.bak "/^\[\[outputs.influxdb]]/s/^/# /" /etc/telegraf/telegraf.conf

sudo systemctl daemon-reload

/usr/bin/telegraf --config /etc/telegraf/telegraf.conf --config-directory /etc/telegraf/telegraf.d&
