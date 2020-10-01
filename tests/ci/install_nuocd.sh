export INFLUXURL=http://localhost:8086
export NUOCMD_API_SERVER=http://localhost:8888

sudo cp -r nuocd /opt/.
sudo chmod 777 /opt/nuocd
pip install -r requirements.txt

sudo cp conf/nuodb.conf /etc/telegraf/telegraf.d
sudo cp conf/outputs.conf /etc/telegraf/telegraf.d

sudo systemctl daemon-reload
sudo systemctl stop telegraf

/usr/bin/telegraf --config /etc/telegraf/telegraf.conf --config-directory /etc/telegraf/telegraf.d&

export PATH=$PATH:/opt/nuodb/bin
nuocmd show domain
ps -ef | grep nuo
ps -ef | grep tele