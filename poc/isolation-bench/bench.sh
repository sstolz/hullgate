#!/bin/sh
# Commande unique du banc d'isolation (BR-005). Voir runner/run_bench.py pour les options.
exec python3 "$(dirname "$0")/runner/run_bench.py" "$@"
