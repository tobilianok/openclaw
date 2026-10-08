#!/usr/bin/env bash
# Importe (ou met a jour) les identifiants et workflows du majordome dans n8n,
# puis publie les workflows. Idempotent : relancer apres une modification.
#
# Usage (sur la VM Docker, dans /opt/stacks/n8n) :
#   ./installer.sh            # utilise ./majordome.env
#   ./installer.sh autre.env
set -euo pipefail

cd "$(dirname "$0")"
ENV_FILE="${1:-majordome.env}"
SERVICE="${N8N_SERVICE:-n8n}"
# Pour les tests hors Docker : N8N_CMD="npx n8n" ./installer.sh
N8N_CMD="${N8N_CMD:-docker compose exec -T $SERVICE n8n}"
# Dossier lu par le conteneur (monte en /import, voir docker-compose.yml)
IMPORT_DIR="${IMPORT_DIR:-./import}"
IMPORT_IN_N8N="${IMPORT_IN_N8N:-/import}"

[ -f "$ENV_FILE" ] || { echo "Fichier $ENV_FILE introuvable (copier majordome.env.example)"; exit 1; }

# Charge les variables sans executer le fichier.
declare -A VARS
while IFS='=' read -r cle valeur; do
  [[ "$cle" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
  VARS[$cle]="$valeur"
done < <(grep -v '^\s*#' "$ENV_FILE")

manquantes=()
for cle in "${!VARS[@]}"; do
  [ -n "${VARS[$cle]}" ] || manquantes+=("$cle")
done
if [ ${#manquantes[@]} -gt 0 ]; then
  echo "Variables vides dans $ENV_FILE : ${manquantes[*]}"; exit 1
fi

remplacer() {  # fichier_source fichier_cible
  python3 - "$1" "$2" <<'PY'
import json, os, re, sys
src, dst = sys.argv[1], sys.argv[2]
vars_ = json.loads(os.environ["MAJ_VARS"])
texte = open(src, encoding="utf-8").read()
def sub(m):
    cle = m.group(1)
    if cle not in vars_:
        sys.exit(f"Marqueur @@{cle}@@ sans valeur dans le fichier env")
    # Echappement JSON : les valeurs finissent dans des chaines JSON.
    return json.dumps(vars_[cle])[1:-1]
texte = re.sub(r"@@([A-Z_][A-Z0-9_]*)@@", sub, texte)
json.loads(texte)  # verifie que le resultat reste du JSON valide
open(dst, "w", encoding="utf-8").write(texte)
PY
}

export MAJ_VARS="$(for k in "${!VARS[@]}"; do printf '%s\0%s\0' "$k" "${VARS[$k]}"; done \
  | python3 -c 'import sys,json; p=sys.stdin.buffer.read().split(b"\0")[:-1]; print(json.dumps({p[i].decode():p[i+1].decode() for i in range(0,len(p),2)}))')"

mkdir -p "$IMPORT_DIR/workflows"
chmod 700 "$IMPORT_DIR"
trap 'rm -rf "$IMPORT_DIR"' EXIT

remplacer credentials.template.json "$IMPORT_DIR/credentials.json"
for wf in workflows/*.json; do
  remplacer "$wf" "$IMPORT_DIR/workflows/$(basename "$wf")"
done

echo "> Import des identifiants"
$N8N_CMD import:credentials --input="$IMPORT_IN_N8N/credentials.json"
echo "> Import des workflows"
$N8N_CMD import:workflow --separate --input="$IMPORT_IN_N8N/workflows"
for id in majOutilsMcp0001 majSensibleMcp01 majRythme0000001; do
  echo "> Publication du workflow $id"
  $N8N_CMD publish:workflow --id="$id"
done

echo
echo "Termine. Redemarrer n8n pour activer les workflows publies :"
echo "  docker compose restart $SERVICE"
echo "Puis supprimer $ENV_FILE (les secrets sont chiffres dans n8n)."
