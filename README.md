# Majordome : OpenClaw + n8n + IA gratuites anonymisées

Un agent personnel joignable partout via Nextcloud Talk, qui tourne 24/7 sur
le ML150. Il réfléchit grâce à plusieurs **IA cloud gratuites**. Tout ce qui
part vers elles passe d'abord par un **anonymiseur** local, qui remplace les
noms, adresses, téléphones, e-mails, IBAN… par des pseudonymes, et remet les
vraies valeurs dans les réponses. Rien ne tourne sur ton PC.

## Architecture

```
 Louis (téléphone, partout)
   │  application Nextcloud Talk
   ▼
 Nextcloud (.12) ──webhook signé──▶ VM 106 majordome (.16)
                                   ┌──────────────────────────────────────────────┐
                                   │ OpenClaw (aucune clé d'IA)                    │
                                   │    │ texte en clair                           │
                                   │    ▼                                          │
                                   │ Anonymiseur (127.0.0.1:8090)                  │
                                   │  • pseudonymise : [PERSONNE_3], [IBAN_1]…     │
                                   │  • routeur : choisit l'IA gratuite disponible │
                                   │  • rétablit les vraies valeurs au retour      │
                                   └──────┬──────────────────────────┬─────────────┘
                     outils MCP (jeton)   │                          │ pseudonymes seulement
                                          ▼                          ▼
                    n8n (VM Docker .14)                   Gemini · Mistral · NVIDIA NIM
                    ├─ Prometheus / Alertmanager (.19)    Groq · GitHub Models · OpenRouter
                    ├─ Agenda Nextcloud
                    ├─ Home Assistant (.21) : via Assist (appareils exposés)
                    ├─ Seerr (films)
                    ├─ Paperless (lecture)
                    └─ crons : briefing 7 h, bilan 21 h ──▶ /hooks/agent
```

### Les garanties

- **OpenClaw n'a aucune clé d'IA.** Il ne peut donc rien envoyer vers le
  cloud sans passer par l'anonymiseur.
- **L'anonymiseur masque :**
  - ce qui est dans ton **dictionnaire personnel** (toi, tes proches, ton
    adresse, tes domaines) : c'est la protection la plus fiable ;
  - les e-mails, téléphones, IBAN, cartes bancaires, numéros de sécu,
    adresses postales et IP publiques, repérés par des règles ;
  - les **noms de personnes**, détectés automatiquement en français (spaCy).
- **Le coffre des pseudonymes reste sur la VM.** Un même nom donne toujours
  le même pseudonyme, ce qui permet à l'IA de suivre la conversation.
- **La recherche web est désactivée** : elle partirait sans anonymisation.
  La mémoire est cherchée par mots-clés, en local.
- **Aucun port public ajouté**, et un pare-feu Proxmox sur la VM.
- **Aucun mot de passe connu du majordome** : les accès HA, Paperless,
  Nextcloud et Seerr restent chiffrés dans n8n.

### Les limites, honnêtement

- L'anonymisation masque **qui**, pas **quoi** : « facture d'électricité de
  84 € » ou « rendez-vous chez le dentiste » partent tels quels, sans
  lien avec ton identité.
- La détection automatique n'est pas parfaite. Un nom rare, ou mal écrit,
  peut passer. **Remplis le dictionnaire** avec tes proches, et vérifie le
  journal des envois les premiers jours (étape 8).
- Les offres gratuites changent souvent (quotas, modèles). Le routeur
  s'adapte aux refus, mais il faudra parfois mettre à jour
  `anonymiseur/config.yaml`.

## Les IA gratuites et le routeur

| Profil | Pour quoi | Ordre d'essai |
|---|---|---|
| `auto` | Conversation et outils | NVIDIA GLM 5.3 → Gemini 3.5 Flash → Nemotron Super → Gemini 3 Flash → Gemini 3.8 Flash |
| `reflexion` | Briefing, bilan | Nemotron Ultra 550B → Gemini 3.8 Flash → GLM 5.3 → Gemini 3.5 Flash → Kimi K3 |
| `rapide` | Petites tâches sans outils | Groq gpt-oss-120b → Groq gpt-oss-20b → NVIDIA gpt-oss-20b → Gemini Flash-Lite |
| `conseil` | Écris **`!conseil`** dans ton message | 3 IA de fournisseurs différents répondent, une 4ᵉ fait la synthèse |

