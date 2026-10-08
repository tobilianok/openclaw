"""Tests du routeur multi-fournisseurs et du mode conseil."""
import asyncio
import json
import time

import httpx
import pytest

from app.routeur import Routeur, aplatir, estimer_tokens


def config(**limites):
    return {
        "fournisseurs": {
            "a": {"url": "https://a.test/v1", "cle_env": "CLE_A"},
            "b": {"url": "https://b.test/v1", "cle_env": "CLE_B"},
            "c": {"url": "https://c.test/v1", "cle_env": "CLE_C"},
            "absent": {"url": "https://x.test/v1", "cle_env": "CLE_ABSENTE"},
        },
        "routes": {
            "a1": {"fournisseur": "a", "modele": "ma", **limites.get("a1", {})},
            "b1": {"fournisseur": "b", "modele": "mb", **limites.get("b1", {})},
            "c1": {"fournisseur": "c", "modele": "mc", **limites.get("c1", {})},
            "x1": {"fournisseur": "absent", "modele": "mx"},
        },
        "profils": {"auto": ["x1", "a1", "b1", "c1"], "reflexion": ["c1", "b1"]},
    }


class Faux:
    """Faux fournisseurs : statut et reponse parametrables par hote."""

    def __init__(self):
        self.appels = []
        self.statuts = {}

    def __call__(self, req: httpx.Request):
        hote = req.url.host.split(".")[0]
        corps = json.loads(req.content)
        self.appels.append((hote, corps))
        statut, texte = self.statuts.get(hote, (200, ""))
        if statut != 200:
            return httpx.Response(statut, text=texte)
        systeme = corps["messages"][0].get("content", "")
        contenu = f"SYNTHESE par {hote}" if "arbitre" in systeme else f"avis de {hote}"
        return httpx.Response(200, json={"choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": contenu}}],
                                         "usage": {"prompt_tokens": 12}})


@pytest.fixture
def faux(monkeypatch):
    for v in ("CLE_A", "CLE_B", "CLE_C"):
        monkeypatch.setenv(v, "k")
    monkeypatch.delenv("CLE_ABSENTE", raising=False)
    return Faux()


def routeur(faux, tmp_path=None, **limites):
    return Routeur(config(**limites), httpx.AsyncClient(transport=httpx.MockTransport(faux)),
                   str(tmp_path / "q.json") if tmp_path else None)


CORPS = {"messages": [{"role": "user", "content": "Bonjour"}]}


def lancer(coro):
    return asyncio.run(coro)


def test_route_sans_cle_ignoree(faux):
    r = routeur(faux)
    assert "x1" not in r.routes and r.profils["auto"] == ["a1", "b1", "c1"]


def test_requete_trop_grosse_evite_les_petits_contextes(faux):
    r = routeur(faux, a1={"contexte": 10})
    gros = {"messages": [{"role": "user", "content": "x" * 300}]}
    assert estimer_tokens(gros) > 10
    _, route = lancer(r.appeler("auto", gros))
    assert route.nom == "b1" and [h for h, _ in faux.appels] == ["b"]


def test_quota_jour_compte_localement(faux, tmp_path):
    r = routeur(faux, tmp_path, a1={"rpd": 2})
    for _ in range(3):
        lancer(r.appeler("auto", CORPS))
    assert [h for h, _ in faux.appels] == ["a", "a", "b"]
    # Les compteurs survivent a un redemarrage
    r2 = routeur(faux, tmp_path, a1={"rpd": 2})
    assert r2.routes["a1"].jour_n == 2


def test_quota_jour_annonce_par_le_fournisseur(faux):
    faux.statuts["a"] = (429, '{"error": "Requests per day limit exceeded"}')
    r = routeur(faux)
    _, route = lancer(r.appeler("auto", CORPS))
    assert route.nom == "b1"
    a1 = r.routes["a1"]
    assert a1.raison_pause == "quota_jour" and a1.pause_jusqua - time.time() > 0
    assert a1.pause_jusqua % 86400 == 0  # minuit UTC
    assert 'anonymiseur_route_en_pause{route="a1",fournisseur="a"} 1' in r.metriques()


def test_limite_minute_respecte_retry_after(faux):
    faux.statuts["a"] = (429, "rate limit")
    r = routeur(faux)
    lancer(r.appeler("auto", CORPS))
    assert r.routes["a1"].raison_pause == "limite"


def test_cle_refusee_pause_une_heure(faux):
    faux.statuts["a"] = (401, "invalid key")
    r = routeur(faux)
    lancer(r.appeler("auto", CORPS))
    assert r.routes["a1"].pause_jusqua - time.time() > 3500


