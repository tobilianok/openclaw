"""Tests de bout en bout de jarvis-ssh, avec un VRAI serveur SSH.

Prerequis (machine de test jetable, en root) : sshd ecoutant sur
127.0.0.1:${JARVIS_TEST_PORT:-2222}. Le test installe l'utilisateur "jarvis"
avec infra/jarvis/installer-jarvis.sh, comme sur une vraie machine.
Lancer : JARVIS_TEST_SSH=1 python -m pytest tests  (depuis n8n/jarvis-ssh/)
"""
import html
import json
import os
import re
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.service import Executeur, creer_app

RACINE = Path(__file__).resolve().parents[3]
PORT = int(os.environ.get("JARVIS_TEST_PORT", "2222"))
pytestmark = pytest.mark.skipif(not os.environ.get("JARVIS_TEST_SSH"), reason="JARVIS_TEST_SSH non defini")

CONFIG = {
    "machines": {
        "srv-nas": {"hote": "127.0.0.1", "description": "test"},
        "docker": {"hote": "127.0.0.1", "description": "test"},
        "ml150": {"hote": "127.0.0.1", "description": "test"},
    },
    "protections": [
        {"machines": ["ml150"], "motif": r"\b(qm|pct)\s+(stop|shutdown|reboot|reset|destroy|suspend|hibernate|migrate|set|unlink|resize)\b[^\n]*\b(106|102)\b",
         "raison": "VM 106/102"},
        {"machines": ["ml150", "docker"], "motif": r"(\b(reboot|shutdown|poweroff|halt|kexec)\b|\binit\s+[06]\b)", "raison": "reboot"},
        {"machines": ["docker"], "motif": r"\bdocker\b[^\n]*\b(stop|kill|rm|restart|pause|down|rename|update|prune)\b[^\n]*\b(n8n|postgres|jarvis-ssh|npm|nginx-proxy|proxy-manager)",
         "raison": "n8n"},
    ],
    "expiration_minutes": 30,
}


class FauxServices:
    """Faux Talk + faux OpenClaw : enregistrent ce qu'ils recoivent."""

    def __init__(self):
        self.talk, self.hooks = [], []

    def __call__(self, req: httpx.Request):
        corps = json.loads(req.content)
        if "/bot/" in req.url.path:
            assert req.headers["X-Nextcloud-Talk-Bot-Signature"]
            self.talk.append(corps["message"])
            return httpx.Response(201, json={})
        self.hooks.append(corps)
        return httpx.Response(200, json={"ok": True})


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    os.environ.update(JARVIS_SSH_TOKEN="tok", JARVIS_SSH_DATA=str(data), JARVIS_URL_PUBLIQUE="https://jarvis.test",
                      NEXTCLOUD_URL="https://nc.test", NEXTCLOUD_TALK_BOT_SECRET="s3cret", TALK_ROOM_MAJORDOME="salon1",
                      OPENCLAW_URL="http://oc.test", OPENCLAW_HOOKS_TOKEN="hk")
    faux = FauxServices()
    executeur = Executeur(data, port=PORT)
    app = creer_app(CONFIG, executeur=executeur, client=httpx.AsyncClient(transport=httpx.MockTransport(faux)))
    lec = (data / "cles" / "lecture.pub").read_text().strip()
    act = (data / "cles" / "action.pub").read_text().strip()
    subprocess.run(["bash", str(RACINE / "infra/jarvis/installer-jarvis.sh"), lec, act, "127.0.0.1"], check=True)
    with TestClient(app) as client:  # une seule boucle d'evenements, comme en production
        yield client, faux
    subprocess.run(["bash", str(RACINE / "infra/jarvis/installer-jarvis.sh"), "--desinstaller"], check=False)


H = {"Authorization": "Bearer tok"}