Comment le routeur choisit :
- Il **écarte les modèles trop petits** pour la requête : une requête du
  majordome fait 10 000 à 25 000 tokens.
- Il **compte les quotas** (par minute et par jour) et passe au suivant
  avant d'atteindre la limite.
- Après un refus (quota, panne), il met le modèle **en pause**. Un quota
  journalier épuisé reste en pause jusqu'à minuit UTC.
- Il expose ses **métriques à Prometheus** : modèle utilisé, refus, quotas.

## Contenu du dépôt

| Chemin | Rôle |
|---|---|
| `anonymiseur/` | Proxy d'anonymisation et routeur (Python, Docker), 30 tests |
| `anonymiseur/config.yaml` | Fournisseurs, modèles, limites, profils, mode conseil |
| `anonymiseur/dictionnaire.example.yaml` | **Ton dictionnaire personnel** (à remplir) |
| `openclaw/openclaw.json5` | Configuration OpenClaw |
| `openclaw/workspace-majordome/` | Personnalité, règles et mémoire du majordome |
| `n8n/` | Stack n8n, 3 workflows, script d'import |
| `infra/proxmox-106.fw` | Pare-feu de la VM majordome |
| `infra/prometheus-anonymiseur.yml` | Collecte des métriques et alertes |

## Ce qui a été testé

