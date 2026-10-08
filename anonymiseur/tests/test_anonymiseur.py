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
         "formes": ["Louis Rousseaux", "Louis", "Rousseaux", "tobilianok"]},
        {"affichage": "Marie", "formes": ["Marie Rousseaux", "Marie"]},
    ],
    "termes": [{"categorie": "DOMAINE", "formes": ["louisrousseaux.fr"]}],
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
        jeton = re.search(r"\[PERSONNE_\d+\]", corps["messages"][-1]["content"]).group()
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
            "gemini": {"url": "https://g.test/v1", "cle_env": "GEMINI_API_KEY", "modele": "gm"},
            "mistral": {"url": "https://m.test/v1", "cle_env": "MISTRAL_API_KEY", "modele": "mm"},
            "groq": {"url": "https://q.test/v1", "cle_env": "GROQ_API_KEY", "modele": "qm"},
        },
        "ordre_auto": ["gemini", "mistral", "groq"],
        "ner": {"PERSONNE": True},
        "coffre": str(tmp_path / "c.db"),
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
    assert r.status_code == 200 and "(mistral)" in r.json()["model"]
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