def lire(client, machine, commande):
    r = client.post("/lecture", json={"machine": machine, "commande": commande}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_jeton_obligatoire(env):
    client, _ = env
    assert client.post("/lecture", json={"machine": "srv-nas", "commande": "uptime"}).status_code == 401
    assert client.post("/demande", json={}, headers={"Authorization": "Bearer faux"}).status_code == 401


def test_lecture_autorisee(env):
    client, _ = env
    r = lire(client, "srv-nas", "uptime")
    assert r["code"] == 0 and "load average" in r["sortie"], r
    r = lire(client, "srv-nas", "free -h")
    assert r["code"] == 0 and "Mem" in r["sortie"]


def test_lecture_refusee_cote_serveur(env):
    client, _ = env
    for commande in ["cat /etc/shadow", "rm -rf /tmp/x", "systemctl restart ssh", "bash"]:
        r = lire(client, "srv-nas", commande)
        assert r["code"] == 126 and "REFUSE" in r["sortie"], (commande, r)
    r = lire(client, "srv-nas", "uptime; id")  # refuse des le service
    assert r["code"] == 126


def test_machine_inconnue(env):
    client, _ = env
    r = client.post("/lecture", json={"machine": "majordome", "commande": "uptime"}, headers=H)
    assert r.status_code == 400 and "machine inconnue" in r.text


def test_cle_lecture_ne_donne_pas_de_shell(env):
    """Meme en contournant le service, la cle lecture reste bridee par le serveur."""
    _, _ = env
    data = Path(os.environ["JARVIS_SSH_DATA"])
    sortie = subprocess.run(["ssh", "-i", str(data / "cles/lecture"), "-p", str(PORT), "-o", "BatchMode=yes",
                             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                             "jarvis@127.0.0.1", "--", "id"], capture_output=True, text=True)
    assert sortie.returncode == 126 and "REFUSE" in sortie.stderr + sortie.stdout


@pytest.mark.parametrize("machine,commande", [
    ("docker", "docker stop n8n-n8n-1"),
    ("docker", "sudo reboot"),
    ("ml150", "qm shutdown 106"),
    ("ml150", "qm stop 102 --skiplock"),
])
def test_garde_fous(env, machine, commande):
    client, faux = env
    avant = len(faux.talk)
    r = client.post("/demande", headers=H, json={"machine": machine, "commandes": commande,
                                                 "explication": "x", "risques": "x", "retour_arriere": "x"})
    assert r.json()["statut"] == "refusee_d_office", r.text
    assert len(faux.talk) == avant  # rien n'est meme propose a Louis


def test_champs_obligatoires(env):
    client, _ = env
    r = client.post("/demande", headers=H, json={"machine": "srv-nas", "commandes": "id"})
    assert r.status_code == 400 and "explication" in r.text


def creer_demande(client, faux, commandes):
    r = client.post("/demande", headers=H, json={
        "machine": "srv-nas", "commandes": commandes, "explication": "Test de bout en bout",
        "risques": "Aucun", "retour_arriere": "rm du fichier"})
    corps = r.json()
    assert corps["statut"] == "en_attente_de_validation", corps
    assert "jarvis.test" not in json.dumps(corps)  # Jarvis ne voit jamais le lien
    lien = re.search(r"https://jarvis\.test(/d/\w+)\?j=([\w-]+)", faux.talk[-1])
    assert lien, faux.talk[-1]
    return corps["id"], lien.group(1), lien.group(2)


def attendre_statut(client, chemin, jeton, statut, delai=30):
    fin = time.time() + delai
    while time.time() < fin:
        if statut in client.get(chemin, params={"j": jeton}).text:
            return True
        time.sleep(0.5)
    return False


def test_demande_validee_execute_en_root(env, tmp_path):
    client, faux = env
    temoin = "/tmp/jarvis-temoin"
    subprocess.run(["rm", "-f", temoin])
    ident, chemin, jeton = creer_demande(client, faux, f"touch {temoin}\nstat -c %U {temoin}")
    assert not Path(temoin).exists()                       # rien avant validation
    assert "Valider et exécuter" in client.get(chemin, params={"j": jeton}).text
    assert client.get(chemin, params={"j": "mauvais"}).status_code == 404
    assert client.post(f"{chemin}/valider", data={"j": "mauvais"}).status_code == 404
    assert not Path(temoin).exists()
    r = client.post(f"{chemin}/valider", data={"j": jeton})
    assert "exécution en cours" in r.text or "Terminée" in r.text
    assert attendre_statut(client, chemin, jeton, "Terminée")
    assert Path(temoin).exists() and Path(temoin).stat().st_uid == 0   # execute en root
    assert "terminée" in faux.talk[-1] and "root" in faux.talk[-1]
    assert faux.hooks[-1]["agentId"] == "majordome" and f"#{ident}" in faux.hooks[-1]["message"]
    # Une 2e validation ne relance rien
    r = client.post(f"{chemin}/valider", data={"j": jeton})
    assert "n'est plus en attente" in html.unescape(r.text)
    subprocess.run(["rm", "-f", temoin])


def test_echec_arrete_la_suite(env):
    client, faux = env
    temoin = "/tmp/jarvis-temoin-2"
    ident, chemin, jeton = creer_demande(client, faux, f"false\ntouch {temoin}")
    client.post(f"{chemin}/valider", data={"j": jeton})
    assert attendre_statut(client, chemin, jeton, "Échec")
    assert not Path(temoin).exists()
    assert "non lancée" in faux.talk[-1]


def test_demande_refusee(env):
    client, faux = env
    temoin = "/tmp/jarvis-temoin-3"
    ident, chemin, jeton = creer_demande(client, faux, f"touch {temoin}")
    r = client.post(f"{chemin}/refuser", data={"j": jeton})
    assert "Refusée" in r.text
    assert "n'est plus en attente" in html.unescape(client.post(f"{chemin}/valider", data={"j": jeton}).text)
    time.sleep(1)
    assert not Path(temoin).exists()
    assert "refusée" in faux.talk[-1] and "REFUSE" in faux.hooks[-1]["message"]


def test_demande_expiree(env):
    client, faux = env
    temoin = "/tmp/jarvis-temoin-4"
    ident, chemin, jeton = creer_demande(client, faux, f"touch {temoin}")
    client.app.state.demandes.db.execute("UPDATE demandes SET expire = 0 WHERE id = ?", (ident,))
    client.app.state.demandes.db.commit()
    assert "Expirée" in client.get(chemin, params={"j": jeton}).text
    r = client.post(f"{chemin}/valider", data={"j": jeton})
    assert "n'est plus en attente" in html.unescape(r.text)
    time.sleep(1)
    assert not Path(temoin).exists()


def test_pseudonyme_non_retabli_jamais_propose(env):
    client, faux = env
    avant = len(faux.talk)
    r = client.post("/demande", headers=H, json={
        "machine": "srv-nas", "commandes": "cd /home/[UTILISATNANT_1]/immich-stack && docker compose pull",
        "explication": "x", "risques": "x", "retour_arriere": "x"})
    assert r.json()["statut"] == "erreur_pseudonyme" and r.json()["pseudonyme"] == "[UTILISATNANT_1]"
    assert len(faux.talk) == avant
    # Les tests bash ordinaires ne sont pas pris pour des pseudonymes
    r = client.post("/demande", headers=H, json={
        "machine": "srv-nas", "commandes": "[ -f /tmp/x ] && [[ -d /tmp ]] && grep '[A-Z]' /etc/hostname",
        "explication": "x", "risques": "x", "retour_arriere": "x"})
    assert r.json()["statut"] == "en_attente_de_validation"
