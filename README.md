# Majordome : OpenClaw + n8n + Ollama

Un agent personnel joignable partout via Nextcloud Talk, qui tourne 24/7 sur
le ML150. Il utilise l'IA locale du PC quand celui-ci est allume, et une IA
cloud gratuite sinon.

## Architecture

```
 Louis (telephone, partout)
   │  application Nextcloud Talk
   ▼
 Nextcloud (.12) ──webhook signe──▶ VM 106 majordome (.22) : OpenClaw
                                     │                 │
                         outils MCP (jeton)        modeles
                                     ▼                 ├─▶ PC (.29) RX 6800, Ollama qwen3:14b
                       n8n (VM Docker .14)             └─▶ PC eteint : Gemini 3 Flash, puis Mistral Small (gratuits)
                       ├─ Prometheus / Alertmanager (.19)
                       ├─ Agenda Nextcloud
                       ├─ Home Assistant (.21) : lecture + scripts autorises
                       ├─ Seerr (films)
                       ├─ Paperless (lecture, sensible)
                       └─ crons : briefing 7h, bilan 21h ──▶ /hooks/agent
```

### Les deux salons Talk

| Salon | Agent | Modele | Outils sensibles |
|---|---|---|---|
| **Majordome** | `majordome` | Local, puis Gemini, puis Mistral | Aucun |
| **Majordome prive** | `prive` | **Local uniquement** (PC eteint = pas de reponse) | Paperless |

La regle « sensible = local uniquement » est appliquee par la configuration,
pas seulement par les instructions :

- l'agent `majordome` n'a pas acces aux outils `n8n-sensible__*` (`tools.deny`) ;
- l'agent `prive` n'a aucun secours cloud (`fallbacks: []`) ;
- la memoire (embeddings) est calculee par Ollama, jamais envoyee a OpenAI ;
- les deux agents ont des espaces memoire separes.

### Securite

- **Aucun port public ajoute** : Talk passe par le Nextcloud deja expose, et
  tout le reste circule sur le LAN.
- **Aucun mot de passe connu du majordome** : les jetons HA, Paperless,
  Nextcloud et Seerr restent chiffres dans n8n. Chaque action passe par un
  workflow et apparait dans l'historique d'execution de n8n.
- **Maison** : le majordome lit l'etat de HA, mais ne peut lancer que des
  **scripts HA** que tu as crees toi-meme (liste blanche de fait).
- **Pare-feu Proxmox** sur la VM : pas d'acces a Proxmox, iLO, Vaultwarden
  ni a l'admin NPM.
- **Pas d'execution de commandes** (`group:runtime` interdit) ni de navigateur.

## Contenu du depot

| Chemin | Role |
|---|---|
| `openclaw/openclaw.json5` | Configuration OpenClaw (agents, modeles, Talk, MCP, hooks) |
| `openclaw/.env.example` | Secrets OpenClaw (`~/.openclaw/.env`) |
| `openclaw/workspace-majordome/` | Personnalite, regles et memoire du majordome |
| `openclaw/workspace-prive/` | Idem pour l'agent prive |
| `n8n/docker-compose.yml`, `n8n/.env.example` | Stack n8n + Postgres |
| `n8n/workflows/` | 3 workflows : outils MCP, outils sensibles, briefing/bilan |
| `n8n/installer.sh`, `n8n/majordome.env.example` | Import des identifiants et workflows en une commande |
| `n8n/outils/generer-workflows.py` | Source des workflows (modifier ici, puis regenerer) |
| `infra/proxmox-106.fw` | Pare-feu de la VM majordome |
| `infra/pc-ollama.sh` | Reglage d'Ollama et du pare-feu sur le PC |

## Ce qui a ete teste

Teste avec OpenClaw 2026.9.8 et n8n 2.42.5, dans un environnement de test
(services simules, pas ton infra) :

- `openclaw config validate` et `openclaw config patch` passent.
- `installer.sh` importe les 6 identifiants et les 3 workflows, puis les publie.
- OpenClaw decouvre les 8 outils et les 2 outils sensibles via MCP. Chaque
  outil envoie la bonne requete, avec la bonne authentification. Un mauvais
  jeton est refuse (403).
- Le briefing n8n appelle `/hooks/agent` et OpenClaw l'accepte. L'agent `prive`
  est refuse par le hook.
- PC injoignable : OpenClaw le detecte et bascule sur Gemini.
- Le webhook Talk est verifie par signature HMAC.

