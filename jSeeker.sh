#!/bin/bash
# Usage: ./jSeeker.sh "<spotify playlist or album link>"
#        ./jSeeker.sh          (only start slskd, e.g. for first-time setup)

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

if ! docker compose version >/dev/null 2>&1; then
    echo "Docker Compose isn't installed. On Arch: sudo pacman -S docker-compose docker-buildx"
    exit 1
fi

# Generate the API key j-seeker uses to talk to slskd's REST API
if ! grep -q '^SLSKD_API_KEY=' .env 2>/dev/null; then
    [ -s .env ] && [ -n "$(tail -c1 .env)" ] && echo >> .env
    echo "SLSKD_API_KEY=role=readwrite;cidr=172.16.0.0/12;$(openssl rand -hex 32)" >> .env
    chmod 600 .env
fi

# Containers run as you, so the files they write are yours, and in your time zone (they default to UTC)
export PUID="$(id -u)" PGID="$(id -g)"
export TZ="${TZ:-$(timedatectl show -p Timezone --value 2>/dev/null || readlink /etc/localtime | sed 's|.*/zoneinfo/||')}"

# Create the mounted folders first; if Docker creates them, they're owned by root and slskd can't write to them
env_value() { grep -m1 "^$1=" .env | cut -d= -f2- | sed "s|^~|$HOME|"; }
MUSIC_DIR="$(env_value MUSIC_DIR)"
SLSKD_DIR="$(env_value SLSKD_DIR)"
mkdir -p "${SLSKD_DIR:-$HOME/slskd}" "${MUSIC_DIR:-$HOME/Music/DJLib}/Tracks"

# An slskd container started by the old Start.sh would clash with the one docker compose manages
if [ -n "$(docker ps -aq -f name='^slskd$')" ] && [ -z "$(docker ps -aq -f name='^slskd$' -f label=com.docker.compose.project)" ]; then
    echo "An slskd container from the old Start.sh still exists. Once nothing is downloading, remove it with:"
    echo "  docker rm -f slskd"
    exit 1
fi

if [ $# -eq 0 ]; then
    exec docker compose up -d slskd
fi
exec docker compose run --rm --service-ports jseeker "$@"
