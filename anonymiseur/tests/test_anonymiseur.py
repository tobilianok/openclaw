"""Tests de l'anonymiseur : python -m pytest tests (depuis anonymiseur/)."""
import json

import httpx
import pytest
import spacy
from fastapi.testclient import TestClient

from app.coffre import Coffre
from app.detection import Anonymiseur
from app.proxy import creer_app

DICO = {
    "personnes": [
        {"jeton": "UTILISATEUR", "affichage": "Louis",
         "formes": ["Louis Rousseaux", "Louis", "Rousseaux"]},
        {"affichage": "Marie", "formes": ["Marie Rousseaux", "Marie"]},
    ],
    "termes": [{"categorie": "DOMAINE", "formes": ["louisrousseaux.fr", "famillerousseaux.fr"]},
               {"categorie": "IDENTIFIANT", "formes": ["tobilianok"]}],
    "jamais": ["Proxmox", "Jellyfin", "Nextcloud"],
}


@pytest.fixture(scope="session")
def nlp():
    return spacy.load("fr_core_news_lg", disable=["parser", "lemmatizer"])


@pytest.fixture
def anon(tmp_path, nlp):
    return Anonymiseur(Coffre(str(tmp_path / "c.db")), DICO, {"PERSONNE": True}, nlp)


def test_dictionnaire_et_regles(anon):
    texte = ("Louis Rousseaux (louis.rousseaux@gmail.com, 06 12 34 56 78) habite 12 rue des Lilas 44000 Nantes. "
             "IBAN FR76 3000 6000 0112 3456 7890 189, carte 4970 1012 3456 7893. Secu 1 85 05 78 006 084 36. "
             "Marie a appele. Le serveur Proxmox est en 192.168.1.10, le site louisrousseaux.fr va bien.")
    sortie = anon.anonymiser(texte)
    for secret in ["Louis", "Rousseaux", "gmail", "06 12", "Lilas", "FR76", "4970", "1 85 05", "Marie",
                   "louisrousseaux.fr"]:
        assert secret not in sortie, (secret, sortie)
    assert "[UTILISATEUR]" in sortie
    assert "Proxmox" in sortie and "192.168.1.10" in sortie  # rien d'identifiant


def test_aller_retour(anon):
    texte = "Rappelle a Marie Rousseaux que Louis passe chez Paul Martin jeudi."
    sortie = anon.anonymiser(texte)
    assert "Paul Martin" not in sortie and "Marie" not in sortie
    assert anon.retablir(sortie) == texte.replace("Marie Rousseaux", "Marie")


def test_pseudonymes_stables(anon):
    a = anon.anonymiser("Paul Martin arrive.")
    b = anon.anonymiser("Je dois rappeler Paul Martin demain.")
    jeton = a.split()[0] + " " + a.split()[1] if not a.startswith("[") else a.split(" arrive")[0]
    assert jeton in b


def test_jetons_sans_crochets(anon):
    anon.anonymiser("Paul Martin")
    assert anon.retablir("Dis bonjour a PERSONNE_2 et a UTILISATEUR.") == "Dis bonjour a Paul Martin et a Louis."


def test_json_outil(anon):
    brut = json.dumps({"titre": "Dejeuner avec Paul Martin", "invites": ["marie@exemple.fr"], "n": 3})
    a = anon.anonymiser_json(brut)
    assert "Paul" not in a and "marie@" not in a and json.loads(a)["n"] == 3
    assert json.loads(anon.retablir_json(a)) == json.loads(brut)


def test_pas_de_double_anonymisation(anon):
    une = anon.anonymiser("Paul Martin et Louis")
    assert anon.anonymiser(une) == une


# --- Proxy de bout en bout, avec un faux fournisseur ------------------------

