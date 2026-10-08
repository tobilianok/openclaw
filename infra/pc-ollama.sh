#!/usr/bin/env bash
# A lancer sur le PC de Louis (Ubuntu 26.04, 192.168.1.29) avec sudo.
# Ollama est deja installe et expose sur le reseau : on garde ses reglages
# et on limite l'acces a la VM majordome.
set -euo pipefail

# Reglages du service Ollama
mkdir -p /etc/systemd/system/ollama.service.d
cat > /etc/systemd/system/ollama.service.d/majordome.conf <<'CONF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_FLASH_ATTENTION=1"
CONF
systemctl daemon-reload
systemctl restart ollama

# Modeles : chat + embeddings de la memoire
ollama pull qwen3:14b
ollama pull qwen3-embedding:0.6b

# Pare-feu : Ollama accessible uniquement depuis la VM majordome
ufw allow from 192.168.1.22 to any port 11434 proto tcp comment 'majordome'
ufw deny 11434/tcp comment 'ollama ferme au reste du reseau'
ufw status numbered
