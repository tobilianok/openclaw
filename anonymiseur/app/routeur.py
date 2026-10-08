"""Routeur multi-fournisseurs : choisit, pour chaque requete, l'IA gratuite
capable de la traiter (taille, quotas) et bascule a la suivante en cas de refus.

Vocabulaire :
  - fournisseur : un compte (Gemini, Mistral...) = une URL + une cle ;
  - route       : un modele chez un fournisseur, avec ses limites gratuites ;
  - profil      : liste ordonnee de routes (auto, reflexion, rapide).
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("anonymiseur.routeur")

SECONDES_JOUR = 86400


def estimer_tokens(corps: dict) -> int:
    """Estimation prudente (francais + JSON : ~3 caracteres par token)."""
    return len(json.dumps(corps.get("messages", []), ensure_ascii=False)) // 3 + \
        len(json.dumps(corps.get("tools", []), ensure_ascii=False)) // 3


def minuit_utc_suivant(maintenant: float) -> float:
    return (int(maintenant) // SECONDES_JOUR + 1) * SECONDES_JOUR


@dataclass
class Route:
    nom: str
    fournisseur: str
    modele: str
    url: str
    cle: str
    contexte: int = 128_000      # tokens d'entree acceptes
    rpm: int | None = None       # requetes / minute
    tpm: int | None = None       # tokens / minute
    rpd: int | None = None       # requetes / jour (UTC)
    delai_s: float = 90
    # Etat
    minute: deque = field(default_factory=deque)       # (horodatage, tokens)
    jour: str = ""
    jour_n: int = 0
    pause_jusqua: float = 0.0
    raison_pause: str = ""

    def _purger(self, maintenant: float) -> None:
        while self.minute and self.minute[0][0] < maintenant - 60:
            self.minute.popleft()
        aujourdhui = time.strftime("%Y-%m-%d", time.gmtime(maintenant))
        if self.jour != aujourdhui:
            self.jour, self.jour_n = aujourdhui, 0

    def refus(self, tokens: int, maintenant: float) -> str | None:
        """Raison pour laquelle cette route ne peut pas prendre la requete."""
        self._purger(maintenant)
        if tokens > self.contexte:
            return "trop_gros"
        if self.pause_jusqua > maintenant:
            return f"pause:{self.raison_pause}"
        if self.rpd is not None and self.jour_n >= self.rpd:
            return "quota_jour"
        if self.rpm is not None and len(self.minute) >= self.rpm:
            return "quota_minute"
        if self.tpm is not None and sum(t for _, t in self.minute) + tokens > self.tpm:
            return "quota_tokens"
        return None

    def compter(self, tokens: int, maintenant: float) -> None:
        self._purger(maintenant)
        self.minute.append((maintenant, tokens))
        self.jour_n += 1

    def pause(self, secondes: float, raison: str, maintenant: float) -> None:
        self.pause_jusqua = max(self.pause_jusqua, maintenant + secondes)
        self.raison_pause = raison


class EchecRoutage(Exception):
    def __init__(self, erreurs: list[str]):
        super().__init__("; ".join(erreurs) or "aucune route disponible")
        self.erreurs = erreurs


class Routeur:
    def __init__(self, config: dict, client: httpx.AsyncClient, etat_chemin: str | None = None):
        self.client = client
        self.pause_defaut = int(config.get("pause_apres_refus_s", 60))
        self.etat_chemin = etat_chemin
        fournisseurs = config.get("fournisseurs") or {}
        self.routes: dict[str, Route] = {}
        for nom, r in (config.get("routes") or {}).items():
            f = fournisseurs.get(r["fournisseur"])
            if not f:
                raise ValueError(f"route {nom} : fournisseur {r['fournisseur']} inconnu")
            cle = os.environ.get(f.get("cle_env") or "", "") if f.get("cle_env") else ""
            if f.get("cle_env") and not cle:
                log.warning("route %s ignoree : %s vide", nom, f["cle_env"])
                continue
            self.routes[nom] = Route(
                nom=nom, fournisseur=r["fournisseur"], modele=r["modele"], url=f["url"].rstrip("/"), cle=cle,
                contexte=r.get("contexte", 128_000), rpm=r.get("rpm"), tpm=r.get("tpm"), rpd=r.get("rpd"),
                delai_s=r.get("delai_s", f.get("delai_s", 90)))
        self.profils = {p: [n for n in noms if n in self.routes]
                        for p, noms in (config.get("profils") or {}).items()}
        self.stats = defaultdict(lambda: {"ok": 0, "echec": 0, "latence": 0.0})
        # Signatures de reflexion de Gemini, par identifiant d'appel d'outil
        # (OpenClaw ne les renvoie pas, Gemini les exige au tour suivant).
        self.signatures: OrderedDict[str, dict] = OrderedDict()
        self._charger_etat()

    # --- Particularites des fournisseurs ----------------------------------
    def _adapter(self, corps: dict, r: Route) -> dict:
        """Corps de requete ajuste pour le fournisseur de la route."""
        if not any(m.get("tool_calls") for m in corps.get("messages", [])):
            return corps
        corps = copy.deepcopy(corps)
        for m in corps["messages"]:
            for tc in m.get("tool_calls") or []:
                if r.fournisseur == "gemini":
                    if "extra_content" not in tc:
                        tc["extra_content"] = self.signatures.get(tc.get("id")) or {
                            "google": {"thought_signature": "skip_thought_signature_validator"}}
                else:
                    tc.pop("extra_content", None)
        return corps

    def _memoriser_signatures(self, donnees: dict) -> None:
        for choix in donnees.get("choices") or []:
            for tc in (choix.get("message") or {}).get("tool_calls") or []:
                if tc.get("id") and tc.get("extra_content"):
                    self.signatures[tc["id"]] = tc["extra_content"]
                    self.signatures.move_to_end(tc["id"])
        while len(self.signatures) > 5000:
            self.signatures.popitem(last=False)

    # --- Persistance des compteurs du jour (survit a un redemarrage) -------
    def _charger_etat(self) -> None:
        if not self.etat_chemin or not os.path.exists(self.etat_chemin):
            return
        try:
            etat = json.load(open(self.etat_chemin))
        except (OSError, ValueError):
            return
        for nom, e in etat.items():
            if nom in self.routes:
                r = self.routes[nom]
                r.jour, r.jour_n = e.get("jour", ""), e.get("jour_n", 0)
                r.pause_jusqua, r.raison_pause = e.get("pause_jusqua", 0.0), e.get("raison_pause", "")

    def _sauver_etat(self) -> None:
        if not self.etat_chemin:
            return
        etat = {n: {"jour": r.jour, "jour_n": r.jour_n, "pause_jusqua": r.pause_jusqua,
                    "raison_pause": r.raison_pause} for n, r in self.routes.items()}
        tmp = self.etat_chemin + ".tmp"
        with open(tmp, "w") as f:
            json.dump(etat, f)
        os.replace(tmp, self.etat_chemin)

    # --- Selection ---------------------------------------------------------
    def candidates(self, profil: str, tokens: int, exclure_fournisseurs: set[str] = frozenset()) -> list[Route]:
        noms = self.profils.get(profil) or ([profil] if profil in self.routes else self.profils.get("auto", []))
        maintenant = time.time()
        possibles, en_attente = [], []
        for n in noms:
            r = self.routes[n]
            if r.fournisseur in exclure_fournisseurs:
                continue
            raison = r.refus(tokens, maintenant)
            if raison is None:
                possibles.append(r)
            elif raison in ("quota_minute", "quota_tokens") or raison.startswith("pause:limite"):
                en_attente.append(r)  # se liberera dans la minute
        # Si tout est sature a la minute, on tente quand meme : mieux vaut un
        # refus rapide qu'aucune reponse.
        return possibles or en_attente

    async def appeler(self, profil: str, corps: dict, exclure_fournisseurs: set[str] = frozenset(),
                      routes: list[Route] | None = None) -> tuple[dict, Route]:
        tokens = estimer_tokens(corps)
        erreurs = []
        if routes is None:
            routes = self.candidates(profil, tokens, exclure_fournisseurs)
        if not routes:
            raise EchecRoutage([f"aucune route du profil {profil} ne peut prendre ~{tokens} tokens maintenant"])
        for r in routes:
            maintenant = time.time()
            r.compter(tokens, maintenant)
            debut = time.monotonic()
            try:
                rep = await self.client.post(
                    f"{r.url}/chat/completions",
                    json={**self._adapter(corps, r), "model": r.modele, "stream": False},
                    headers={"Authorization": f"Bearer {r.cle}"} if r.cle else {},
                    timeout=r.delai_s,
                )
            except httpx.HTTPError as e:
                r.pause(self.pause_defaut, "reseau", time.time())
                erreurs.append(f"{r.nom}: {type(e).__name__}")
                self.stats[r.nom]["echec"] += 1
                continue
            finally:
                self._sauver_etat()
            duree = time.monotonic() - debut
            if rep.status_code == 200:
                donnees = rep.json()
                if (donnees.get("choices") or [{}])[0].get("message") is None:
                    erreurs.append(f"{r.nom}: reponse vide")
                    self.stats[r.nom]["echec"] += 1
                    continue
                reels = (donnees.get("usage") or {}).get("prompt_tokens")
                if reels and r.minute:
                    r.minute[-1] = (r.minute[-1][0], reels)
                self.stats[r.nom]["ok"] += 1
                self.stats[r.nom]["latence"] += duree
                self._memoriser_signatures(donnees)
                return donnees, r
            self._traiter_refus(r, rep)
            erreurs.append(f"{r.nom}: HTTP {rep.status_code} {rep.text[:160]}")
            self.stats[r.nom]["echec"] += 1
            self._sauver_etat()
        raise EchecRoutage(erreurs)

    def _traiter_refus(self, r: Route, rep: httpx.Response) -> None:
        maintenant = time.time()
        texte = rep.text.lower()
        if rep.status_code in (401, 403):
            r.pause(3600, "cle_refusee", maintenant)
            log.error("route %s : cle refusee (HTTP %s), pause 1 h", r.nom, rep.status_code)
        elif rep.status_code in (400, 413) and re.search(r"context|too long|too large|maximum|tokens", texte):
            # Requete trop grosse pour ce modele : on ne met pas la route en pause
            log.warning("route %s : requete trop grosse", r.nom)
        elif rep.status_code == 429 or rep.status_code == 402:
            if re.search(r"per.?day|daily|quota|rpd|exhausted|credits", texte):
                r.pause(minuit_utc_suivant(maintenant) - maintenant, "quota_jour", maintenant)
                log.warning("route %s : quota du jour atteint, pause jusqu'a minuit UTC", r.nom)
            else:
                try:
                    attente = float(rep.headers.get("retry-after", self.pause_defaut))
                except ValueError:
                    attente = self.pause_defaut
                r.pause(max(attente, 5), "limite", maintenant)
        else:
            r.pause(self.pause_defaut, f"http_{rep.status_code}", maintenant)

    # --- Mode conseil ------------------------------------------------------
    async def conseil(self, corps: dict, premiere: dict, route1: Route, membres: list[str],
                      profil_synthese: str) -> tuple[dict, str]:
        """Deux autres IA repondent a la meme question, une troisieme fait la
        synthese. `premiere` est la reponse deja obtenue (sans appel d'outil)."""
        a_plat = {"messages": aplatir(corps["messages"])}
        for k in ("temperature", "max_tokens"):
            if k in corps:
                a_plat[k] = corps[k]
        deja = {route1.fournisseur}
        routes_prises = {route1.nom}
        taches, choisies = [], []
        tokens = estimer_tokens(a_plat)
        for profil in membres:
            # D'abord un autre fournisseur ; a defaut, un autre modele. Si le
            # modele choisi echoue, les suivants de la liste prennent le relais.
            options = self.candidates(profil, tokens, deja) or [
                r for r in self.candidates(profil, tokens) if r.nom not in routes_prises]
            options = [r for r in options if r.nom not in routes_prises]
            if not options:
                continue
            r = options[0]
            deja.add(r.fournisseur)
            routes_prises.add(r.nom)
            choisies.append(r)
            taches.append(self.appeler(profil, a_plat, routes=options[:3]))
        resultats = await asyncio.gather(*taches, return_exceptions=True)
        avis = [(route1.nom, premiere["choices"][0]["message"].get("content") or "")]
        for r, res in zip(choisies, resultats):
            if isinstance(res, Exception):
                log.warning("conseil : %s et ses remplacants indisponibles (%s)", r.nom, res)
                continue
            if res[1].nom in {nom for nom, _ in avis}:
                continue  # un remplacant est tombe sur un modele deja entendu
            avis.append((res[1].nom, res[0]["choices"][0]["message"].get("content") or ""))
        if len(avis) < 2:
            return premiere, route1.nom
        question = derniere_question(corps["messages"])
        blocs = "\n\n".join(f"### Avis {i} ({nom})\n{texte}" for i, (nom, texte) in enumerate(avis, 1))
        synthese = {
            "messages": [
                {"role": "system", "content": (
                    "Tu es l'arbitre d'un conseil de plusieurs IA. A partir de leurs avis, ecris LA meilleure "
                    "reponse finale, en francais, directement a l'utilisateur : garde ce qui est juste et utile, "
                    "corrige les erreurs, signale en une phrase un vrai desaccord s'il y en a un. Ne mentionne pas "
                    "le conseil ni les avis. Recopie a l'identique les pseudonymes entre crochets comme "
                    "[PERSONNE_3].")},
                {"role": "user", "content": f"Question :\n{question}\n\n{blocs}"},
            ],
        }
        reponse, r_synth = await self.appeler(profil_synthese, synthese)
        noms = "+".join(n for n, _ in avis)
        return reponse, f"conseil[{noms}]->{r_synth.nom}"

    # --- Metriques Prometheus ---------------------------------------------
    def metriques(self) -> str:
        maintenant = time.time()
        lignes = [
            "# TYPE anonymiseur_requetes_total counter",
            "# TYPE anonymiseur_latence_secondes_total counter",
            "# TYPE anonymiseur_quota_jour_utilise gauge",
            "# TYPE anonymiseur_quota_jour_max gauge",
            "# TYPE anonymiseur_route_en_pause gauge",
        ]
        for n, r in self.routes.items():
            r._purger(maintenant)
            etiq = f'route="{n}",fournisseur="{r.fournisseur}"'
            s = self.stats[n]
            lignes += [
                f'anonymiseur_requetes_total{{{etiq},statut="ok"}} {s["ok"]}',
                f'anonymiseur_requetes_total{{{etiq},statut="echec"}} {s["echec"]}',
                f"anonymiseur_latence_secondes_total{{{etiq}}} {s['latence']:.3f}",
                f"anonymiseur_quota_jour_utilise{{{etiq}}} {r.jour_n}",
                f"anonymiseur_route_en_pause{{{etiq}}} {1 if r.pause_jusqua > maintenant else 0}",
            ]
            if r.rpd is not None:
                lignes.append(f"anonymiseur_quota_jour_max{{{etiq}}} {r.rpd}")
        return "\n".join(lignes) + "\n"


def aplatir(messages: list[dict]) -> list[dict]:
    """Conversation sans appels d'outils (pour les membres du conseil)."""
    sortie: list[dict] = []
    for m in messages:
        role, contenu = m.get("role"), m.get("content")
        if isinstance(contenu, list):
            contenu = "\n".join(p.get("text", "") for p in contenu if p.get("type") == "text")
        contenu = contenu or ""
        if role == "assistant" and m.get("tool_calls"):
            appels = "; ".join(f"{tc['function']['name']}({tc['function'].get('arguments', '')})"
                               for tc in m["tool_calls"])
            contenu = (contenu + f"\n(Outils appeles : {appels})").strip()
        if role == "tool":
            role, contenu = "user", f"Resultat d'outil :\n{contenu}"
        if role not in ("system", "user", "assistant"):
            role = "user"
        if sortie and sortie[-1]["role"] == role and role != "system":
            sortie[-1]["content"] += "\n\n" + contenu
        else:
            sortie.append({"role": role, "content": contenu})
    return sortie


def derniere_question(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = "\n".join(p.get("text", "") for p in c if p.get("type") == "text")
            return c or ""
    return ""
