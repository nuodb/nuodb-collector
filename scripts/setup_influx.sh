#! /bin/bash
set -e

influx bucket create --name nuodb_internal --retention 7d

influx bucket create --name nuodb --retention 7d