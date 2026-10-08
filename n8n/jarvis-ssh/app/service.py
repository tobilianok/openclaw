"""jarvis-ssh : SSH de Jarvis vers l'infra, avec validation humaine obligatoire.

API interne (appelee par n8n, jeton Bearer) :
  POST /lecture  {machine, commande}               -> cle LECTURE, immediat
  POST /demande  {machine, commandes, explication,  -> rien n'est execute :
                  risques, retour_arriere}             message dans Talk + lien
Pages de decision (via NPM + Authentik, jeton unique dans l'URL) :
  GET  /d/{id}?j=...            -> details de la demande, boutons
  POST /d/{id}/valider|refuser  -> execution (cle ACTION) ou abandon

Jarvis ne voit jamais le lien de validation : il ne peut pas s'auto-valider.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import shlex
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("jarvis-ssh")

CARACTERES_LECTURE = re.compile(r"^[A-Za-z0-9 ._/@:=,+-]{1,300}$")
MAX_COMMANDES = 10
MAX_SORTIE = 12000


def charger_config(chemin: str) -> dict:
    with open(chemin, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Executeur:
    """Lance les commandes SSH (binaire ssh du systeme)."""

    def __init__(self, dossier: Path, ssh: str = "ssh", port: int = 22, utilisateur: str = "jarvis"):
        self.dossier, self.ssh, self.port, self.utilisateur = dossier, ssh, port, utilisateur
        self.cles = dossier / "cles"
        self.known_hosts = dossier / "known_hosts"

    def preparer_cles(self) -> None:
        self.cles.mkdir(parents=True, exist_ok=True)
        for nom in ("lecture", "action"):
            cle = self.cles / nom
            if not cle.exists():
                os.system(f"ssh-keygen -q -t ed25519 -N '' -C jarvis-{nom} -f {shlex.quote(str(cle))}")
                log.info("cle %s generee", nom)

    async def lancer(self, cle: str, hote: str, commande: str, delai: float = 60) -> tuple[int, str]:
        argv = [
            self.ssh, "-i", str(self.cles / cle), "-p", str(self.port),
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=8",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "LogLevel=ERROR",
            f"{self.utilisateur}@{hote}", "--", commande,
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            sortie, _ = await asyncio.wait_for(proc.communicate(), delai)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, f"(delai de {delai:.0f} s depasse)"
        texte = sortie.decode("utf-8", "replace")
        if len(texte) > MAX_SORTIE:
            texte = texte[:MAX_SORTIE] + f"\n... (sortie tronquee, {len(texte)} caracteres)"
        return proc.returncode, texte


class Talk:
    """Poste dans le salon Talk en tant que bot (signature HMAC)."""

    def __init__(self, url: str, secret: str, salon: str, client: httpx.AsyncClient):
        self.url, self.secret, self.salon, self.client = url.rstrip("/"), secret, salon, client

    async def poster(self, message: str) -> None:
        if not (self.url and self.secret and self.salon):
            log.warning("Talk non configure, message non envoye")
            return
        message = message[:31000]
        aleatoire = secrets.token_hex(32)
        signature = hmac.new(self.secret.encode(), (aleatoire + message).encode(), hashlib.sha256).hexdigest()
        try:
            r = await self.client.post(
                f"{self.url}/ocs/v2.php/apps/spreed/api/v1/bot/{self.salon}/message",
                json={"message": message},
                headers={"OCS-APIRequest": "true", "Accept": "application/json",
                         "X-Nextcloud-Talk-Bot-Random": aleatoire,
                         "X-Nextcloud-Talk-Bot-Signature": signature},
                timeout=20)
            if r.status_code >= 300:
                log.error("Talk a refuse le message : HTTP %s %s", r.status_code, r.text[:200])
        except httpx.HTTPError as e:
            log.error("Talk injoignable : %s", e)


class Demandes:
    def __init__(self, chemin: str):
        self.db = sqlite3.connect(chemin, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS demandes (
            id TEXT PRIMARY KEY, jeton TEXT NOT NULL, machine TEXT NOT NULL, hote TEXT NOT NULL,
            commandes TEXT NOT NULL, explication TEXT, risques TEXT, retour_arriere TEXT,
            statut TEXT NOT NULL, cree REAL NOT NULL, expire REAL NOT NULL,
            decide REAL, resultat TEXT)""")
        self.db.commit()

    def creer(self, **champs) -> dict:
        ident = secrets.token_hex(3)
        while self.lire(ident):
            ident = secrets.token_hex(3)
        champs.update(id=ident, jeton=secrets.token_urlsafe(24), statut="en_attente")
        champs["commandes"] = json.dumps(champs["commandes"], ensure_ascii=False)
        cles = ", ".join(champs)
        self.db.execute(f"INSERT INTO demandes ({cles}) VALUES ({', '.join('?' * len(champs))})",
                        list(champs.values()))
        self.db.commit()
        return self.lire(ident)

    def lire(self, ident: str) -> dict | None:
        ligne = self.db.execute("SELECT * FROM demandes WHERE id = ?", (ident,)).fetchone()
        if not ligne:
            return None
        d = dict(ligne)
        d["commandes"] = json.loads(d["commandes"])
        if d["statut"] == "en_attente" and d["expire"] < time.time():
            d["statut"] = "expiree"
        return d

    def passer(self, ident: str, de: str, vers: str, **champs) -> bool:
        """Change le statut seulement s'il vaut encore `de` (pas de double execution)."""
        sets = ", ".join(["statut = ?"] + [f"{k} = ?" for k in champs])
        cur = self.db.execute(f"UPDATE demandes SET {sets} WHERE id = ? AND statut = ? AND expire >= ?",
                              [vers, *champs.values(), ident, de, time.time() if de == "en_attente" else 0])
        self.db.commit()
        return cur.rowcount == 1


