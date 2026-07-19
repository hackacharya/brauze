#!/bin/sh

PORT=8000
case $1 in
  -p) PORT=$2; shift; shift;;
esac

if [ $# -lt 1 ]; then
  echo "usage: $0 [-p port] HOST_DIRECTORY" >&2
  exit 1
fi

HOST_DIRECTORY=$1

if [ ! -d "$HOST_DIRECTORY" ]; then
  echo "not a directory: $HOST_DIRECTORY" >&2
  exit 1
fi

echo "Starting brauze on $PORT for $HOST_DIRECTORY ..."
docker run --rm -it \
  -p $PORT:8000 \
  -v "$HOST_DIRECTORY:/data:ro" \
  brauze