Avec OpenClaw 2026.9.8, n8n 2.42.5, spaCy 3.8 et `fr_core_news_lg`, dans un
environnement de test (faux fournisseurs d'IA, faux Nextcloud, pas ton infra) :

- **Chaîne complète** : message Talk signé → OpenClaw → anonymiseur → « IA »
  → réponse dans Talk. Avec la phrase « Salut Sophie m'a dit que Marc Lefebvre
  passe au 14 rue Victor Hugo 44000 Nantes. Dis à Inès que c'est OK.
  Son numéro : 07 98 76 54 32. » :
  - **aucune fuite**, ni dans le message, ni dans l'historique, ni dans le
    prompt système ;
  - les pseudonymes posés dans la réponse sont bien remplacés par les vrais
    noms dans Talk.
- **Contrôle d'accès Talk** : un expéditeur inconnu est rejeté,
  `users/tobilianok` est accepté.
- **30 tests automatisés** (`cd anonymiseur && python -m pytest tests`) :
  - anonymisation : aller-retour des pseudonymes, appels d'outils, texte
    encodé en JSON, agenda iCal (les dates sont gardées), pas de faux noms
    dans le prompt anglais d'OpenClaw ;
  - routeur : quotas, pauses, bascule, mode conseil, métriques.
- **n8n** : import, publication, les 10 outils MCP appelés pour de vrai.

**Pas testé ici** :
- la construction de l'image Docker, faute de Docker dans l'environnement de
  test (l'application, elle, a été lancée avec la même commande) ;
- les vraies API gratuites : leurs identifiants de modèles sont à vérifier
  à l'étape 6.

---

## Installation pas à pas

Compte environ 2 heures. Valide chaque étape avant de passer à la suivante.

### 0. Sauvegardes (avant tout)

Mets en place le job vzdump (point n°1 de ton rapport d'infra). Au minimum,
fais un vzdump manuel de Nextcloud (100) et de la VM Docker (102) avant de
commencer.

### 1. Les comptes d'IA gratuits

Tous les secrets vont dans Vaultwarden.

| Fournisseur | Où | À faire |
|---|---|---|
| Google Gemini | aistudio.google.com → Get API key | Rien d'autre : en Europe, Google n'entraîne pas ses modèles sur les requêtes gratuites |
| Mistral | console.mistral.ai → plan *Experiment* (vérification par SMS) | **Admin Console → Privacy : désactive l'utilisation des données pour l'entraînement** |
| NVIDIA NIM | build.nvidia.com (NVIDIA Developer Program) | Génère une clé API |
| Groq | console.groq.com | Clé API |
| GitHub Models | github.com → Settings → Developer settings → Fine-grained token | Permission **Models : read-only** uniquement |
| OpenRouter | openrouter.ai → Keys | Clé API (dernier recours) |

Seuls Gemini et Mistral sont indispensables. Chaque fournisseur ajouté
augmente les quotas, et il suffit de laisser une clé vide pour le désactiver.

### 2. La VM majordome (Proxmox)

1. Crée la VM 106 :
   - Ubuntu Server 26.04 ;
   - 2 vCPU, **6 Go de RAM** (6144 Mo) (le modèle de langue de l'anonymiseur prend
     environ 1 Go), 40 Go de disque sur `vmstorage` ;
   - carte réseau sur `vmbr0`, IP fixe **192.168.1.16**.
     (Attention : .16 était l'IP prévue pour HAOS ; choisis-en une autre pour lui.)
2. Installe Docker : `curl -fsSL https://get.docker.com | sh`.
3. Copie `infra/proxmox-106.fw` dans `/etc/pve/firewall/106.fw` sur le
   ML150, puis active le pare-feu dans VM > Pare-feu > Options et sur `net0`.
4. Ajoute la VM 106 au job vzdump.
5. Récupère le dépôt :
   `git clone -b claude/sleepy-allen-gdy9mm https://github.com/tobilianok/openclaw.git ~/majordome`

**Validation** :
- `docker run --rm hello-world` fonctionne ;
- depuis la VM, `curl -I https://api.mistral.ai` répond ;
- depuis la VM, `ping 192.168.1.10` (Proxmox) **échoue**.

### 3. Les comptes de service

| Où | Quoi |
|---|---|
| Nextcloud | Utilisateur `majordome`. Partage-lui ton agenda **en lecture seule**, puis crée-lui un mot de passe d'application. |
| Home Assistant | Utilisateur `majordome` (non admin), avec un jeton longue durée. **Expose à Assist** les appareils que le majordome peut voir et piloter (Paramètres → Assistants vocaux → Exposer). Facultatif : des scripts autorisés, par exemple `script.majordome_tout_eteindre`. |
| Paperless | Utilisateur en **lecture seule**, avec son jeton d'API |
| Seerr | Paramètres > Général > Clé API |

### 4. n8n sur la VM Docker (192.168.1.14)

```bash
sudo mkdir -p /opt/stacks/n8n && sudo cp -r ~/majordome/n8n/. /opt/stacks/n8n/ && cd /opt/stacks/n8n
sudo cp .env.example .env && sudo chmod 600 .env && sudo nano .env
sudo mkdir -p data postgres import && sudo chown 1000:1000 data
sudo docker compose up -d

sudo cp majordome.env.example majordome.env && sudo chmod 600 majordome.env && sudo nano majordome.env
sudo ./installer.sh
sudo docker compose restart n8n
sudo rm majordome.env
```

Ajoute ensuite un hôte NPM `n8n.louisrousseaux.fr` vers `192.168.1.14:5678`,
avec une liste d'accès LAN/Tailscale, puis crée le compte propriétaire de
n8n. Sauvegarde `N8N_ENCRYPTION_KEY` dans Vaultwarden.

**Validation** :
- `installer.sh` affiche « Successfully imported 6 credentials » et
  « 3 workflows » ;
- dans n8n, les 3 workflows « Majordome » sont publiés.

### 5. Nextcloud Talk : le bot et le salon

Sur la VM Nextcloud (.12). Si Nextcloud tourne en Docker, préfixe les
commandes `occ` avec `docker exec -u www-data <conteneur>`.

```bash
# Autoriser Nextcloud à appeler une IP du LAN (le webhook du majordome)
sudo -u www-data php occ config:system:set allow_local_remote_servers --value=true --type=boolean

SECRET=$(openssl rand -hex 32); echo "$SECRET"   # à garder pour l'étape 7
sudo -u www-data php occ talk:bot:install "Majordome" "$SECRET" \
  "http://192.168.1.16:18789/nextcloud-talk-webhook" \
  --feature webhook --feature response --feature reaction
sudo -u www-data php occ talk:bot:list
```

Si Talk refuse une URL en `http://`, publie la route dans NPM :
- un hôte interne `majordome.louisrousseaux.fr` vers `192.168.1.16:18789` ;
- avec Let's Encrypt et une liste d'accès LAN ;
- puis utilise cette URL en `https://` dans la commande `talk:bot:install`.

Dans Talk, crée la conversation **Majordome** et active le bot dans ses
paramètres (onglet Bots). Note ensuite le **jeton** du salon : c'est la fin
de son URL, `.../call/<jeton>`.

**Validation** : `talk:bot:list` montre le bot avec les fonctionnalités
`webhook`, `response` et `reaction`.

### 6. L'anonymiseur (VM majordome)

```bash
sudo mkdir -p /opt/stacks/anonymiseur && sudo cp -r ~/majordome/anonymiseur/. /opt/stacks/anonymiseur/
cd /opt/stacks/anonymiseur
sudo cp .env.example .env && sudo chmod 600 .env && sudo nano .env      # clés de l'étape 1
sudo cp dictionnaire.example.yaml dictionnaire.yaml && sudo chown 1000 dictionnaire.yaml && sudo chmod 600 dictionnaire.yaml
sudo nano dictionnaire.yaml        # tes proches, ton adresse, ton employeur…
sudo mkdir -p data && sudo chown 1000:1000 data
sudo docker compose up -d --build  # quelques minutes (modèle de langue de 570 Mo)
```

**Validation** (le jeton est la valeur de `ANONYMISEUR_TOKEN`) :

```bash
T=<ANONYMISEUR_TOKEN>
curl -s localhost:8090/sante
# → la liste des routes actives : elles doivent correspondre aux clés remplies

curl -s localhost:8090/v1/anonymiser -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' \
  -d '{"texte":"Louis doit rappeler Marie Durand au 06 12 34 56 78"}'
# → {"envoye":"[UTILISATEUR] doit rappeler [PERSONNE_1] au [TELEPHONE_1]"}

curl -s localhost:8090/v1/chat/completions -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"Dis bonjour à Marie Durand en une phrase."}]}'
# → une vraie réponse d'IA qui contient "Marie Durand" (rétabli localement)
```

Si une route renvoie une erreur de modèle inconnu, corrige son `modele`
dans `config.yaml`, puis lance `docker compose restart`. Les erreurs sont
dans `docker compose logs`.

### 7. OpenClaw (VM majordome)

```bash
# Node 24 + OpenClaw, sans l'assistant interactif
curl -fsSL https://openclaw.ai/install.sh | bash -s -- --no-onboard
openclaw plugins install @openclaw/nextcloud-talk

# Secrets (aucune clé d'IA ici)
cp ~/majordome/openclaw/.env.example ~/.openclaw/.env && chmod 600 ~/.openclaw/.env
nano ~/.openclaw/.env

# Personnalité et mémoire
cp -r ~/majordome/openclaw/workspace-majordome ~/.openclaw/

# Configuration : remplacer le jeton du salon, puis FUSIONNER (ne pas copier
# le fichier par-dessus : OpenClaw restaurerait l'ancienne version)
sed 's/JETON_SALON_MAJORDOME/<jeton du salon>/g' \
    ~/majordome/openclaw/openclaw.json5 > /tmp/majordome.json5
openclaw config patch --file /tmp/majordome.json5
openclaw config validate

# Service (actif même sans session ouverte) + démarrage
sudo loginctl enable-linger "$USER"
openclaw gateway install
openclaw gateway restart
```

Si `NEXTCLOUD_URL` résout vers une IP privée (DNS local), décommente
`network.dangerouslyAllowPrivateNetwork` dans la section Talk de la config.

**Validation** :
- `openclaw doctor` ne signale pas d'erreur bloquante ;
- `openclaw mcp doctor n8n-outils --probe` et `n8n-sensible` sont `ok` ;
- `openclaw channels status --probe` : Nextcloud Talk est connecté.

### 8. Premier message et contrôle de l'anonymisation

1. Écris « Salut » dans le salon **Majordome**. Le majordome lance
   l'entretien de découverte, et ses réponses arrivent avec les vrais noms.
2. Vérifie **ce qui est réellement parti** vers les IA :
   ```bash
   sudo tail -n 1 /opt/stacks/anonymiseur/data/envois.jsonl | python3 -m json.tool | less
   ```
   Cherche ton nom, ceux de tes proches et ton adresse : ils ne doivent pas
   y être. Si un nom passe, ajoute-le au dictionnaire, puis lance
   `docker compose restart`.
3. Une fois rassuré, au bout de quelques jours, passe `journal_envois: null`
   dans `config.yaml` : le journal contient tes conversations.

**Si rien ne répond**, `openclaw logs --follow` montre la raison. Par
exemple, `drop group sender … group_policy_not_allowlisted` signifie que
`NEXTCLOUD_USER_ID` est faux.

### 9. Supervision et vérifications finales

Ajoute le job et les alertes de `infra/prometheus-anonymiseur.yml` à ton
Prometheus (.19).

- [ ] Dans Prometheus, la cible `anonymiseur` est `UP`.
- [ ] « Comment va l'infra ? » liste les alertes actives.
- [ ] « Qu'est-ce que j'ai demain ? » lit l'agenda.
- [ ] « Retrouve ma dernière facture EDF » interroge Paperless.
- [ ] « !conseil Je devrais passer à la fibre 8 Gb ? » donne une réponse
      synthétisée par plusieurs IA.
- [ ] Le lendemain à 7 h, le briefing arrive dans Talk.

## SSH sur l'infra (Jarvis assiste, Louis décide)

```
Jarvis ──outil MCP──▶ n8n ──▶ jarvis-ssh (VM Docker) ──SSH──▶ machines (utilisateur "jarvis")
  infra_ssh_lecture ............ clé LECTURE : commandes de diagnostic seulement,
                                 bridées CÔTÉ SERVEUR par /usr/local/bin/jarvis-lecture
  infra_ssh_demande_action ..... rien n'est exécuté : message dans Talk avec les
                                 commandes exactes + lien ✅/❌ (NPM + Authentik)
                                 → après TON clic : clé ACTION (sudo), résultat
                                 posté dans Talk, Jarvis prévenu
```

- Jarvis n'a **aucune clé** et ne voit **jamais** le lien de validation.
- **Garde-fous** (`n8n/jarvis-ssh/config.yaml`) : toute action qui couperait la VM
  de Jarvis (106), la VM Docker (102), n8n, NPM ou l'hyperviseur est refusée d'office.
- Les clés ne sont acceptées que depuis la VM Docker (`from=` dans `authorized_keys`).
- Retirer l'accès d'une machine : `sudo bash installer-jarvis.sh --desinstaller`.
- Tests : `infra/jarvis` (verrou de lecture) et `n8n/jarvis-ssh/tests` (bout en bout avec
  un vrai sshd : `JARVIS_TEST_SSH=1 python -m pytest tests`).

## Faire évoluer le majordome

- **Masquer un nouveau nom** : ajoute-le dans `dictionnaire.yaml`, puis
  `docker compose restart`.
- **Un faux positif** (un mot masqué à tort) : ajoute-le à la liste
  `jamais` du dictionnaire.
- **Ajouter ou changer une IA** : ajoute un fournisseur et une route dans
  `anonymiseur/config.yaml`, puis place la route dans un profil.
- **Ajouter un outil** : ajoute un `outil_http(...)` dans
  `n8n/outils/generer-workflows.py`, puis lance
  `python3 n8n/outils/generer-workflows.py` et `./installer.sh`.
- **Sa mémoire** se trouve dans `~/.openclaw/workspace-majordome` sur la
  VM, en clair (lisible et modifiable), et elle est sauvegardée avec la VM.
- **Le coffre des pseudonymes** (`/opt/stacks/anonymiseur/data/coffre.db`)
  est sauvegardé aussi. Si tu le perds, les pseudonymes changent, mais rien
  ne casse.

## Limites connues

- L'agenda est lu en lecture seule via l'export iCalendar de Nextcloud.
- Si le ML150 tombe, le majordome tombe avec lui (même limite que tes
  alertes Talk). Le dead man's switch healthchecks.io reste à faire.
- Pas d'images ni de pièces jointes : le majordome ne traite que du texte.
