"""Coffre des pseudonymes : valeur reelle <-> jeton, persistant (SQLite).

Un meme nom donne toujours le meme jeton, d'une requete a l'autre : l'IA
cloud peut suivre une conversation sans jamais voir la vraie valeur.
Le coffre ne quitte jamais la VM.
"""
import re
import sqlite3
import threading
import unicodedata


def normaliser(valeur: str) -> str:
    v = unicodedata.normalize("NFKC", valeur).casefold()
    return re.sub(r"\s+", " ", v).strip()


class Coffre:
    def __init__(self, chemin: str):
        self._verrou = threading.Lock()
        self._db = sqlite3.connect(chemin, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jetons (
                jeton TEXT PRIMARY KEY,
                categorie TEXT NOT NULL,
                affichage TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS formes (
                cle TEXT PRIMARY KEY,
                jeton TEXT NOT NULL REFERENCES jetons(jeton)
            );
            """
        )
        self._db.commit()
        # Caches memoire (le coffre reste petit : quelques milliers d'entrees)
        self._par_cle = dict(self._db.execute("SELECT cle, jeton FROM formes"))
        self._par_jeton = {j: a for j, a in self._db.execute("SELECT jeton, affichage FROM jetons")}

    def _nouveau_jeton(self, categorie: str) -> str:
        n = 1 + sum(1 for j in self._par_jeton if j.startswith(f"[{categorie}_"))
        return f"[{categorie}_{n}]"

    def jeton(self, categorie: str, valeur: str) -> str:
        """Jeton de `valeur`, cree au besoin."""
        cle = f"{categorie}:{normaliser(valeur)}"
        with self._verrou:
            j = self._par_cle.get(cle)
            if j:
                return j
            j = self._nouveau_jeton(categorie)
            self._db.execute("INSERT INTO jetons VALUES (?, ?, ?)", (j, categorie, valeur.strip()))
            self._db.execute("INSERT INTO formes VALUES (?, ?)", (cle, j))
            self._db.commit()
            self._par_cle[cle] = j
            self._par_jeton[j] = valeur.strip()
            return j

    def enregistrer_groupe(self, categorie: str, formes: list[str], affichage: str,
                           jeton_fixe: str | None = None, propre: bool = False) -> str:
        """Plusieurs formes (ex: 'Louis', 'Louis Rousseaux') -> un seul jeton.

        propre=True : le jeton ne doit appartenir qu'a cette forme (un ancien
        jeton partage avec d'autres formes n'est pas repris)."""
        with self._verrou:
            j = f"[{jeton_fixe}]" if jeton_fixe else None
            if not j:
                for f in formes:
                    j = self._par_cle.get(f"{categorie}:{normaliser(f)}")
                    if j and propre and normaliser(self._par_jeton.get(j, "")) != normaliser(affichage):
                        j = None
                    if j:
                        break
            if not j:
                j = self._nouveau_jeton(categorie)
            self._db.execute(
                "INSERT INTO jetons VALUES (?, ?, ?) ON CONFLICT(jeton) DO UPDATE SET affichage=excluded.affichage",
                (j, categorie, affichage),
            )
            self._par_jeton[j] = affichage
            for f in formes:
                cle = f"{categorie}:{normaliser(f)}"
                self._db.execute(
                    "INSERT INTO formes VALUES (?, ?) ON CONFLICT(cle) DO UPDATE SET jeton=excluded.jeton",
                    (cle, j),
                )
                self._par_cle[cle] = j
            self._db.commit()
            return j

    def valeur(self, jeton: str) -> str | None:
        return self._par_jeton.get(jeton)

    def taille(self) -> int:
        return len(self._par_jeton)
