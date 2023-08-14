<img src="images/nuodb.svg" width="200" height="200" />

# NuoDB Collector

[![Build Status](https://travis-ci.org/nuodb/nuodb-collector.svg?branch=master)](https://travis-ci.org/nuodb/nuodb-collector)

# Introduction

Most modern application monitoring systems consist of the following 3
core components:

* Collector             — daemon(s) to gather metrics such as this repository
* Time Series database  — for storage of real-time, high volume metrics (e.g. InfluxDB, Prometheus, LogStash)
* Query & Visualization — that enables visual monitoring and root cause analysis (e.g. Grafana)

NuoDB Collector utilizes a popular open-source collector - `telegraf`.
It's designed to be used alongside a NuoDB engine process to collect metrics from the engine and publish those metrics to a time series database.

Built into this container are 4 input plugins to collect metrics from the NuoDB engine:

1.  `metrics` - collects the [Engine Metrics](https://doc.nuodb.com/nuodb/4.0.x/reference-information/metrics-published-by-database-processes/)  on a
    regular 10s interval.
2.  `msgtrace` - collects internal NuoDB message tracing data on a regular 30s interval.
3.  `synctrace` - collects internal NuoDB lock tracing data on a regular 30s interval.
4.  `threads` - extends the [Telegraf ProcStat Input plugin](https://github.com/influxdata/telegraf/tree/master/plugins/inputs/procstat) with per-thread data.
Collects host machine resource consumption statistics on a regular 10s interval.

To setup NuoDB Insights visual monitoring which uses the NuoDB Collector, follow the instructions on the [NuoDB Insights](https://github.com/nuodb/nuodb-insights) github page.

To setup NuoDB Performance metric collection using NuoDB Collector when not using NuoDB Insights, follow the instruction on this page.

# NuoDB Collector Page Outline
[Quick Start with Docker Compose](#Quick-start-with-docker-compose)

[Setup Manually in Docker](#Setup-manually-in-docker)

[Setup in Kubernetes](#Setup-in-Kubernetes)

[Setup on Bare Metal Linux](#Setup-on-bare-metal-linux)

[Check Collection Status](#Check-collection-status)

# Quick Start with Docker Compose

For a complete example on how to set up the NuoDB domain with NuoDB collector, you can use `docker compose`.
This repository contains a Docker Compose file (`docker-compose.yml`) which will start:

- 2 Admin Processes (AP)
- 1 Storage Manager (SM)
- 2 Transaction Engines (TE)
- 3 NuoDB Collector containers (1 for SM, 1 for TE, 1 for AP)
- InfluxDB database

Clone the NuoDB Collector repository and `cd` into it:

```
git clone git@github.com:nuodb/nuodb-collector.git
cd nuodb-collector
```

Modify .env file in root folder by changing

```
DOCKER_INFLUXDB_INIT_USERNAME=[your_username]
DOCKER_INFLUXDB_INIT_PASSWORD=[your_password]
DOCKER_INFLUXDB_INIT_ORG=[name_of_organization]
DOCKER_INFLUXDB_INIT_RETENTION=[retention_time]
DOCKER_INFLUXDB_INIT_BUCKET=[your_database_name]
DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=[your_secret_key]
```

To know more about these env variables checkout [docs](https://hub.docker.com/_/influxdb#Automated%20Setup:~:text=to%20the%20host.-,Automated%20Setup,-The%20InfluxDB%20image)

Then run `docker-compose up` to start the processes specified in the Docker Compose file:

```
DOCKER_IMAGE=nuodb/nuodb-ce:latest docker-compose up -d
```

Stop processes started with `docker-compose up` by running the following command:

```
DOCKER_IMAGE=nuodb/nuodb-ce:latest docker-compose down
```

# Setup Manually in Docker

## Download the NuoDB Collector Docker Image

```
docker pull nuodb/nuodb-collector:latest
```

## Building docker image from source (Optional)

Instead of pulling a pre-built Docker image, you can build it yourself from source.

```
git clone git@github.com:nuodb/nuodb-collector.git
cd nuodb-collector
docker build .
docker tag <SHA> <TAG>
```

## Prerequisites

### NuoDB Database

As a prerequisite you must have a running NuoDB Admin domain and database.
To start NuoDB in Docker, follow the [NuoDB Docker Blog Part I](https://nuodb.com/blog/deploy-nuodb-database-docker-containers-part-i).
Following this tutorial will also create the Docker network `nuodb-net`.

After following the [NuoDB Docker Blog Part I](https://nuodb.com/blog/deploy-nuodb-database-docker-containers-part-i) tutorial, verify your domain by running `nuocmd`:

```
$ docker exec -it nuoadmin1 nuocmd show domain
server version: 4.1.1-3-2203dab8dd, server license: Community
server time: 2020-10-20T19:20:35.915, client token: 2003aa06ce0444b2152b543beff9e95312b47e84
Servers:
  [nuoadmin1] nuoadmin1:48005 [last_ack = 1.02] [member = ADDED] [raft_state = ACTIVE] (LEADER, Leader=nuoadmin1, log=0/19/19) Connected *
  [nuoadmin2] nuoadmin2:48005 [last_ack = 1.02] [member = ADDED] [raft_state = ACTIVE] (FOLLOWER, Leader=nuoadmin1, log=0/18/18) Connected
  [nuoadmin3] nuoadmin3:48005 [last_ack = 1.01] [member = ADDED] [raft_state = ACTIVE] (FOLLOWER, Leader=nuoadmin1, log=0/19/19) Connected
Databases:
  test [state = RUNNING]
    [SM] test-sm-1/172.20.0.5:48006 [start_id = 0] [server_id = nuoadmin1] [pid = 39] [node_id = 1] [last_ack =  7.14] MONITORED:RUNNING
    [TE] test-te-1/172.20.0.6:48006 [start_id = 1] [server_id = nuoadmin1] [pid = 39] [node_id = 2] [last_ack =  0.14] MONITORED:RUNNING
```

### NuoDB Insights

If you haven't already, [start InfluxDB and Grafana for NuoDB Insights](https://github.com/nuodb/nuodb-insights#starting-nuodb-insights). 

## Running NuoDB Collector

Each NuoDB Collector runs colocated with a NuoDB engine in the same process namespace.
As such, you must start a NuoDB collector docker container for every running NuoDB engine you want to monitor.

The following value replacement must be done to start a NuoDB Collector container:
- Replace the `<hostname>` with the hostname of the monitored engine container. The hostnames must match. In our example it will be `test-sm-1`
- Replace the `<hostinflux>` placeholder with the URL of a running InfluxDB container. In our example, it will be `influxdb`.
- Replace the `<nuoadmin>` placeholder with the URL of a running NuoDB admin container. In our example, it will be `nuoadmin1`.
- Replace the `<enginecontainer>` placeholder with the URL of a running NuoDB Engine container. In our example, it will be `test-sm-1`.
- Replace the `<influxdb_token>` placeholder with influx api token. To know more about it go to this [link](https://docs.influxdata.com/influxdb/cloud/security/tokens)
- Replace the `<influxdb_bucket_name>` placeholder with initial bucket name. To know more about the bucket visit [link](https://docs.influxdata.com/influxdb/v2.0/organizations/buckets/)
- Replace the `<name_of_organization>` placeholder with the name of organization. To know more about the organization visit [link](https://docs.influxdata.com/influxdb/v2.0/organizations/)
```
docker run -d --name nuocd-sm \
      --hostname <hostname> \
      --network nuodb-net \
      --env INFLUXURL=http://<hostinflux>:8086 \
      --env NUOCMD_API_SERVER=<nuoadmin>:8888 \
      --env INFLUXDB_TOKEN=<influxdb_token> \
      --env INFLUXDB_BUCKET=<influxdb_bucket_name> \
      --env INFLUXDB_ORG=<name_of_organization>
      --pid container:<enginecontainer> \
      nuodb/nuodb-collector:latest
```

Repeat the steps above for all running NuoDB engine containers you want to monitor.

# Setup in Kubernetes

## Software Release requirements

| Software   | Release Requirements                           | 
|------------|------------------------------------------------|
| NuoDB Helm | NuoDB Helm Charts [3.0.0](https://github.com/nuodb/nuodb-helm-charts) or newer |


## Deploying NuoDB Collector using NuoDB Helm Charts

Follow the instructions on the [NuoDB Helm charts](https://github.com/nuodb/nuodb-helm-charts/blob/master/README.md#nuodb-helm-chart-installation) installation page.
NuoDB Collector can be enabled separately for [Admin](https://github.com/nuodb/nuodb-helm-charts/tree/master/stable/admin) and [Database](https://github.com/nuodb/nuodb-helm-charts/tree/master/stable/database) charts. To enable it set the `nuocollector.enabled` variable to `true`. For example:

```bash
helm install admin nuodb/admin --set nuocollector.enabled=true --namespace nuodb
helm install database nuodb/database --set nuocollector.enabled=true --namespace nuodb
```

## Deploying custom plugins

Additional Telegraf plugins can be deployed online without restarting NuoDB services. Plugins are created as Kubernetes configMap resources which are labeled with `nuodb.com/nuocollector-plugin` and the admin or database full name as a label value.

The plugins are specified in `nuocollector.plugins.admin` or `nuocollector.plugins.database` by using the plugin name and plugin text as multiline string. Following example Helm values snippet is adding `outputs.file` plugin with name `file` for database resources:

```
nuocollector:
  plugins:
    database:
      file: |-
        [[outputs.file]]
        files = ["/var/log/nuodb/metrics.log"]
        data_format = "influx"
```

# Setup on Bare Metal Linux

## Installation

These steps are for Red Hat or CentOS bare-metal hosts or VMs. For other platforms, see [Telegraf Documentation](https://portal.influxdata.com/downloads/).

### 1) Install dependencies

NuoDB Collector requires Python 2.7 or > 3.6, which comes installed on most distributions.

The instructions below use `pip` to install Python dependencies. `pip` can be installed on RedHat or CentOS as follows:

```
sudo yum install -y epel-release
sudo yum install -y python-pip
```

### 2) Download and install `telegraf`

```
# influxdb.key GPG Fingerprint: 05CE15085FC09D18E99EFB22684A14CF2582E0C5
cat << 'EOF' | sudo tee /etc/yum.repos.d/influxdata.repo
[influxdata]
name = InfluxData Repository - Stable
baseurl = https://repos.influxdata.com/stable/$basearch/main
enabled = 1
gpgcheck = 1
gpgkey = https://repos.influxdata.com/influxdb.key
EOF
sudo yum install telegraf
```

### 3) Download and install NuoDB Collector

```
git clone https://github.com/nuodb/nuodb-collector.git
cd nuodb-collector
python -m pip install -r requirements.txt -t nuocd/pylib --no-cache
sudo cp -r nuocd /opt/
sudo cp bin/nuocd /usr/local/bin/nuocd
```

## Configuration

The `conf/nuodb.conf` file in this repository configures all four input plugins for NuoDB running on localhost as described in the section above.
The `conf/outputs.conf` file configures an output plugin to a InfluxDB instance defined by the `$INFLUXURL` environment variable.
The `bin/nuocd` file is a convenience wrapper for python.
Replace the `<hostinflux>` placeholder in the `INFLUXURL` line below with the hostname of the machine running the InfluxDB instance, and then run the commands.
```
sudo cp conf/nuodb.conf /etc/telegraf/telegraf.d
sudo cp conf/outputs.conf /etc/telegraf/telegraf.d
sudo cp bin/nuocd /usr/local/bin/nuocd
sudo chown -R telegraf.telegraf /etc/telegraf
cat <<EOF >/etc/default/telegraf
INFLUXURL=http://<hostinflux>:8086
PYTHONPATH=/opt/nuocd/pylib:/opt/nuodb/etc/python/site-packages
EOF
```

## Start NuoDB Collector

```
sudo systemctl daemon-reload
sudo systemctl restart telegraf
```

**NOTE:** If not starting telegraf via `systemd` then the variables set in `/etc/default/telegraf` are not picked up automatically.
Instead you can start telegraf with the following command:
```
sh -c "$(cat /etc/default/telegraf | tr '\n' ' ') telegraf --config /etc/telegraf/telegraf.conf --config-directory /etc/telegraf/telegraf.d"
```

# Check Collection Status

After starting collection, your InfluxDB instance should contain the `nuodb_internal` and `nuodb` databases.
You can check if `nuodb_internal` and `nuodb` exist by launching the InfluxDB CLI:

```
influx
```

Then, from the InfluxDB CLI, run the `SHOW DATABASES` command and check that `nuodb_internal` exists (it may take a minute for the database to be created):

```
$ influx
Connected to http://localhost:8086 version 1.8.3
InfluxDB shell version: 1.8.3
> show databases
name: databases
name
----
_internal
telegraf
nuodb_internal
nuodb
```