def faux_fournisseur(recus, statut_par_cle):
    def gerer(requete: httpx.Request):
        cle = requete.headers["authorization"].split()[-1]
        corps = json.loads(requete.content)
        recus.append((cle, corps))
        statut = statut_par_cle.get(cle, 200)
        if statut != 200:
            return httpx.Response(statut, json={"error": "quota"})
        # L'IA "repond" en reutilisant le jeton vu dans le resultat d'outil.
        import re
        trouve = re.search(r"\[PERSONNE_\d+\]", json.dumps(corps["messages"][-1]))
        jeton = trouve.group() if trouve else "[UTILISATEUR]"
        if "tools" not in corps:  # avis du conseil ou synthese : texte seul
            return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": f"Avis pour {jeton}."}}]})
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 1, "model": corps["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": f"Je note pour {jeton} et [UTILISATEUR].",
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "agenda_ajout", "arguments": json.dumps({"titre": f"Appeler {jeton}"})}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    return httpx.MockTransport(gerer)


@pytest.fixture
def proxy(tmp_path, nlp, monkeypatch):
    monkeypatch.setenv("ANONYMISEUR_TOKEN", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "cle-gemini")
    monkeypatch.setenv("MISTRAL_API_KEY", "cle-mistral")
    monkeypatch.setenv("GROQ_API_KEY", "")
    recus, statuts = [], {}
    config = {
        "fournisseurs": {
            "gemini": {"url": "https://g.test/v1", "cle_env": "GEMINI_API_KEY"},
            "mistral": {"url": "https://m.test/v1", "cle_env": "MISTRAL_API_KEY"},
            "groq": {"url": "https://q.test/v1", "cle_env": "GROQ_API_KEY"},
        },
        "routes": {
            "gemini-flash": {"fournisseur": "gemini", "modele": "gm"},
            "mistral-small": {"fournisseur": "mistral", "modele": "mm"},
            "groq-petit": {"fournisseur": "groq", "modele": "qm", "contexte": 50},
        },
        "profils": {"auto": ["gemini-flash", "mistral-small", "groq-petit"],
                    "reflexion": ["mistral-small", "gemini-flash"]},
        "ner": {"PERSONNE": True},
        "coffre": str(tmp_path / "c.db"),
        "etat_quotas": str(tmp_path / "quotas.json"),
        "journal_envois": str(tmp_path / "envois.jsonl"),
    }
    monkeypatch.setenv("ANONYMISEUR_DICTIONNAIRE", "")
    app = creer_app(config, nlp=nlp, client=httpx.AsyncClient(transport=faux_fournisseur(recus, statuts)))
    app.state.anon.__init__(app.state.anon.coffre, DICO, {"PERSONNE": True}, nlp)
    return TestClient(app), recus, statuts, tmp_path


DEMANDE = {
    "model": "auto",
    "messages": [
        {"role": "system", "content": "Tu es le majordome de Louis Rousseaux (tobilianok)."},
        {"role": "user", "content": [{"type": "text", "text": "Rappelle-moi d'appeler Paul Martin au 06 12 34 56 78."}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c0", "type": "function", "function": {
            "name": "agenda", "arguments": json.dumps({"note": "Paul Martin"})}}]},
        {"role": "tool", "tool_call_id": "c0", "content": "RDV avec Paul Martin, paul@martin.fr"},
    ],
    "tools": [{"type": "function", "function": {"name": "agenda", "parameters": {"type": "object"}}}],
    "max_completion_tokens": 100,
    "store": True,
}


def test_proxy_rien_ne_sort_en_clair(proxy):
    client, recus, _, tmp = proxy
    r = client.post("/v1/chat/completions", json=DEMANDE, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200, r.text
    envoye = json.dumps(recus[0][1], ensure_ascii=False)
    for secret in ["Louis", "Rousseaux", "tobilianok", "Paul", "Martin", "06 12", "paul@"]:
        assert secret not in envoye, secret
    assert "store" not in recus[0][1] and recus[0][1]["max_tokens"] == 100
    msg = r.json()["choices"][0]["message"]
    assert msg["content"] == "Je note pour Paul Martin et Louis."
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"titre": "Appeler Paul Martin"}
    assert "Paul" not in (tmp / "envois.jsonl").read_text()