def creer_app(config: dict | None = None, executeur: Executeur | None = None,
              client: httpx.AsyncClient | None = None) -> FastAPI:
    env = os.environ
    config = config if config is not None else charger_config(env.get("JARVIS_SSH_CONFIG", "config.yaml"))
    dossier = Path(env.get("JARVIS_SSH_DATA", "/data"))
    dossier.mkdir(parents=True, exist_ok=True)
    jeton_api = env.get("JARVIS_SSH_TOKEN", "")
    if not jeton_api:
        raise RuntimeError("JARVIS_SSH_TOKEN est obligatoire")
    url_publique = env.get("JARVIS_URL_PUBLIQUE", "").rstrip("/")
    client = client or httpx.AsyncClient()
    talk = Talk(env.get("NEXTCLOUD_URL", ""), env.get("NEXTCLOUD_TALK_BOT_SECRET", ""),
                env.get("TALK_ROOM_MAJORDOME", ""), client)
    openclaw_url = env.get("OPENCLAW_URL", "").rstrip("/")
    openclaw_jeton = env.get("OPENCLAW_HOOKS_TOKEN", "")
    executeur = executeur or Executeur(dossier)
    executeur.preparer_cles()
    demandes = Demandes(str(dossier / "demandes.db"))
    machines: dict = config.get("machines") or {}
    protections = [(set(p["machines"]), re.compile(p["motif"], re.IGNORECASE), p["raison"])
                   for p in config.get("protections") or []]
    expiration = 60 * int(config.get("expiration_minutes", 30))
    fuseau = ZoneInfo(config.get("fuseau", "Europe/Paris"))  # heures affichees a Louis

    app = FastAPI(title="jarvis-ssh")
    app.state.demandes = demandes
    taches: set[asyncio.Task] = set()  # garde une reference : une tache orpheline peut etre detruite

    def verifier(request: Request) -> None:
        recu = request.headers.get("authorization", "")
        if not hmac.compare_digest(recu, f"Bearer {jeton_api}"):
            raise HTTPException(401, "jeton invalide")

    def machine_ou_erreur(nom: str) -> dict:
        m = machines.get((nom or "").strip().lower())
        if not m:
            raise HTTPException(400, f"machine inconnue : {nom!r}. Machines : {', '.join(machines)}")
        return m

    async def prevenir_jarvis(message: str) -> None:
        if not (openclaw_url and openclaw_jeton):
            return
        try:
            await client.post(f"{openclaw_url}/hooks/agent", timeout=20,
                              headers={"Authorization": f"Bearer {openclaw_jeton}"},
                              json={"message": message, "name": "jarvis-ssh", "agentId": "majordome",
                                    "channel": "nextcloud-talk", "to": f"room:{talk.salon}"})
        except httpx.HTTPError as e:
            log.error("OpenClaw injoignable : %s", e)

    @app.get("/sante")
    async def sante():
        return {"ok": True, "machines": list(machines)}

    # --- Lecture : immediate, cle bridee cote serveur ---------------------
    @app.post("/lecture")
    async def lecture(request: Request):
        verifier(request)
        corps = await request.json()
        m = machine_ou_erreur(corps.get("machine", ""))
        commande = " ".join(str(corps.get("commande", "")).split())
        if not CARACTERES_LECTURE.match(commande):
            return {"code": 126, "sortie": "REFUSE : commande vide, trop longue ou avec des caracteres interdits "
                                           "(pas de ; | & $ ` < > guillemets). Tape 'aide' pour la liste."}
        code, sortie = await executeur.lancer("lecture", m["hote"], commande, delai=40)
        log.info("lecture %s : %s -> %s", corps.get("machine"), commande, code)
        return {"machine": corps.get("machine"), "commande": commande, "code": code, "sortie": sortie}

    # --- Demande d'action : rien n'est execute sans Louis -------------------
    @app.post("/demande")
    async def demande(request: Request):
        verifier(request)
        corps = await request.json()
        nom = (corps.get("machine") or "").strip().lower()
        m = machine_ou_erreur(nom)
        brut = corps.get("commandes") or ""
        lignes = brut if isinstance(brut, list) else str(brut).split("\n")
        commandes = [c.strip() for c in lignes if c.strip()]
        explication = str(corps.get("explication") or "").strip()
        risques = str(corps.get("risques") or "").strip()
        retour = str(corps.get("retour_arriere") or "").strip()
        manque = [n for n, v in (("commandes", commandes), ("explication", explication),
                                 ("risques", risques), ("retour_arriere", retour)) if not v]
        if manque:
            raise HTTPException(400, f"champs obligatoires manquants : {', '.join(manque)}")
        if len(commandes) > MAX_COMMANDES or any(len(c) > 500 for c in commandes):
            raise HTTPException(400, f"au plus {MAX_COMMANDES} commandes de 500 caracteres")
        for c in commandes:
            for concernees, motif, raison in protections:
                if nom in concernees and motif.search(c):
                    log.warning("demande refusee d'office sur %s : %s", nom, c)
                    return {"statut": "refusee_d_office", "commande": c, "raison": raison,
                            "consigne": "N'insiste pas et ne cherche pas de contournement. Explique a Louis "
                                        "pourquoi c'est bloque et comment il peut le faire lui-meme s'il le veut."}
        d = demandes.creer(machine=nom, hote=m["hote"], commandes=commandes, explication=explication,
                           risques=risques, retour_arriere=retour, cree=time.time(),
                           expire=time.time() + expiration)
        lien = f"{url_publique}/d/{d['id']}?j={d['jeton']}"
        bloc = "\n".join(commandes)
        heure = datetime.fromtimestamp(d["expire"], fuseau).strftime("%H:%M")
        await talk.poster(
            f"🔐 **Jarvis demande ton accord** — action `#{d['id']}` sur **{nom}** ({m['hote']})\n\n"
            f"**Pourquoi** : {explication}\n\n"
            f"**Commandes** (exécutées telles quelles, en root) :\n```\n{bloc}\n```\n"
            f"**Risques** : {risques}\n\n**Retour arrière** : {retour}\n\n"
            f"👉 Examiner et décider : {lien}\n_(expire à {heure} ; sans réponse, rien ne sera fait)_")
        log.info("demande #%s creee sur %s (%d commandes)", d["id"], nom, len(commandes))
        return {"statut": "en_attente_de_validation", "id": d["id"], "expire": heure,
                "consigne": "La demande est affichee a Louis dans Talk avec un lien de validation. Tu n'as rien "
                            "d'autre a faire : tu seras prevenu du resultat. Ne la represente pas."}

    # --- Pages de decision (Louis) ------------------------------------------
    def demande_ou_404(ident: str, jeton: str) -> dict:
        d = demandes.lire(ident)
        if not d or not hmac.compare_digest(d["jeton"], jeton or ""):
            raise HTTPException(404, "demande introuvable")
        return d

    @app.get("/d/{ident}", response_class=HTMLResponse)
    async def page(ident: str, j: str = ""):
        return page_html(demande_ou_404(ident, j))

    @app.post("/d/{ident}/valider", response_class=HTMLResponse)
    async def valider(ident: str, j: str = Form("")):
        d = demande_ou_404(ident, j)
        if not demandes.passer(ident, "en_attente", "en_cours", decide=time.time()):
            return page_html(demandes.lire(ident), "Cette demande n'est plus en attente.")
        tache = asyncio.create_task(executer(d))
        taches.add(tache)
        tache.add_done_callback(taches.discard)
        return page_html(demandes.lire(ident), "Validée : exécution en cours, le résultat arrive dans Talk.")

    @app.post("/d/{ident}/refuser", response_class=HTMLResponse)
    async def refuser(ident: str, j: str = Form("")):
        demande_ou_404(ident, j)
        if not demandes.passer(ident, "en_attente", "refusee", decide=time.time()):
            return page_html(demandes.lire(ident), "Cette demande n'est plus en attente.")
        await talk.poster(f"❌ Action `#{ident}` **refusée** par Louis. Rien n'a été exécuté.")
        await prevenir_jarvis(f"Louis a REFUSE ta demande d'action #{ident}. Rien n'a ete execute. "
                              "Ne la represente pas ; demande-lui s'il veut une autre approche.")
        return page_html(demandes.lire(ident), "Refusée. Rien n'a été exécuté.")

    async def executer(d: dict) -> None:
        compte_rendu, ok = [], True
        for c in d["commandes"]:
            code, sortie = await executeur.lancer(
                "action", d["hote"], f"sudo -n bash -c {shlex.quote(c)}", delai=300)
            compte_rendu.append({"commande": c, "code": code, "sortie": sortie})
            if code != 0:
                ok = False
                break
        statut = "terminee" if ok else "echec"
        demandes.passer(d["id"], "en_cours", statut, resultat=json.dumps(compte_rendu, ensure_ascii=False))
        texte = "\n\n".join(f"$ {r['commande']}\n(code {r['code']})\n{r['sortie'][-1500:]}" for r in compte_rendu)
        non_lancees = len(d["commandes"]) - len(compte_rendu)
        suite = f"\n\n⚠️ {non_lancees} commande(s) suivante(s) non lancée(s) après l'échec." if non_lancees else ""
        await talk.poster(f"{'✅' if ok else '⚠️'} Action `#{d['id']}` sur **{d['machine']}** : "
                          f"{'terminée' if ok else 'échec'}\n```\n{texte[-6000:]}\n```{suite}")
        await prevenir_jarvis(
            f"Resultat de ton action #{d['id']} sur {d['machine']}, validee par Louis "
            f"({'succes' if ok else 'ECHEC'}) :\n{texte[-4000:]}{suite}\n\n"
            "Explique en 2-3 phrases a Louis ce qui s'est passe et s'il faut faire quelque chose.")
        log.info("demande #%s : %s", d["id"], statut)

    return app


