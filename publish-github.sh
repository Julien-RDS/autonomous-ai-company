#!/usr/bin/env bash
set -euo pipefail

OWNER="Julien-RDS"
REPO="autonomous-ai-company"
FULL_NAME="${OWNER}/${REPO}"
cd "$(dirname "$0")"

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) n'est pas installé. Installation Ubuntu: sudo apt update && sudo apt install gh"
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "Connecte-toi à GitHub CLI (avec le compte Julien-RDS): gh auth login"
  exit 1
fi
LOGIN="$(gh api user --jq '.login')"
if [[ "$LOGIN" != "$OWNER" ]]; then
  echo "Compte GitHub actif: $LOGIN ; attendu: $OWNER. Change de compte avant publication."
  exit 1
fi
if [[ ! -d .git ]]; then
  echo "Le dossier .git est absent. Utilise l'archive GitHub-ready avec l'historique ou restaure le bundle Git."
  exit 1
fi
if gh repo view "$FULL_NAME" --json name >/dev/null 2>&1; then
  VISIBILITY="$(gh repo view "$FULL_NAME" --json visibility --jq '.visibility')"
  if [[ "$VISIBILITY" != "PRIVATE" ]]; then
    echo "Le dépôt existe mais n'est pas privé ($VISIBILITY). Arrêt par sécurité."
    exit 1
  fi
  echo "Le dépôt privé existe déjà : $FULL_NAME"
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "https://github.com/${FULL_NAME}.git"
  else
    git remote add origin "https://github.com/${FULL_NAME}.git"
  fi
  git push -u origin main
else
  echo "Création du dépôt privé $FULL_NAME et publication de main..."
  gh repo create "$FULL_NAME" --private --source=. --remote=origin --push \
    --description "Orchestrateur multi-agents IA avec supervision, budget et évaluations"
fi

echo "Dépôt : https://github.com/${FULL_NAME}"
echo "Pour démarrer Claude Code : claude"