def test_proxy_bascule_et_pause(proxy):
    client, recus, statuts, _ = proxy
    statuts["cle-gemini"] = 429
    h = {"Authorization": "Bearer secret"}
    r = client.post("/v1/chat/completions", json=DEMANDE, headers=h)
    assert r.status_code == 200 and "(mistral-small)" in r.json()["model"]
    assert [c for c, _ in recus] == ["cle-gemini", "cle-mistral"]
    recus.clear()
    client.post("/v1/chat/completions", json=DEMANDE, headers=h)
    assert [c for c, _ in recus] == ["cle-mistral"]  # gemini en pause


def test_proxy_flux_sse(proxy):
    client, _, _, _ = proxy
    demande = {**DEMANDE, "stream": True, "stream_options": {"include_usage": True}}
    r = client.post("/v1/chat/completions", json=demande, headers={"Authorization": "Bearer secret"})
    lignes = [l[6:] for l in r.text.splitlines() if l.startswith("data: ")]
    assert lignes[-1] == "[DONE]"
    morceaux = [json.loads(l) for l in lignes[:-1]]
    delta = morceaux[0]["choices"][0]["delta"]
    assert delta["content"] == "Je note pour Paul Martin et Louis."
    assert delta["tool_calls"][0]["index"] == 0
    assert morceaux[1]["choices"][0]["finish_reason"] == "tool_calls"
    assert morceaux[-1]["usage"]["total_tokens"] == 15


def test_proxy_jeton_obligatoire(proxy):
    client, recus, _, _ = proxy
    assert client.post("/v1/chat/completions", json=DEMANDE).status_code == 401
    assert client.post("/v1/chat/completions", json=DEMANDE, headers={"Authorization": "Bearer faux"}).status_code == 401
    assert recus == []