**Pas encore confirme** : le format exact de l'expediteur Talk dans la liste
blanche (`tobilianok` ou `users/tobilianok`). Les deux formes sont listees ; voir
l'etape 6.

---

## Installation pas a pas

Compte environ 1h30. Fais-le **apres** avoir mis en place les sauvegardes
vzdump (point n°1 de ton rapport d'infra) : la memoire du majordome doit etre
sauvegardee.

### 1. Le PC (192.168.1.29)

```bash
sudo ./infra/pc-ollama.sh
```

Ce script expose Ollama, telecharge `qwen3:14b` et `qwen3-embedding:0.6b`, et
limite le port 11434 a la VM majordome. Si `ufw` n'est pas actif, active-le
avec `sudo ufw enable`, apres avoir autorise ton SSH si tu en as besoin.

Verifier depuis une autre machine du LAN : `curl http://192.168.1.29:11434/api/tags`
doit etre **refuse**. Depuis la VM majordome, apres l'etape 2, il doit repondre.

### 2. La VM majordome (Proxmox)

1. Cree la VM 106 : Ubuntu Server 26.04, 2 vCPU, 4 Go de RAM, 32 Go de disque
   sur `vmstorage`, carte reseau sur `vmbr0`, IP fixe **192.168.1.22**.
2. Copie `infra/proxmox-106.fw` dans `/etc/pve/firewall/106.fw` sur le ML150.
   Active ensuite le pare-feu dans VM > Pare-feu > Options et sur `net0`.
3. Ajoute la VM 106 au job vzdump.

### 3. Les comptes et jetons

| Ou | Quoi |
|---|---|
| Google AI Studio | Cle API Gemini (gratuite) |
| Mistral La Plateforme | Cle API, offre gratuite « Experiment » |
| Nextcloud | Utilisateur `majordome`. Partage-lui ton agenda **en lecture seule** et cree-lui un mot de passe d'application. |
| Home Assistant | Utilisateur `majordome` (non admin) et un jeton longue duree. Cree les scripts que tu autorises, par exemple `script.majordome_tout_eteindre`. |
| Paperless | Utilisateur en **lecture seule** et son jeton API |
| Seerr | Parametres > General > Cle API |

Tous ces secrets vont dans Vaultwarden.

### 4. Nextcloud Talk : le bot et les salons

Sur la VM Nextcloud (.12) :

```bash
# Autoriser Nextcloud a appeler une IP du LAN (le webhook du majordome)
sudo -u www-data php occ config:system:set allow_local_remote_servers --value=true --type=boolean

# Creer le bot (le secret doit faire au moins 40 caracteres)
SECRET=$(openssl rand -hex 32); echo "$SECRET"   # a garder pour l'etape 5
sudo -u www-data php occ talk:bot:install "Majordome" "$SECRET" \
  "http://192.168.1.22:18789/nextcloud-talk-webhook" \
  --feature webhook --feature response --feature reaction
sudo -u www-data php occ talk:bot:list
```

Si Talk refuse une URL en `http://`, publie la route dans NPM : un hote interne
`majordome.louisrousseaux.fr` vers `192.168.1.22:18789`, avec Let's Encrypt et
une liste d'acces LAN. Utilise alors cette URL en `https://`.

Dans Talk, cree deux conversations : **Majordome** et **Majordome prive**.
Dans les parametres de chacune, onglet Bots, active « Majordome ». Note ensuite
le **jeton** de chaque salon, c'est la fin de son URL `.../call/<jeton>`.

### 5. OpenClaw sur la VM majordome

```bash
# Node 24 + OpenClaw, sans l'assistant interactif
curl -fsSL https://openclaw.ai/install.sh | bash -s -- --no-onboard

# Plugins : Talk et Mistral
openclaw plugins install @openclaw/nextcloud-talk
openclaw plugins install @openclaw/mistral-provider

# Secrets
cp openclaw/.env.example ~/.openclaw/.env && chmod 600 ~/.openclaw/.env
nano ~/.openclaw/.env          # remplir toutes les valeurs

# Charger le catalogue Mistral (a besoin de la vraie cle)
set -a; . ~/.openclaw/.env; set +a
openclaw models list --refresh --provider mistral

# Workspaces (personnalite + memoire)
cp -r openclaw/workspace-majordome openclaw/workspace-prive ~/.openclaw/

# Configuration : remplacer les jetons de salon, puis FUSIONNER (ne pas copier
# le fichier par-dessus : OpenClaw restaurerait l'ancienne version)
sed -e 's/JETON_SALON_MAJORDOME/<jeton salon Majordome>/g' \
    -e 's/JETON_SALON_PRIVE/<jeton salon prive>/g' \
    openclaw/openclaw.json5 > /tmp/majordome.json5
openclaw config patch --file /tmp/majordome.json5
openclaw config validate

# Service systemd (utilisateur) + demarrage, actif meme sans session ouverte
sudo loginctl enable-linger "$USER"
openclaw gateway install
openclaw gateway restart
openclaw doctor
openclaw mcp doctor n8n-outils --probe     # apres l'etape 7
openclaw channels status --probe
```

Si `NEXTCLOUD_URL` resout vers une IP privee (DNS local), decommente
`network.dangerouslyAllowPrivateNetwork` dans la section Talk de la config.

### 6. Premier message

Ecris « Salut » dans le salon **Majordome**, puis suis les logs :

```bash
openclaw logs --follow
```

- Si tu vois `drop group sender users/tobilianok (reason=group_policy_not_allowlisted)`,
  copie l'identifiant exact affiche dans `groupAllowFrom` :
  `openclaw config set channels.nextcloud-talk.groupAllowFrom '["users/tobilianok"]' --strict-json`.
- Si tout va bien, le majordome lance l'entretien de decouverte de
  `BOOTSTRAP.md`.

Fais ensuite le test de la regle sensible :
1. Eteins le PC et ecris dans **Majordome** : la reponse vient de Gemini.
2. Ecris dans **Majordome prive** : pas de reponse, une erreur dans les logs.
   C'est le comportement voulu.

### 7. n8n sur la VM Docker (192.168.1.14)

```bash
sudo mkdir -p /opt/stacks/n8n && cd /opt/stacks/n8n
# copier ici le contenu du dossier n8n/ du depot
cp .env.example .env && chmod 600 .env && nano .env
mkdir -p data postgres import && sudo chown 1000:1000 data
docker compose up -d

# Identifiants + workflows en une commande
cp majordome.env.example majordome.env && chmod 600 majordome.env && nano majordome.env
sudo ./installer.sh
docker compose restart n8n
rm majordome.env
```

Ajoute ensuite un hote NPM `n8n.louisrousseaux.fr` vers `192.168.1.14:5678`,
avec une liste d'acces LAN/Tailscale, et cree le compte proprietaire de n8n.
Sauvegarde `N8N_ENCRYPTION_KEY` dans Vaultwarden.

### 8. Verifications finales

- [ ] `openclaw mcp doctor n8n-outils --probe` et `n8n-sensible` sont `ok`.
- [ ] Dans **Majordome**, « comment va l'infra ? » liste les alertes actives.
- [ ] Dans **Majordome**, « qu'est-ce que j'ai demain ? » lit l'agenda.
- [ ] Dans **Majordome prive** (PC allume), « retrouve ma derniere facture EDF »
      interroge Paperless.
- [ ] Le lendemain a 7h, le briefing arrive dans Talk.
- [ ] L'historique d'execution de n8n montre chaque appel d'outil.

## Faire evoluer le majordome

- **Ajouter un outil** : ajoute un `outil_http(...)` dans
  `n8n/outils/generer-workflows.py`, puis lance
  `python3 n8n/outils/generer-workflows.py` et `./installer.sh`.
- **Changer de modele local** : modifie `models.providers.ollama.models` et
  `agents.entries.*.model.primary`. Sur 16 Go de VRAM, `qwen3:14b` avec un
  contexte de 32k est un bon equilibre.
- **Sa memoire** se trouve dans `~/.openclaw/workspace-*` sur la VM. Elle est
  lisible et modifiable a la main, et sauvegardee avec la VM.

## Limites connues

- Les offres cloud gratuites peuvent utiliser tes echanges pour entrainer
  leurs modeles. C'est pour ca que le salon **Majordome** n'a ni outil ni
  memoire sensible.
- L'agenda est lu via l'export iCalendar de Nextcloud : lecture seule, pas de
  creation d'evenement pour l'instant.
- Si le ML150 tombe, le majordome tombe avec lui (meme limite que tes alertes
  Talk). Le dead man's switch healthchecks.io reste a faire.
