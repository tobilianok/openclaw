"""Proxy OpenAI-compatible anonymisant, entre OpenClaw et les IA cloud.

    OpenClaw --(texte en clair)--> anonymiseur --(pseudonymes)--> Gemini / Mistral / Groq...
    OpenClaw <--(texte en clair)-- anonymiseur <--(pseudonymes)--

Seul ce service detient les cles des IA cloud : OpenClaw n'en a aucune, il ne
peut donc rien envoyer dehors sans passer par ici.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .coffre import Coffre
from .detection import Anonymiseur

log = logging.getLogger("anonymiseur")

# Champs transmis aux fournisseurs (le reste est retire : chaque API gratuite
# a ses propres refus de parametres).
CHAMPS_TRANSMIS = {"messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens", "stop",
                   "response_format"}


def charger_yaml(chemin: str) -> dict:
    if not chemin or not os.path.exists(chemin):
        return {}
    with open(chemin, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Fournisseurs:
    """Fournisseurs gratuits, essayes dans l'ordre, avec pause apres un refus."""

    def __init__(self, config: dict, client: httpx.AsyncClient):
        self.client = client
        self.liste = {}
        for nom, f in (config.get("fournisseurs") or {}).items():
            cle = os.environ.get(f.get("cle_env", ""), "")
            if cle:
                self.liste[nom] = {**f, "cle": cle}
            else:
                log.warning("fournisseur %s ignore : %s vide", nom, f.get("cle_env"))
        self.ordre = [n for n in config.get("ordre_auto", list(self.liste)) if n in self.liste]
        self.pause_jusqua: dict[str, float] = {}
        self.pause_defaut = int(config.get("pause_apres_refus_s", 60))

    def candidats(self, modele: str) -> list[str]:
        if modele in self.liste:
            return [modele]
        maintenant = time.time()
        dispo = [n for n in self.ordre if self.pause_jusqua.get(n, 0) <= maintenant]
        # Si tout le monde est en pause, on retente quand meme dans l'ordre.
        return dispo or list(self.ordre)

    async def appeler(self, modele: str, corps: dict) -> tuple[dict, str]:
        erreurs = []
        for nom in self.candidats(modele):
            f = self.liste[nom]
            envoi = {**corps, "model": f["modele"], "stream": False}
            try:
                r = await self.client.post(
                    f["url"].rstrip("/") + "/chat/completions",
                    json=envoi,
                    headers={"Authorization": f"Bearer {f['cle']}"},
                    timeout=f.get("delai_s", 90),
                )
            except httpx.HTTPError as e:
                erreurs.append(f"{nom}: {type(e).__name__}")
                self.pause_jusqua[nom] = time.time() + self.pause_defaut
                continue
            if r.status_code == 200:
                return r.json(), nom
            pause = self.pause_defaut
            if r.status_code == 429:
                try:
                    pause = max(pause, int(float(r.headers.get("retry-after", 0))))
                except ValueError:
                    pass
            self.pause_jusqua[nom] = time.time() + pause
            erreurs.append(f"{nom}: HTTP {r.status_code} {r.text[:200]}")
            log.warning("fournisseur %s en echec (HTTP %s), suivant", nom, r.status_code)
        raise HTTPException(status_code=502, detail={"error": {"message": "; ".join(erreurs) or "aucun fournisseur"}})


