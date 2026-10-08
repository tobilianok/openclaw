"""Detection et remplacement des donnees personnelles dans du texte francais.

Trois sources, par ordre de priorite :
  1. le dictionnaire personnel (noms des proches, adresse, domaines...) :
     le plus fiable, a remplir soi-meme ;
  2. des regles (email, telephone, IBAN, carte, n° de secu, adresse postale,
     IP publique) ;
  3. la reconnaissance d'entites nommees de spaCy (noms de personnes, et en
     option lieux et organisations).
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from functools import lru_cache

from .coffre import Coffre, normaliser

# Un nom de personne : 1 a 4 mots commencant par une majuscule (particules
# "de", "du", "van"... admises), lettres, tirets et apostrophes uniquement.
_MOT_NOM = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’]*(?:-[A-ZÀ-ÖØ-Þa-zà-öø-ÿ][a-zà-öø-ÿ'’]*)*"
FORME_NOM = re.compile(
    rf"^(?:(?:M\.|Mme|Mlle|Dr|Me|Pr)\.? )?{_MOT_NOM}(?: (?:(?:de|du|des|d'|da|van|von|le|la) ?)?{_MOT_NOM}){{0,3}}$")

def _tronquer_nom(mot: str) -> str:
    """'Paul Martin jeudi' -> 'Paul Martin' ; '' si ce n'est pas un nom."""
    mots = mot.split(" ")
    for n in range(min(len(mots), 5), 0, -1):
        candidat = " ".join(mots[:n])
        if FORME_NOM.match(candidat):
            return candidat
    return ""


MOTS_EN = frozenset("the and to of is you with for when this that use if not or be it are your only "
                    "do does should must can will from by on as an at any never always each".split())
MOTS_FR = frozenset("le la les de des et est un une pour que qui dans pas tu je il elle avec sur du au aux "
                    "ce cette son sa ses mon ma mes ton ta tes nous vous ne se en a".split())


def _ligne_francaise(texte: str, position: int, strict: bool = False) -> bool:
    debut = texte.rfind("\n", 0, position) + 1
    fin = texte.find("\n", position)
    ligne = texte[debut:fin if fin != -1 else len(texte)].lower()
    mots = re.findall(r"[a-zà-ÿ']+", ligne)
    en = sum(m in MOTS_EN for m in mots)
    fr = sum(m in MOTS_FR for m in mots)
    # strict (prompt systeme, surtout en anglais) : il faut des indices de
    # francais ; sinon (tes messages) : dans le doute, on anonymise.
    return fr > en if strict else fr >= en

# Jetons deja poses : "[PERSONNE_3]", "[UTILISATEUR]"...
MOTIF_JETON = re.compile(r"\[[A-Z][A-Z0-9_]*\]")

LETTRE = "A-Za-zÀ-ÖØ-öø-ÿ"
REGLES: list[tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,4})?\b")),
    ("NIR", re.compile(r"\b[12] ?\d{2} ?(?:0[1-9]|1[0-2]|[2-9]\d) ?(?:\d{2}|2[AB]) ?\d{3} ?\d{3}(?: ?\d{2})?\b")),
    ("CARTE", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("TELEPHONE", re.compile(r"(?<![\w+])(?:\+33 ?|0033 ?|0)[1-9](?:[ .-]?\d{2}){4}\b")),
    ("ADRESSE", re.compile(
        rf"\b\d{{1,4}}(?: ?(?:bis|ter))?,? (?:rue|avenue|av\.|boulevard|bd|chemin|impasse|allée|allee|place|"
        rf"route|quai|cours|square|lotissement|résidence|residence|hameau|lieu-dit)"
        rf"(?: [{LETTRE}'’-]+){{1,6}}(?:,? \d{{5}}(?: [{LETTRE}'’-]+){{1,3}})?",
        re.IGNORECASE)),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
]


_ECHAPPEMENT = re.compile(r"\\u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})")


def decoder_unicode(texte: str) -> str:
    """Remplace les sequences \\uXXXX (y compris paires de substitution)."""
    if "\\u" not in texte:
        return texte

    def remplacer(m: re.Match) -> str:
        if m.group(3):
            code = int(m.group(3), 16)
            if 0xD800 <= code <= 0xDFFF or code < 0x20:
                return m.group(0)  # moitie de paire isolee, ou caractere de controle
            return chr(code)
        haut, bas = int(m.group(1), 16), int(m.group(2), 16)
        return chr(0x10000 + ((haut - 0xD800) << 10) + (bas - 0xDC00))

    return _ECHAPPEMENT.sub(remplacer, texte)


def _luhn(chiffres: str) -> bool:
    total, double = 0, False
    for c in reversed(chiffres):
        d = int(c)
        if double:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        double = not double
    return total % 10 == 0