def page_html(d: dict, annonce: str = "") -> HTMLResponse:
    e = html.escape
    couleurs = {"en_attente": "#b26b00", "en_cours": "#1565c0", "terminee": "#2e7d32",
                "echec": "#c62828", "refusee": "#555", "expiree": "#555"}
    libelles = {"en_attente": "En attente de ta décision", "en_cours": "Exécution en cours",
                "terminee": "Terminée", "echec": "Échec", "refusee": "Refusée", "expiree": "Expirée"}
    boutons = ""
    if d["statut"] == "en_attente":
        boutons = f"""
<form method="post" action="/d/{e(d['id'])}/valider"><input type="hidden" name="j" value="{e(d['jeton'])}">
<button class="ok">✅ Valider et exécuter</button></form>
<form method="post" action="/d/{e(d['id'])}/refuser"><input type="hidden" name="j" value="{e(d['jeton'])}">
<button class="non">❌ Refuser</button></form>"""
    resultat = ""
    if d.get("resultat"):
        resultat = "<h2>Résultat</h2>" + "".join(
            f"<pre>$ {e(r['commande'])}\n(code {r['code']})\n{e(r['sortie'])}</pre>"
            for r in json.loads(d["resultat"]))
    return HTMLResponse(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Jarvis – action #{e(d['id'])}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:760px;margin:0 auto;padding:16px;background:#111;color:#eee}}
pre{{background:#000;padding:12px;border-radius:8px;overflow-x:auto;white-space:pre-wrap}}
.statut{{display:inline-block;padding:4px 10px;border-radius:12px;background:{couleurs.get(d['statut'], '#555')}}}
button{{width:100%;padding:16px;margin:8px 0;font-size:18px;border:0;border-radius:10px;cursor:pointer}}
.ok{{background:#2e7d32;color:#fff}}.non{{background:#444;color:#fff}}.annonce{{padding:12px;background:#1e3a5f;border-radius:8px}}</style>
</head><body><h1>Action #{e(d['id'])} sur {e(d['machine'])}</h1>
<p><span class="statut">{libelles.get(d['statut'], d['statut'])}</span> — {e(d['hote'])}</p>
{f'<p class="annonce">{e(annonce)}</p>' if annonce else ''}
<h2>Pourquoi</h2><p>{e(d['explication'] or '')}</p>
<h2>Commandes exécutées telles quelles, en root</h2><pre>{e(chr(10).join(d['commandes']))}</pre>
<h2>Risques</h2><p>{e(d['risques'] or '')}</p><h2>Retour arrière</h2><p>{e(d['retour_arriere'] or '')}</p>
{boutons}{resultat}</body></html>""")


def app_depuis_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return creer_app()
