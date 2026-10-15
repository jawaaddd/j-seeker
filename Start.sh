#!/bin/bash

# Directory where this repo is
DIR="$(cd "$(dirname "$0")" && pwd)"

# Generates the API KEY for my script to connect to the SLSKD REST API with
if [ ! -f "$DIR/.env" ]; then
    echo "SLSKD_API_KEY=role=readwrite;cidr=172.16.0.0/12;$(openssl rand -hex 32)" > "$DIR/.env"
    chmod 600 "$DIR/.env"
fi

docker run -d --name slskd --user "$(id -u):$(id -g)" -p 5030:5030 -p 50300:50300 --env-file "$DIR/.env" -e SLSKD_REMOTE_CONFIGURATION=true -v ~/slskd:/app -v ~/Music/DJLib:/downloads slskd/slskd:latest
