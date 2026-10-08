#!/usr/bin/env bash
# Installe l'acces SSH de Jarvis sur une machine (a lancer en root).
#
#   sudo bash installer-jarvis.sh "<cle publique lecture>" "<cle publique action>" [origines]
#
# - utilisateur "jarvis", sans mot de passe (connexion par cle uniquement) ;
# - cle LECTURE : bridee par /usr/local/bin/jarvis-lecture (diagnostic seulement) ;
# - cle ACTION : sudo complet, mais utilisee uniquement par le service
#   jarvis-ssh APRES validation de Louis dans Talk ;
# - les deux cles ne sont acceptees que depuis la VM Docker (n8n), par defaut.
#
# Le fichier jarvis-lecture doit etre dans le meme dossier que ce script.
# Desinstallation : sudo bash installer-jarvis.sh --desinstaller
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "A lancer en root (sudo)."; exit 1; }
ICI="$(cd "$(dirname "$0")" && pwd)"

if [ "${1:-}" = "--desinstaller" ]; then
  rm -f /etc/sudoers.d/jarvis /usr/local/bin/jarvis-lecture
  if id jarvis >/dev/null 2>&1; then userdel -r jarvis 2>/dev/null || userdel jarvis; fi
  echo "Acces de Jarvis supprime."
  exit 0
fi

CLE_LECTURE="${1:?cle publique lecture manquante}"
CLE_ACTION="${2:?cle publique action manquante}"
ORIGINES="${3:-192.168.1.14,172.16.0.0/12}"

for cle in "$CLE_LECTURE" "$CLE_ACTION"; do
  [[ "$cle" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || { echo "Cle invalide : $cle"; exit 1; }
done
[ -f "$ICI/jarvis-lecture" ] || { echo "jarvis-lecture introuvable a cote du script"; exit 1; }

# sudo (absent par defaut sur Proxmox)
command -v sudo >/dev/null || { apt-get update -qq && apt-get install -y -qq sudo; }

# Utilisateur dedie, sans mot de passe utilisable
if ! id jarvis >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --comment "Jarvis (majordome IA)" jarvis
fi
# Mot de passe inutilisable ("*") : connexion par cle uniquement. (passwd -l
# "verrouillerait" le compte, ce que certains sshd refusent meme avec une cle.)
usermod -p "*" jarvis

install -m 0755 -o root -g root "$ICI/jarvis-lecture" /usr/local/bin/jarvis-lecture

echo "jarvis ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/jarvis
chmod 0440 /etc/sudoers.d/jarvis
visudo -cf /etc/sudoers.d/jarvis >/dev/null

install -d -m 0700 -o jarvis -g jarvis ~jarvis/.ssh
cat > ~jarvis/.ssh/authorized_keys <<EOF
# Gere par installer-jarvis.sh -- ne pas modifier a la main
from="$ORIGINES",command="/usr/local/bin/jarvis-lecture",restrict $CLE_LECTURE
from="$ORIGINES",restrict $CLE_ACTION
EOF
chown jarvis:jarvis ~jarvis/.ssh/authorized_keys
chmod 0600 ~jarvis/.ssh/authorized_keys

echo "Acces de Jarvis installe sur $(hostname) (origines autorisees : $ORIGINES)."
