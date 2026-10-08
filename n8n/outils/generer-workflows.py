#!/usr/bin/env python3
"""Genere les workflows n8n du majordome (n8n/workflows/*.json).

Les fichiers generes contiennent des marqueurs @@VARIABLE@@ que
installer.sh remplace par les valeurs de majordome.env avant l'import.
Modifier ce script plutot que les JSON, puis relancer :
    python3 n8n/outils/generer-workflows.py
"""
import json
import pathlib

SORTIE = pathlib.Path(__file__).resolve().parent.parent / "workflows"

# Identifiants fixes : installer.sh s'en sert pour importer et publier.
CRED = {
    "mcp": {"id": "majCredMcpBearer", "name": "Majordome - jeton MCP", "type": "httpBearerAuth"},
    "ha": {"id": "majCredHomeAssist", "name": "Majordome - Home Assistant", "type": "httpHeaderAuth"},
    "nc": {"id": "majCredNextcloud1", "name": "Majordome - Nextcloud", "type": "httpBasicAuth"},
    "seerr": {"id": "majCredSeerrApi01", "name": "Majordome - Seerr", "type": "httpHeaderAuth"},
    "paperless": {"id": "majCredPaperless1", "name": "Majordome - Paperless (lecture)", "type": "httpHeaderAuth"},
    "hooks": {"id": "majCredOcHooks001", "name": "Majordome - hooks OpenClaw", "type": "httpBearerAuth"},
}


def cred(cle):
    c = CRED[cle]
    return {c["type"]: {"id": c["id"], "name": c["name"]}}


def outil_http(nom, position, description, methode, url, cle_cred=None,
               query=None, corps=None):
    """Noeud 'HTTP Request Tool' branche sur le MCP Server Trigger."""
    p = {
        "toolDescription": description,
        "method": methode,
        "url": url,
        "options": {"timeout": 20000},
    }
    if cle_cred:
        p["authentication"] = "genericCredentialType"
        p["genericAuthType"] = CRED[cle_cred]["type"]
    if query:
        p["sendQuery"] = True
        p["queryParameters"] = {"parameters": [{"name": k, "value": v} for k, v in query]}
    if corps:
        p["sendBody"] = True
        p["specifyBody"] = "json"
        p["jsonBody"] = corps
    noeud = {
        "parameters": p,
        "id": f"outil-{nom}",
        "name": nom,
        "type": "n8n-nodes-base.httpRequestTool",
        "typeVersion": 4.2,
        "position": position,
    }
    if cle_cred:
        noeud["credentials"] = cred(cle_cred)
    return noeud


def serveur_mcp(wf_id, nom, chemin, instructions, outils):
    trigger = {
        "parameters": {
            "authentication": "bearerAuth",
            "path": chemin,
            "instructions": instructions,
        },
        "id": "mcp-trigger",
        "name": "MCP Server Trigger",
        "type": "@n8n/n8n-nodes-langchain.mcpTrigger",
        "typeVersion": 2.1,
        "position": [0, 0],
        "webhookId": f"{chemin}-0000-4000-8000-majordome",
        "credentials": cred("mcp"),
    }
    connexions = {
        o["name"]: {"ai_tool": [[{"node": "MCP Server Trigger", "type": "ai_tool", "index": 0}]]}
        for o in outils
    }
    return {
        "id": wf_id,
        "name": nom,
        "active": False,
        "nodes": [trigger, *outils],
        "connections": connexions,
        "settings": {"executionOrder": "v1", "saveDataSuccessExecution": "all"},
        "tags": [],
    }


def ai(cle, description, type_="string"):
    return "{{ $fromAI('%s', `%s`, '%s') }}" % (cle, description, type_)