def _valide(categorie: str, texte: str) -> bool:
    if categorie == "CARTE":
        chiffres = re.sub(r"\D", "", texte)
        return 13 <= len(chiffres) <= 19 and _luhn(chiffres)
    if categorie == "IP":
        try:
            ip = ipaddress.ip_address(texte)
        except ValueError:
            return False
        # Les IP du reseau local ne disent rien de toi : on les garde.
        return ip.is_global
    if categorie == "IBAN":
        return len(re.sub(r"\s", "", texte)) >= 15
    return True


@dataclass(frozen=True)
class Zone:
    debut: int
    fin: int
    categorie: str
    priorite: int  # plus petit = plus fort
    jeton: str | None = None  # deja connu (dictionnaire)


class Anonymiseur:
    def __init__(self, coffre: Coffre, dictionnaire: dict, ner: dict, nlp=None):
        self.coffre = coffre
        self.nlp = nlp
        self.ner_categories = {k for k, v in (ner or {}).items() if v}
        self.jamais = {normaliser(m) for m in dictionnaire.get("jamais", [])}
        self._formes: dict[str, str] = {}  # forme normalisee -> jeton
        for p in dictionnaire.get("personnes", []):
            self._groupe("PERSONNE", p)
        for t in dictionnaire.get("termes", []):
            self._groupe(t.get("categorie", "TERME").upper(), t)
        # Pas de lettre ni "@" juste avant : "auth.louisrousseaux.fr" est bien
        # masque (sous-domaine), et une adresse e-mail entiere reste prise par
        # la regle EMAIL, plus longue.
        formes = sorted(self._formes, key=len, reverse=True)
        self._motif_dico = (
            re.compile(r"(?<![\w@])(?:" + "|".join(re.escape(f) for f in formes) + r")(?![\w@]|\.\w)", re.IGNORECASE)
            if formes else None
        )

    def _groupe(self, categorie: str, entree: dict) -> None:
        formes = [f for f in entree.get("formes", []) if f.strip()]
        if not formes:
            return
        jeton = self.coffre.enregistrer_groupe(
            categorie, formes, entree.get("affichage", formes[0]), entree.get("jeton"))
        for f in formes:
            self._formes[normaliser(f)] = jeton

    # --- Detection ---------------------------------------------------------
    def _zones(self, texte: str, strict: bool = False, utilisateur: bool = False) -> list[Zone]:
        zones: list[Zone] = []
        if self._motif_dico:
            for m in self._motif_dico.finditer(texte):
                zones.append(Zone(m.start(), m.end(), "DICO", 0, self._formes[normaliser(m.group())]))
        for i, (cat, motif) in enumerate(REGLES, start=1):
            for m in motif.finditer(texte):
                if _valide(cat, m.group()):
                    zones.append(Zone(m.start(), m.end(), cat, i))
        if self.nlp is not None and self.ner_categories:
            for debut, fin, cat in _entites(self.nlp, texte):
                # Une entite ne deborde jamais sur la ligne suivante (format
                # iCalendar, JSON...) et ne contient pas de chiffres (dates).
                coupure = texte.find("\n", debut, fin)
                if coupure != -1:
                    fin = coupure
                mot = texte[debut:fin].rstrip()
                fin = debut + len(mot)
                if not mot or any(c.isdigit() for c in mot):
                    continue
                # "Runtime: ...", "os=Linux" : une etiquette, pas une personne
                if texte[fin:fin + 1] in (":", "="):
                    continue
                if cat in ("PERSONNE", "NOM_PROPRE"):
                    mot = _tronquer_nom(mot)
                    if not mot:
                        continue
                    fin = debut + len(mot)
                # Le modele francais voit des "noms" dans l'anglais (prompt
                # systeme d'OpenClaw) : on ne le croit que sur du francais.
                if not _ligne_francaise(texte, debut, strict):
                    continue
                # Sigles courts ("RDV", "EDF", "SMS") : faux positifs frequents
                if mot.isupper() and len(mot) <= 5:
                    continue
                if cat == "NOM_PROPRE":
                    if not utilisateur or "PERSONNE" not in self.ner_categories:
                        continue
                    cat = "PERSONNE"
                if cat in self.ner_categories:
                    zones.append(Zone(debut, fin, cat, 50))
        # Ne jamais retoucher un jeton deja pose.
        proteges = [(m.start(), m.end()) for m in MOTIF_JETON.finditer(texte)]
        zones = [z for z in zones if not any(z.debut < f and d < z.fin for d, f in proteges)]
        # Chevauchements : dictionnaire et regles passent avant spaCy ; entre
        # eux, la zone la plus longue gagne (une adresse email entiere plutot
        # que le prenom qu'elle contient).
        zones.sort(key=lambda z: (z.priorite >= 50, -(z.fin - z.debut), z.priorite, z.debut))
        retenues: list[Zone] = []
        for z in zones:
            if all(z.fin <= r.debut or z.debut >= r.fin for r in retenues):
                retenues.append(z)
        return sorted(retenues, key=lambda z: z.debut)

    # --- Remplacement ------------------------------------------------------
    def anonymiser(self, texte: str, strict: bool = False, utilisateur: bool = False) -> str:
        """strict=True pour le prompt systeme : detection des noms plus
        prudente. utilisateur=True pour les messages de Louis : on masque
        aussi les noms propres en milieu de phrase. Le dictionnaire et les
        regles s'appliquent toujours."""
        if not texte or not texte.strip():
            return texte
        # Du texte encode en JSON ("In\\u00e8s") echapperait a la detection,
        # alors que l'IA le lit sans peine : on decode avant d'analyser.
        texte = decoder_unicode(texte)
        morceaux, curseur = [], 0
        for z in self._zones(texte, strict, utilisateur):
            valeur = texte[z.debut:z.fin]
            if z.jeton is None:
                propre = valeur.strip(" .,;:!?'’\"()")
                if len(propre) < 2 or normaliser(propre) in self.jamais:
                    continue
                jeton = self.coffre.jeton(z.categorie, propre)
                # garder la ponctuation retiree autour de la valeur
                d = valeur.find(propre)
                remplacement = valeur[:d] + jeton + valeur[d + len(propre):]
            else:
                remplacement = z.jeton
            morceaux.append(texte[curseur:z.debut])
            morceaux.append(remplacement)
            curseur = z.fin
        morceaux.append(texte[curseur:])
        return "".join(morceaux)

    def anonymiser_json(self, brut: str) -> str:
        """Arguments d'outil (JSON) : anonymise chaque chaine, garde la structure."""
        try:
            donnees = json.loads(brut)
        except (TypeError, ValueError):
            return self.anonymiser(brut)
        return json.dumps(_parcourir(donnees, self.anonymiser), ensure_ascii=False)

    def retablir(self, texte: str) -> str:
        """Remplace les jetons par les vraies valeurs (reponse de l'IA)."""
        if not texte:
            return texte

        def remplacer(m: re.Match) -> str:
            return self.coffre.valeur(f"[{m.group(1)}]") or m.group(0)

        # Les IA perdent parfois les crochets : "PERSONNE_3" est aussi accepte.
        return re.sub(r"\[?\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)\b\]?",
                      lambda m: remplacer(m) if self.coffre.valeur(f"[{m.group(1)}]") else m.group(0),
                      texte)

    def retablir_json(self, brut: str) -> str:
        try:
            donnees = json.loads(brut)
        except (TypeError, ValueError):
            return self.retablir(brut)
        return json.dumps(_parcourir(donnees, self.retablir), ensure_ascii=False)


