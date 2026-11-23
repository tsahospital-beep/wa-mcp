#!/bin/sh
# Creates secrets/wa_mcp.key, the key of the database fields for both containers: 32 random bytes as
# 64 hex characters, mode 0600, owned by user 65532 (the user inside the images).
set -eu
key="$(dirname "$0")/wa_mcp.key"
if [ -e "$key" ]; then
  echo "$key exists; keep it, without it the stored messages cannot be read"
  exit 0
fi
umask 077
od -An -tx1 -N32 /dev/urandom | tr -d ' \n' > "$key"
echo >> "$key"
if [ "$(id -u)" = 0 ]; then
  chown 65532:65532 "$key"
else
  sudo chown 65532:65532 "$key" || echo "run: sudo chown 65532:65532 $key"
fi
echo "created $key; keep a copy outside the server, there is no recovery without it"