def creer_app(config: dict | None = None, nlp=None, client: httpx.AsyncClient | None = None) -> FastAPI:
    config = config if config is not None else charger_yaml(os.environ.get("ANONYMISEUR_CONFIG", "config.yaml"))
    dictionnaire = charger_yaml(os.environ.get("ANONYMISEUR_DICTIONNAIRE", config.get("dictionnaire", "")))
    jeton_acces = os.environ.get("ANONYMISEUR_TOKEN", "")
    if not jeton_acces:
        raise RuntimeError("ANONYMISEUR_TOKEN est obligatoire")

    if nlp is None and any((config.get("ner") or {}).values()):
        import spacy
        nlp = spacy.load(config.get("modele_spacy", "fr_core_news_lg"), disable=["parser", "lemmatizer"])

    coffre = Coffre(config.get("coffre", "/data/coffre.db"))
    anon = Anonymiseur(coffre, dictionnaire, config.get("ner", {}), nlp)
    fournisseurs = Fournisseurs(config, client or httpx.AsyncClient())
    journal = config.get("journal_envois")

    app = FastAPI(title="Anonymiseur du majordome")
    app.state.anon = anon
    app.state.fournisseurs = fournisseurs

    def verifier(request: Request):
        if request.headers.get("authorization") != f"Bearer {jeton_acces}":
            raise HTTPException(status_code=401, detail="jeton invalide")

    @app.get("/sante")
    async def sante():
        return {"ok": True, "fournisseurs": list(fournisseurs.liste), "pseudonymes": coffre.taille()}

    @app.get("/v1/models")
    async def modeles(request: Request):
        verifier(request)
        noms = ["auto", *fournisseurs.liste]
        return {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "anonymiseur"} for n in noms]}

    @app.post("/v1/anonymiser")
    async def tester(request: Request):
        """Pour verifier a la main ce qui partirait vers le cloud."""
        verifier(request)
        texte = (await request.json()).get("texte", "")
        return {"envoye": anon.anonymiser(texte)}

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        verifier(request)
        demande = await request.json()
        flux = bool(demande.get("stream"))
        corps = {k: v for k, v in demande.items() if k in CHAMPS_TRANSMIS}
        if "max_tokens" not in corps and demande.get("max_completion_tokens"):
            corps["max_tokens"] = demande["max_completion_tokens"]
        corps["messages"] = [anonymiser_message(anon, m) for m in demande.get("messages", [])]
        if journal:
            with open(journal, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%F %T"), "messages": corps["messages"]},
                                   ensure_ascii=False) + "\n")

        reponse, nom = await fournisseurs.appeler(demande.get("model", "auto"), corps)
        for choix in reponse.get("choices", []):
            retablir_message(anon, choix.get("message") or {})
        reponse["model"] = f"{demande.get('model', 'auto')} ({nom})"
        log.info("reponse via %s", nom)
        if not flux:
            return JSONResponse(reponse)
        inclure_usage = bool((demande.get("stream_options") or {}).get("include_usage"))
        return StreamingResponse(flux_sse(reponse, inclure_usage), media_type="text/event-stream")

    return app


def anonymiser_message(anon: Anonymiseur, message: dict) -> dict:
    m = dict(message)
    contenu = m.get("content")
    if isinstance(contenu, str):
        m["content"] = anon.anonymiser(contenu)
    elif isinstance(contenu, list):
        m["content"] = [
            {**p, "text": anon.anonymiser(p["text"])} if p.get("type") == "text" and "text" in p else p
            for p in contenu
        ]
    if m.get("tool_calls"):
        m["tool_calls"] = [
            {**tc, "function": {**tc["function"], "arguments": anon.anonymiser_json(tc["function"].get("arguments", ""))}}
            for tc in m["tool_calls"]
        ]
    # Champs propres a certains clients, inutiles aux IA cloud
    for champ in ("reasoning_content", "reasoning", "name"):
        if champ in m and m.get("role") != "tool":
            m.pop(champ)
    return m


def retablir_message(anon: Anonymiseur, message: dict) -> None:
    if isinstance(message.get("content"), str):
        message["content"] = anon.retablir(message["content"])
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        if "arguments" in fn:
            fn["arguments"] = anon.retablir_json(fn["arguments"])
    for champ in ("reasoning_content", "reasoning"):
        if isinstance(message.get(champ), str):
            message[champ] = anon.retablir(message[champ])


def flux_sse(reponse: dict, inclure_usage: bool):
    """Les IA repondent en une fois (les jetons doivent etre retablis en
    entier) ; on renvoie ensuite la reponse au format "streaming" attendu."""
    ident = reponse.get("id") or f"chatcmpl-{uuid.uuid4().hex}"
    base = {"id": ident, "object": "chat.completion.chunk", "created": reponse.get("created", int(time.time())),
            "model": reponse.get("model", "auto")}
    for choix in reponse.get("choices", []):
        msg = choix.get("message") or {}
        delta = {"role": "assistant"}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = [{**tc, "index": i} for i, tc in enumerate(msg["tool_calls"])]
        yield f"data: {json.dumps({**base, 'choices': [{'index': choix.get('index', 0), 'delta': delta, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
        fin = {"index": choix.get("index", 0), "delta": {}, "finish_reason": choix.get("finish_reason") or "stop"}
        yield f"data: {json.dumps({**base, 'choices': [fin]})}\n\n"
    if inclure_usage and reponse.get("usage"):
        yield f"data: {json.dumps({**base, 'choices': [], 'usage': reponse['usage']})}\n\n"
    yield "data: [DONE]\n\n"


def app_depuis_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return creer_app()