def test_proxy_tous_en_echec(proxy):
    client, _, statuts, _ = proxy
    statuts.update({"cle-gemini": 500, "cle-mistral": 429})
    r = client.post("/v1/chat/completions", json=DEMANDE, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 502


def test_agenda_ical_garde_les_dates(anon):
    ics = "BEGIN:VEVENT\nSUMMARY:Dentiste Dr Lefevre\nDTSTART:20261009T100000\nEND:VEVENT"
    sortie = anon.anonymiser(ics)
    assert "DTSTART:20261009T100000" in sortie and "Lefevre" not in sortie
    assert anon.retablir(sortie) == ics


def test_proxy_conseil_via_mot_cle(proxy):
    client, recus, _, _ = proxy
    demande = {"model": "auto", "messages": [
        {"role": "user", "content": "!conseil Paul Martin me propose un job, j'accepte ?"}]}
    r = client.post("/v1/chat/completions", json=demande, headers={"Authorization": "Bearer secret"})
    assert r.status_code == 200, r.text
    assert "conseil[" in r.json()["model"]
    # Aucun des appels (avis + synthese) ne contient le vrai nom
    assert all("Paul" not in json.dumps(c, ensure_ascii=False) for _, c in recus)
    assert len({cle for cle, _ in recus}) >= 2


def test_metriques_sans_donnees_personnelles(proxy):
    client, _, _, _ = proxy
    client.post("/v1/chat/completions", json=DEMANDE, headers={"Authorization": "Bearer secret"})
    m = client.get("/metrics").text
    assert 'anonymiseur_requetes_total{route="gemini-flash",fournisseur="gemini",statut="ok"} 1' in m
    assert "Paul" not in m and "Louis" not in m


def test_pas_de_faux_noms_dans_un_prompt_anglais(anon):
    prompt = ("Show the result to the user only when asked. Read the file first.\n"
              "Subagents: avoid subagents for simple one-step work. Proactively check memory.\n"
              "Mark it blocked only when the same blocker has recurred for at least three turns.\n"
              "- Préfère des réponses courtes. Sa compagne s'appelle Emma.")
    sortie = anon.anonymiser(prompt)
    assert sortie.count("[PERSONNE_") == 1 and "Emma" not in sortie, sortie
    assert "Show the result" in sortie and "Subagents" in sortie


def test_forme_des_noms(anon):
    for nom in ["Jean-Pierre Dupont", "Mme Nguyen", "Charles de Gaulle", "Léo"]:
        assert nom not in anon.anonymiser(f"Demain je vois {nom} au marché."), nom


def test_prompt_systeme_strict_conversation_large(anon):
    ligne = "Several: most specific. None: read none."
    assert anon.anonymiser(ligne, strict=True) == ligne
    for phrase in ["Salut Sophie !", "Dis à Sophie que je suis en retard.", "Salut Sophie, tu viens ?"]:
        assert "Sophie" not in anon.anonymiser(phrase, utilisateur=True), phrase
    assert "Emma" not in anon.anonymiser("Sa compagne s'appelle Emma.", strict=True)


def test_resultats_d_outils_gardent_les_noms_techniques(anon):
    alerte = '{"labels": {"alertname": "ZfsPoolDegraded", "instance": "Jellyfin"}, "state": "firing"}'
    assert anon.anonymiser(alerte) == alerte


def test_texte_encode_en_json(anon):
    sortie = anon.anonymiser('{"message": "Dis \\u00e0 In\\u00e8s que c\'est OK \\ud83d\\ude00"}', utilisateur=True)
    assert "Inès" not in sortie and "In\\u00e8s" not in sortie and "à" in sortie and "😀" in sortie


def test_etiquettes_techniques(anon):
    ligne = "Runtime: agent=majordome | os=Linux 6.18 | host=vm"
    assert anon.anonymiser(ligne, utilisateur=True) == ligne


def test_domaine_avec_sous_domaine(anon):
    texte = "Portail : auth.louisrousseaux.fr et https://cloud.louisrousseaux.fr/x"
    sortie = anon.anonymiser(texte)
    assert "louisrousseaux" not in sortie, sortie
    assert anon.retablir(sortie) == texte


def test_horodatage_ajoute_au_prompt_systeme():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from app.proxy import ajouter_horodatage
    t = datetime(2026, 10, 8, 15, 59, tzinfo=ZoneInfo("Europe/Paris"))
    m = ajouter_horodatage([{"role": "system", "content": "Regles"}, {"role": "user", "content": "x"}], t)
    assert len(m) == 2 and m[0]["content"].endswith("jeudi 8 octobre 2026, 15:59.")
    m = ajouter_horodatage([{"role": "user", "content": "x"}], t)
    assert m[0]["role"] == "system" and "jeudi 8 octobre 2026" in m[0]["content"]


def test_chemins_et_domaines_reviennent_a_l_identique(anon):
    texte = "cd /home/tobilianok/immich-stack ; voir https://nextcloud.famillerousseaux.fr et auth.louisrousseaux.fr"
    envoye = anon.anonymiser(texte)
    for secret in ["tobilianok", "famillerousseaux", "louisrousseaux"]:
        assert secret not in envoye
    assert anon.retablir(envoye) == texte


def test_ancien_coffre_avec_domaines_groupes(tmp_path, nlp):
    """Coffre cree avec l'ancien comportement (formes groupees) : chacune retrouve sa valeur."""
    coffre = Coffre(str(tmp_path / "c.db"))
    coffre.enregistrer_groupe("DOMAINE", ["louisrousseaux.fr", "famillerousseaux.fr"], "louisrousseaux.fr")
    coffre.enregistrer_groupe("PERSONNE", ["Louis", "tobilianok"], "Louis", "UTILISATEUR")
    a = Anonymiseur(coffre, DICO, {"PERSONNE": True}, nlp)
    texte = "nextcloud.famillerousseaux.fr, louisrousseaux.fr et /home/tobilianok"
    assert a.retablir(a.anonymiser(texte)) == texte
