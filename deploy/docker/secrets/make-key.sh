#!/bin/sh
# Creates secrets/wa_mcp.key, the key of the database fields for both containers: 32 random bytes as
# 64 hex characters, mode 0600, owned by user 65532 (the user inside the images).
set -eu
key="$(dirname "$0")/wa_mcp.key"
if [ -f "$key" ]; then
  echo "$key exists; keep it, without it the stored messages cannot be read"
  exit 0
fi
umask 077
tmp=$(mktemp "$key.XXXXXXXX")
trap 'rm -f "$tmp"' EXIT
trap 'exit 1' HUP INT TERM
key_hex=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
[ "${#key_hex}" -eq 64 ] || exit 1
printf '%s\n' "$key_hex" > "$tmp"
as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi
}
as_root chown 65532:65532 "$tmp"
[ ! -d "$key" ] || exit 1
if ! as_root ln "$tmp" "$key"; then
  [ -f "$key" ] || exit 1
  echo "$key already exists; keeping it"
  exit 0
fi
echo "created $key; keep a copy outside the server, there is no recovery without it"
