# Secrets for deploy/docker

Files in this folder are mounted into the containers as Docker secrets. Git and the Docker build
context ignore everything here except this file and `make-key.sh`.

| File | Used for | Make it with |
| --- | --- | --- |
| `wa_mcp.key` | Key of the encrypted fields in `wa.db` (`encryption.mode` keyfile), for both containers | `sh secrets/make-key.sh` |
| `admin_password.txt` | Optional admin password for the OAuth consent page (`WA_MCP_ADMIN_PASSWORD_FILE`) | `printf '%s' 'your password' > secrets/admin_password.txt`, then `sudo chown 65532:65532` and `chmod 600` |

The containers run as user 65532, and key files must not be readable by group or others, so every file
here belongs to user 65532 with mode 0600. Keep a copy of `wa_mcp.key` outside the server: without it the
stored message text cannot be read.
