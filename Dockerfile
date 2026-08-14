# fde-sandbox: build-time deps only — runtime is --network none, so nothing
# can be pip/npm-installed after build. Keep the image lean.
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git bash python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV FDE_CONTAINER=1
WORKDIR /workspace