# --- Serveur MCP "majordome-outils" : le quotidien, rien de sensible -------
outils = [
    outil_http(
        "infra_alertes", [-400, 300],
        "Liste les alertes Prometheus actuellement actives sur l'infra de Louis "
        "(nom, severite, machine, depuis quand). A utiliser pour 'comment va l'infra ?'.",
        "GET", "@@ALERTMANAGER_URL@@/api/v2/alerts",
        query=[("active", "true"), ("silenced", "false"), ("inhibited", "false")],
    ),
    outil_http(
        "infra_prometheus", [-200, 300],
        "Execute une requete PromQL instantanee en lecture seule sur le Prometheus "
        "de l'infra (ex: node_filesystem_avail_bytes, up, zfs_pool_health). "
        "Renvoie le JSON brut de /api/v1/query.",
        "GET", "@@PROMETHEUS_URL@@/api/v1/query",
        query=[("query", "=" + ai("promql", "Requete PromQL a executer"))],
    ),
    outil_http(
        "agenda", [0, 300],
        "Lit l'agenda Nextcloud de Louis sur les N prochains jours (evenements "
        "recurrents deplies). Renvoie du iCalendar (VEVENT: SUMMARY, DTSTART, "
        "DTEND, LOCATION). Heures en Europe/Paris sauf mention contraire.",
        "GET",
        "=@@NEXTCLOUD_URL@@@@NC_CALENDAR_PATH@@?export&expand=1"
        "&start={{ Math.floor(Date.now() / 1000) }}"
        "&end={{ Math.floor(Date.now() / 1000) + 86400 * "
        + "$fromAI('jours', `Nombre de jours a lire a partir de maintenant (1 a 31)`, 'number') }}",
        cle_cred="nc",
    ),
    outil_http(
        "meteo", [200, 300],
        "Previsions meteo du domicile de Louis pour les prochains jours "
        "(temperatures min/max, pluie, vent, code meteo WMO).",
        "GET", "https://api.open-meteo.com/v1/forecast",
        query=[
            ("latitude", "@@METEO_LAT@@"),
            ("longitude", "@@METEO_LON@@"),
            ("daily", "weather_code,temperature_2m_max,temperature_2m_min,"
                      "precipitation_sum,precipitation_probability_max,wind_speed_10m_max"),
            ("current", "temperature_2m,weather_code,wind_speed_10m"),
            ("timezone", "Europe/Paris"),
            ("forecast_days", "=" + ai("jours", "Nombre de jours de prevision (1 a 7)", "number")),
        ],
    ),
    outil_http(
        "maison_lecture", [400, 300],
        "Lit l'etat de la maison dans Home Assistant en evaluant un template Jinja "
        "(lecture seule, ne change rien). Exemples : "
        "\"{{ states('sensor.temperature_salon') }}\", "
        "\"{{ states.light | selectattr('state','eq','on') | map(attribute='name') | list }}\".",
        "POST", "@@HA_URL@@/api/template", cle_cred="ha",
        corps="={{ JSON.stringify({ template: $fromAI('template', `Template Jinja Home Assistant a evaluer`, 'string') }) }}",
    ),
    outil_http(
        "maison_script", [600, 300],
        "Lance un script Home Assistant (seule action possible sur la maison). "
        "Les scripts autorises sont ceux que Louis a crees dans HA, ex: "
        "script.majordome_tout_eteindre. Demander confirmation a Louis avant.",
        "POST", "@@HA_URL@@/api/services/script/turn_on", cle_cred="ha",
        corps="={{ JSON.stringify({ entity_id: $fromAI('script', `Entite du script, ex: script.majordome_tout_eteindre`, 'string') }) }}",
    ),
    outil_http(
        "films_recherche", [800, 300],
        "Cherche un film ou une serie dans Seerr (TMDB) et indique s'il est deja "
        "disponible ou demande. Renvoie id, mediaType, titre, annee.",
        "GET", "@@SEERR_URL@@/api/v1/search", cle_cred="seerr",
        query=[
            ("query", "=" + ai("titre", "Titre du film ou de la serie")),
            ("language", "fr"),
        ],
    ),
    outil_http(
        "films_demande", [1000, 300],
        "Demande un film ou une serie dans Seerr (telechargement automatique ensuite). "
        "Utiliser l'id et le mediaType renvoyes par films_recherche. "
        "Demander confirmation a Louis avant.",
        "POST", "@@SEERR_URL@@/api/v1/request", cle_cred="seerr",
        corps="={{ JSON.stringify({ mediaType: $fromAI('mediaType', `movie ou tv`, 'string'), "
              "mediaId: $fromAI('mediaId', `Identifiant TMDB renvoye par films_recherche`, 'number'), "
              "seasons: $fromAI('mediaType', `movie ou tv`, 'string') === 'tv' ? 'all' : undefined }) }}",
    ),
]
outils_wf = serveur_mcp(
    "majOutilsMcp0001", "Majordome - outils (MCP)", "majordome-outils",
    "Outils du quotidien du majordome de Louis : infra, agenda, meteo, maison, films. "
    "Aucune donnee sensible.",
    outils,
)

