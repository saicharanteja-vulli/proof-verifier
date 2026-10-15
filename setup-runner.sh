#!/bin/sh
set -eu
cd "$(dirname "$0")"
docker build -t proof-fastapi-runner:1 runner