def _parcourir(donnees, fonction):
    if isinstance(donnees, str):
        return fonction(donnees)
    if isinstance(donnees, list):
        return [_parcourir(x, fonction) for x in donnees]
    if isinstance(donnees, dict):
        return {k: _parcourir(v, fonction) for k, v in donnees.items()}
    return donnees


NER_ETIQUETTES = {"PER": "PERSONNE", "LOC": "LIEU", "ORG": "ORGANISATION"}


@lru_cache(maxsize=4096)
def _entites_cache(nlp_id: int, texte: str) -> tuple:
    nlp = _NLP_REGISTRE[nlp_id]
    doc = nlp(texte)
    entites = [(e.start_char, e.end_char, NER_ETIQUETTES[e.label_])
               for e in doc.ents if e.label_ in NER_ETIQUETTES]
    # Noms propres que le modele n'a pas classes ("Salut Sophie", "Dis a
    # Sophie") : suites de PROPN en milieu de phrase.
    i = 0
    while i < len(doc):
        t = doc[i]
        if t.pos_ == "PROPN" and t.text[:1].isupper():
            j = i
            while j + 1 < len(doc) and doc[j + 1].pos_ == "PROPN" and doc[j + 1].text[:1].isupper():
                j += 1
            avant = texte[:t.idx].rstrip()
            debut_phrase = not avant or avant[-1] in ".!?:;{[(\"'\n" or avant.endswith("\\n")
            # En debut de phrase, le 1er mot est souvent un mot courant avec
            # majuscule ("Salut Sophie", "Merci Paul") : on le saute.
            k = i + 1 if debut_phrase else i
            if k <= j:
                entites.append((doc[k].idx, doc[j].idx + len(doc[j].text), "NOM_PROPRE"))
            i = j + 1
        else:
            i += 1
    return tuple(entites)


_NLP_REGISTRE: dict[int, object] = {}


def _entites(nlp, texte: str):
    # Le prompt systeme (profil, memoire) est renvoye a chaque tour : le cache
    # evite de le re-analyser.
    _NLP_REGISTRE[id(nlp)] = nlp
    return _entites_cache(id(nlp), texte)