# --- Serveur MCP "majordome-sensible" : reserve a l'agent prive (local) ----
sensibles = [
    outil_http(
        "paperless_recherche", [-100, 300],
        "Recherche plein texte dans les documents Paperless de Louis (factures, "
        "impots, banque, sante...). Renvoie id, titre, date, correspondant, type.",
        "GET", "@@PAPERLESS_URL@@/api/documents/", cle_cred="paperless",
        query=[
            ("query", "=" + ai("recherche", "Mots-cles de recherche")),
            ("page_size", "10"),
            ("fields", "id,title,created,correspondent,document_type,tags"),
        ],
    ),
    outil_http(
        "paperless_document", [100, 300],
        "Lit le texte (OCR) d'un document Paperless a partir de son id "
        "(obtenu avec paperless_recherche).",
        "GET",
        "=@@PAPERLESS_URL@@/api/documents/{{ $fromAI('id', `Identifiant du document`, 'number') }}/",
        cle_cred="paperless",
        query=[("fields", "id,title,created,correspondent,document_type,content")],
    ),
]
sensible_wf = serveur_mcp(
    "majSensibleMcp01", "Majordome - sensible (MCP)", "majordome-sensible",
    "Outils sensibles du majordome de Louis (documents administratifs). "
    "Reserve a l'agent local.",
    sensibles,
)


# --- Rythme de la journee : n8n reveille le majordome ---------------------
def declencheur(nom, heure, position):
    return {
        "parameters": {"rule": {"interval": [{"field": "cronExpression", "expression": f"0 {heure} * * *"}]}},
        "id": f"trigger-{nom}",
        "name": nom,
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": position,
    }


def appel_majordome(nom, consigne, position):
    corps = {
        "message": consigne,
        "name": nom,
        "agentId": "majordome",
        "channel": "nextcloud-talk",
        "to": "room:@@TALK_ROOM_MAJORDOME@@",
    }
    return {
        "parameters": {
            "method": "POST",
            "url": "@@OPENCLAW_URL@@/hooks/agent",
            "authentication": "genericCredentialType",
            "genericAuthType": "httpBearerAuth",
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": json.dumps(corps, ensure_ascii=False, indent=2),
            "options": {"timeout": 30000},
        },
        "id": f"appel-{nom}",
        "name": nom,
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": position,
        "credentials": cred("hooks"),
        "retryOnFail": True,
        "maxTries": 3,
        "waitBetweenTries": 5000,
    }


BRIEFING = (
    "C'est l'heure du briefing du matin de Louis. Utilise tes outils : agenda "
    "(aujourd'hui et demain), meteo (1 jour), infra_alertes. Ecris un message "
    "court en francais : la journee (rendez-vous avec heures), la meteo en une "
    "ligne (et s'il faut un parapluie), puis l'infra seulement si une alerte est "
    "active. Ajoute au plus une remarque utile tiree de ce que tu sais de Louis. "
    "Pas de titre, pas de formule de politesse."
)
BILAN = (
    "C'est l'heure du bilan du soir. Relis ta note du jour dans memory/, mets a "
    "jour USER.md et MEMORY.md avec ce que tu as appris aujourd'hui (rien de "
    "sensible). Puis envoie a Louis un message tres court : ce qui l'attend "
    "demain (agenda) et, seulement s'il y en a, une alerte infra ou un point "
    "en suspens. S'il n'y a rien a signaler, dis-le en une ligne."
)
rythme_wf = {
    "id": "majRythme0000001",
    "name": "Majordome - briefing et bilan",
    "active": False,
    "nodes": [
        declencheur("Chaque matin 7h", 7, [0, 0]),
        appel_majordome("Briefing du matin", BRIEFING, [300, 0]),
        declencheur("Chaque soir 21h", 21, [0, 250]),
        appel_majordome("Bilan du soir", BILAN, [300, 250]),
    ],
    "connections": {
        "Chaque matin 7h": {"main": [[{"node": "Briefing du matin", "type": "main", "index": 0}]]},
        "Chaque soir 21h": {"main": [[{"node": "Bilan du soir", "type": "main", "index": 0}]]},
    },
    "settings": {"executionOrder": "v1", "timezone": "Europe/Paris"},
    "tags": [],
}

SORTIE.mkdir(parents=True, exist_ok=True)
for fichier, wf in [
    ("majordome-outils.json", outils_wf),
    ("majordome-sensible.json", sensible_wf),
    ("majordome-rythme.json", rythme_wf),
]:
    (SORTIE / fichier).write_text(json.dumps(wf, ensure_ascii=False, indent=2) + "\n")
    print("ecrit", SORTIE / fichier)
