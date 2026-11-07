#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 n'est pas installé" >&2
  exit 1
fi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Configuration .env créée en mode SIMULATION"
fi
python -m company init
echo "Installation prête. Commande suivante : source .venv/bin/activate && python -m company once"
