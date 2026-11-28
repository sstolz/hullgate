#!/usr/bin/env python3
"""Génère les secrets jetables du banc dans .run/env (BR-010). Écrase tout fichier précédent."""
import os
import secrets
import sys

KEYS = ["PG_PASSWORD", "CF_JWT_SECRET", "CF_ENC_SECRET", "CF_ADMIN_PASSWORD",
        "UP_CLIENT_SECRET_1", "UP_CLIENT_SECRET_2", "UP_CONTROL_TOKEN", "U1_PASSWORD", "U2_PASSWORD",
        "GW_ADMIN_TOKEN", "GITEA_ADMIN_PASSWORD", "FRONT_CONTROL_TOKEN"]

def main(path=".run/env"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k in KEYS:
            f.write(f"{k}={secrets.token_urlsafe(24)}\n")

if __name__ == "__main__":
    main(*sys.argv[1:])