def test_contexte_trop_long_ne_met_pas_en_pause(faux):
    faux.statuts["a"] = (400, "This model's maximum context length is 8192 tokens")
    r = routeur(faux)
    lancer(r.appeler("auto", CORPS))
    assert r.routes["a1"].pause_jusqua == 0


def test_conseil_trois_fournisseurs_differents_et_synthese(faux):
    r = routeur(faux)
    premiere, route1 = lancer(r.appeler("auto", CORPS))
    rep, nom = lancer(r.conseil(CORPS, premiere, route1, ["reflexion", "auto"], "reflexion"))
    hotes = [h for h, _ in faux.appels]
    assert hotes[:3] == ["a", "c", "b"]  # a: 1er avis, c et b: les deux autres
    assert rep["choices"][0]["message"]["content"].startswith("SYNTHESE")
    synthese = faux.appels[-1][1]["messages"][1]["content"]
    assert "avis de a" in synthese and "avis de b" in synthese and "avis de c" in synthese
    assert nom.startswith("conseil[a1+c1+b1]")


def test_conseil_tolere_un_membre_en_panne(faux):
    r = routeur(faux)
    premiere, route1 = lancer(r.appeler("auto", CORPS))
    faux.statuts["c"] = (500, "panne")
    rep, nom = lancer(r.conseil(CORPS, premiere, route1, ["reflexion", "auto"], "reflexion"))
    assert rep["choices"][0]["message"]["content"].startswith("SYNTHESE")
    # c1 en panne : remplace par b1 (suivant du profil reflexion), conseil a 2 avis + synthese par b1
    assert nom.startswith("conseil[a1+b1]")


def test_aplatir_les_appels_d_outils():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": [{"type": "text", "text": "Q"}]},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "1", "type": "function", "function": {"name": "agenda", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "RDV 10h"},
    ]
    a = aplatir(msgs)
    assert [m["role"] for m in a] == ["system", "user", "assistant", "user"]
    assert "agenda" in a[2]["content"] and "RDV 10h" in a[3]["content"]


def test_conseil_meme_fournisseur_si_pas_d_autre(faux, monkeypatch):
    monkeypatch.delenv("CLE_B")
    monkeypatch.delenv("CLE_C")
    cfg = config()
    cfg["routes"]["a2"] = {"fournisseur": "a", "modele": "ma2"}
    cfg["profils"] = {"auto": ["a1", "a2"], "reflexion": ["a2", "a1"]}
    r = Routeur(cfg, httpx.AsyncClient(transport=httpx.MockTransport(faux)))
    premiere, route1 = lancer(r.appeler("auto", CORPS))
    rep, nom = lancer(r.conseil(CORPS, premiere, route1, ["reflexion", "auto"], "reflexion"))
    assert nom.startswith("conseil[a1+a2]") and rep["choices"][0]["message"]["content"].startswith("SYNTHESE")


def test_signatures_gemini_reinjectees_et_retirees_ailleurs(faux):
    cfg = config()
    cfg["fournisseurs"]["a"]["url"] = "https://a.test/v1"
    r = Routeur(cfg, httpx.AsyncClient(transport=httpx.MockTransport(faux)))
    r.routes["a1"].fournisseur = "gemini"
    r.signatures["c1"] = {"google": {"thought_signature": "SIG"}}
    corps = {"messages": [
        {"role": "user", "content": "Q"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "agenda", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "agenda", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"}]}
    vers_gemini = r._adapter(corps, r.routes["a1"])
    tcs = vers_gemini["messages"][1]["tool_calls"]
    assert tcs[0]["extra_content"]["google"]["thought_signature"] == "SIG"
    assert tcs[1]["extra_content"]["google"]["thought_signature"] == "skip_thought_signature_validator"
    assert "extra_content" not in corps["messages"][1]["tool_calls"][0]  # original intact
    corps["messages"][1]["tool_calls"][0]["extra_content"] = {"google": {}}
    vers_autre = r._adapter(corps, r.routes["b1"])
    assert all("extra_content" not in tc for tc in vers_autre["messages"][1]["tool_calls"])


def test_signatures_memorisees(faux):
    r = routeur(faux)
    r._memoriser_signatures({"choices": [{"message": {"tool_calls": [
        {"id": "x9", "extra_content": {"google": {"thought_signature": "S"}}}]}}]})
    assert r.signatures["x9"]["google"]["thought_signature"] == "S"
