#!/bin/sh -x

NAME=brauze
PFX=hackacharya/$NAME
docker tag $NAME ${PFX}:latest$1
docker push ${PFX}:latest$1
