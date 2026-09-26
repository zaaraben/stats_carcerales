#!/usr/bin/env python3
"""Mise à jour mensuelle de la carte « Densité carcérale » (mesdonneeslocales.fr).

Reprend le notebook 20260315_densite_carcerale_optimized.ipynb sous forme de
script autonome, exécutable à la main ou par GitHub Actions.

Étapes :
  1. Récupère le fichier Excel du ministère de la Justice
     (téléchargement automatique, ou fichier local avec --fichier).
  2. Vérifie la structure du fichier (onglets, colonnes attendues).
  3. Fusionne les chiffres avec le référentiel des établissements.
  4. Écrit stats_carcerales/data/stats_carcerales.json (GeoJSON, EPSG:3857).
  5. Met à jour le titre de la carte (stats_carcerales.xml) et la date de
     mise à jour (stats_carcerales/densite_carcerale.mst).
  6. Écrit un fichier de suivi stats_carcerales/data/stats_carcerales.meta.json.

Exemples :
  python scripts/maj_stats_carcerales.py                   # mois précédent, téléchargé
  python scripts/maj_stats_carcerales.py --mois 2026-08    # mois donné, téléchargé
  python scripts/maj_stats_carcerales.py --fichier stats.xlsx --mois 2026-08

Codes de sortie :
  0  mise à jour effectuée (ou déjà à jour)
  1  erreur : fichier illisible, structure inattendue, trop peu d'établissements…
  2  établissements absents du référentiel (seulement avec --strict)
  3  fichier du ministère pas encore publié
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

RACINE = Path(__file__).resolve().parent.parent          # racine du dépôt
APP = RACINE / "stats_carcerales"

REFERENTIEL = RACINE / "referentiel" / "referentiel_etablissements.xlsx"
SORTIE_GEOJSON = APP / "data" / "stats_carcerales.json"
SORTIE_META = APP / "data" / "stats_carcerales.meta.json"
FICHIER_XML = RACINE / "stats_carcerales.xml"
FICHIER_MST = APP / "densite_carcerale.mst"

URL_MODELE = (
    "https://www.justice.gouv.fr/sites/default/files/{dossier}/"
    "statistique_etablissements_personnes_ecrouees_01{mm}{aaaa}.xlsx"
)

# Onglets contenant le détail par établissement (Tab14 … Tab23)
ONGLET_DEBUT = 14
ONGLET_FIN = 24  # exclu

COLONNES_ATTENDUES = [
    "Etablissement",
    "Quartier (1)",
    "Capacité norme circulaire",
    "Capacité opérationnelle",
    "Ecroués détenus",
    "Densité carcérale",
]

# En dessous de cette proportion d'établissements retrouvés dans le
# référentiel, on considère que le fichier a changé de structure.
TAUX_CORRESPONDANCE_MIN = 0.90

MOIS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

FUSEAU = ZoneInfo("Europe/Paris")


class ErreurStructure(Exception):
    """Le fichier du ministère n'a pas la structure attendue."""


class FichierNonPublie(Exception):
    """Le fichier du mois n'est pas (encore) en ligne."""


# --------------------------------------------------------------------------
# Fonctions utilitaires (reprises du notebook)
# --------------------------------------------------------------------------

def convert_to_int(value):
    """Convertit en int si possible, sinon renvoie la valeur telle quelle."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return value


def pourcentage_conversion(val):
    """'99,0 %' -> 0.990. None si non convertible ('--', 'NC', 'Inf'…)."""
    if not isinstance(val, str):
        return val
    val = val.strip().replace(",", ".")
    if val.endswith("%"):
        try:
            return round(float(val.rstrip("%")) / 100, 3)
        except ValueError:
            return None
    return None


def replace_nan(obj):
    """Remplace récursivement les NaN par None (JSON valide)."""
    if isinstance(obj, float) and math.isnan(obj):
        return None
    if isinstance(obj, dict):
        return {k: replace_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [replace_nan(elem) for elem in obj]
    return obj


def row_to_geojson_feature(row):
    """Transforme une ligne du DataFrame final en Feature GeoJSON."""
    return {
        "type": "Feature",
        "id": row["id"],
        "geometry": {
            "type": "Point",
            "coordinates": row["geometry.coordinates"],
        },
        "geometry_name": "geom",
        "properties": {
            "id": row["properties.id"],
            "etablissement": row["properties.etablissement"],
            "quartier": row["properties.quartier"],
            "capacite_norme": row["properties.capacite_norme"],
            "capacite_oper": row["properties.capacite_oper"],
            "lc_disp": row["properties.lc_disp"],
            "cd_etablissement": convert_to_int(row["properties.cd_etablissement"]),
            "adresse_etab": row["properties.adresse_etab"],
            "ville_etab": row["properties.ville_etab"],
            "code_postal": row["properties.code_postal"],
            "latitude": row["properties.latitude"],
            "longitude": row["properties.longitude"],
            "ecroue_detenu": convert_to_int(row["properties.ecroue_detenu"]),
            "densite_car": row["properties.densite_car"],
        },
    }


# --------------------------------------------------------------------------
# 1. Récupération du fichier du ministère
# --------------------------------------------------------------------------

def mois_precedent(aujourdhui: date) -> tuple[int, int]:
    if aujourdhui.month == 1:
        return aujourdhui.year - 1, 12
    return aujourdhui.year, aujourdhui.month - 1


def urls_candidates(annee: int, mois: int) -> list[str]:
    """Le fichier du mois M est publié fin M dans le dossier AAAA-MM.
    En cas de publication tardive, il peut se trouver dans le dossier M+1."""
    suivant = (annee + 1, 1) if mois == 12 else (annee, mois + 1)
    dossiers = [f"{annee}-{mois:02d}", f"{suivant[0]}-{suivant[1]:02d}"]
    return [
        URL_MODELE.format(dossier=d, mm=f"{mois:02d}", aaaa=annee)
        for d in dossiers
    ]


def telecharger(annee: int, mois: int, dossier: Path) -> tuple[Path, str]:
    """Télécharge le fichier du mois. Renvoie (chemin local, url)."""
    for url in urls_candidates(annee, mois):
        print(f"Téléchargement : {url}")
        requete = urllib.request.Request(
            url, headers={"User-Agent": "mesdonneeslocales.fr (mise a jour mensuelle)"}
        )
        try:
            with urllib.request.urlopen(requete, timeout=60) as reponse:
                contenu = reponse.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                print("  -> absent (404)")
                continue
            raise
        if not contenu.startswith(b"PK"):  # un .xlsx est une archive zip
            print("  -> la réponse n'est pas un fichier Excel, ignorée")
            continue
        chemin = dossier / Path(url).name
        chemin.write_bytes(contenu)
        print(f"  -> OK ({len(contenu) // 1024} Ko)")
        return chemin, url
    raise FichierNonPublie(
        f"Aucun fichier trouvé pour {MOIS_FR[mois - 1]} {annee}."
    )


# --------------------------------------------------------------------------
# 2. Extraction et contrôle de la structure
# --------------------------------------------------------------------------

def trouver_ligne_entete(brut: pd.DataFrame, onglet: str) -> int:
    """Repère la ligne d'en-tête (celle qui contient 'Etablissement')
    au lieu de supposer qu'elle est toujours à la ligne 7."""
    for i in range(min(20, len(brut))):
        valeurs = [str(v).strip() for v in brut.iloc[i].tolist()]
        if "Etablissement" in valeurs:
            return i
    raise ErreurStructure(
        f"Onglet {onglet} : ligne d'en-tête 'Etablissement' introuvable."
    )


def extract_data(fichier: Path, onglet: str) -> pd.DataFrame:
    """Charge un onglet et retire les lignes de total."""
    brut = pd.read_excel(fichier, sheet_name=onglet, header=None)
    entete = trouver_ligne_entete(brut, onglet)
    df = pd.read_excel(fichier, sheet_name=onglet, skiprows=entete)

    manquantes = [c for c in COLONNES_ATTENDUES if c not in df.columns]
    if manquantes:
        raise ErreurStructure(
            f"Onglet {onglet} : colonnes absentes {manquantes}. "
            f"Colonnes trouvées : {list(df.columns)}"
        )

    df = df[~df["Etablissement"].astype(str).str.startswith("Total")]
    df = df.dropna(subset=["Quartier (1)"])
    df = df[~df["Quartier (1)"].astype(str).str.contains("Total", na=False)]
    return df.reset_index(drop=True)


def collect_all_data(fichier: Path, debut: int, fin: int) -> pd.DataFrame:
    onglets_presents = pd.ExcelFile(fichier).sheet_names
    onglets = [f"Tab{i}" for i in range(debut, fin)]
    absents = [o for o in onglets if o not in onglets_presents]
    if absents:
        raise ErreurStructure(
            f"Onglets absents : {absents}. Onglets du fichier : {onglets_presents}"
        )
    frames = [extract_data(fichier, o) for o in onglets]
    return pd.concat(frames, ignore_index=True)


def _normalize_col(series: pd.Series) -> pd.Series:
    """Retire les espaces ('1 234') et convertit en nombre (NaN sinon)."""
    return pd.to_numeric(
        series.astype(str).str.replace(" ", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(" ", "", regex=False),
        errors="coerce",
    )


def normalize_data(fichier: Path, debut: int, fin: int) -> pd.DataFrame:
    data = collect_all_data(fichier, debut, fin)

    for col in ["Capacité norme circulaire", "Capacité opérationnelle"]:
        data[col] = _normalize_col(data[col]).apply(
            lambda x: int(x) if pd.notna(x) else x
        )

    data["Ecroués détenus"] = (
        _normalize_col(data["Ecroués détenus"])
        .astype(object)
        .where(lambda s: s.notna(), "NC")
        .apply(lambda x: int(x) if x != "NC" else x)
    )
    return data


# --------------------------------------------------------------------------
# 3. Fusion avec le référentiel et création du GeoJSON
# --------------------------------------------------------------------------

def comparer_referentiel(new_data: pd.DataFrame, reference_df: pd.DataFrame) -> dict:
    ref_index = reference_df.index
    new_index = pd.MultiIndex.from_frame(new_data[["Etablissement", "Quartier (1)"]])
    return {
        "nb_referentiel": len(ref_index),
        "nb_nouvelles": len(new_index),
        "nb_communes": len(ref_index.intersection(new_index)),
        "absents_referentiel": [list(x) for x in new_index.difference(ref_index)],
        "absents_nouvelles_donnees": [list(x) for x in ref_index.difference(new_index)],
    }


def charger_referentiel(chemin: Path) -> pd.DataFrame:
    ref = pd.read_excel(chemin)
    ref.set_index(["properties.etablissement", "properties.quartier"], inplace=True)
    for col in ["properties.ecroue_detenu", "properties.densite_car",
                "properties.capacite_norme", "properties.capacite_oper"]:
        if col in ref.columns:
            ref[col] = ref[col].astype(object)
    return ref


def _en_texte_entier(x):
    if x == "NC" or (isinstance(x, float) and math.isnan(x)):
        return x
    return str(int(x))


def construire_geojson(df: pd.DataFrame, reference_df: pd.DataFrame) -> dict:
    new_df = pd.DataFrame({
        "properties.etablissement": df["Etablissement"].values,
        "properties.quartier": df["Quartier (1)"].values,
        "properties.capacite_norme": df["Capacité norme circulaire"].values,
        "properties.capacite_oper": df["Capacité opérationnelle"].values,
        "properties.ecroue_detenu": df["Ecroués détenus"].values,
        "properties.densite_car": df["Densité carcérale"].values,
    }).set_index(["properties.etablissement", "properties.quartier"])

    final = reference_df.copy()
    final.update(new_df)

    final["geometry.coordinates"] = final["geometry.coordinates"].apply(
        lambda x: ast.literal_eval(x) if isinstance(x, str) else x
    )
    final["properties.densite_car"] = final["properties.densite_car"].apply(
        pourcentage_conversion
    )

    # Les établissements sans capacité (absents des nouvelles données) sont retirés
    final_clean = final.dropna(subset=["properties.capacite_norme"]).copy()
    final_clean["properties.cd_etablissement"] = (
        final_clean["properties.cd_etablissement"].apply(convert_to_int)
    )
    final_clean.reset_index(inplace=True)
    for col in ["properties.capacite_norme", "properties.capacite_oper"]:
        final_clean[col] = final_clean[col].apply(_en_texte_entier)
    final_clean["properties.ecroue_detenu"] = final_clean["properties.ecroue_detenu"].apply(
        lambda x: int(x) if x != "NC" else x
    )

    features = [row_to_geojson_feature(row) for _, row in final_clean.iterrows()]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {
        "type": "FeatureCollection",
        "features": features,
        "totalFeatures": len(features),
        "numberMatched": len(features),
        "numberReturned": len(features),
        "timeStamp": now,
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3857"}},
    }


def verifier_geojson(geojson: dict, nb_attendus: int) -> None:
    """Contrôles de cohérence avant d'écrire quoi que ce soit."""
    features = geojson["features"]
    if nb_attendus == 0 or len(features) / nb_attendus < TAUX_CORRESPONDANCE_MIN:
        raise ErreurStructure(
            f"Seulement {len(features)} établissements retrouvés sur {nb_attendus} "
            "lignes du fichier : les noms ou la structure ont probablement changé."
        )
    for f in features:
        x, y = f["geometry"]["coordinates"]
        # Emprise EPSG:3857 large (métropole + outre-mer)
        if not (-20037508 <= x <= 20037508 and -20037508 <= y <= 20037508):
            raise ErreurStructure(f"Coordonnées invalides pour {f['properties']['etablissement']}")
        d = f["properties"]["densite_car"]
        if isinstance(d, (int, float)) and not math.isnan(d) and not (0 <= d <= 5):
            raise ErreurStructure(
                f"Densité improbable ({d}) pour {f['properties']['etablissement']}"
            )


# --------------------------------------------------------------------------
# 4. Mise à jour du titre et de la date d'affichage
# --------------------------------------------------------------------------

def libelle_date(annee: int, mois: int) -> str:
    return f"1er {MOIS_FR[mois - 1]} {annee}"


def maj_titre(annee: int, mois: int) -> None:
    texte = FICHIER_XML.read_text(encoding="utf-8")
    nouveau, n = re.subn(
        r"au 1er [a-zéû]+ \d{4}", f"au {libelle_date(annee, mois)}", texte
    )
    if n == 0:
        print(f"ATTENTION : titre non trouvé dans {FICHIER_XML.name}, non modifié.")
        return
    FICHIER_XML.write_text(nouveau, encoding="utf-8")


def maj_date_template(jour: date) -> None:
    texte = FICHIER_MST.read_text(encoding="utf-8")
    nouveau, n = re.subn(
        r"Mise à jour : \d{2}/\d{2}/\d{4}",
        f"Mise à jour : {jour.strftime('%d/%m/%Y')}",
        texte,
    )
    if n == 0:
        print(f"ATTENTION : date non trouvée dans {FICHIER_MST.name}, non modifiée.")
        return
    FICHIER_MST.write_text(nouveau, encoding="utf-8")


def deja_a_jour(annee: int, mois: int) -> bool:
    if not SORTIE_META.exists():
        return False
    meta = json.loads(SORTIE_META.read_text(encoding="utf-8"))
    return meta.get("mois_donnees") == f"{annee}-{mois:02d}"


# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mois", help="Mois des données, AAAA-MM (défaut : mois précédent)")
    parser.add_argument("--fichier", type=Path, help="Fichier Excel local (pas de téléchargement)")
    parser.add_argument("--referentiel", type=Path, default=REFERENTIEL)
    parser.add_argument("--onglets", default=f"{ONGLET_DEBUT}-{ONGLET_FIN - 1}",
                        help="Plage d'onglets TabN, ex. 14-23 (défaut)")
    parser.add_argument("--force", action="store_true",
                        help="Retraiter même si ce mois a déjà été publié")
    parser.add_argument("--strict", action="store_true",
                        help="Échouer si des établissements manquent au référentiel")
    args = parser.parse_args(argv)

    aujourdhui = datetime.now(FUSEAU).date()
    if args.mois:
        annee, mois = (int(p) for p in args.mois.split("-"))
    else:
        annee, mois = mois_precedent(aujourdhui)
    debut, fin = (int(p) for p in args.onglets.split("-"))
    fin += 1

    print(f"== Densité carcérale au {libelle_date(annee, mois)} ==")

    if not args.force and not args.fichier and deja_a_jour(annee, mois):
        print("Déjà à jour, rien à faire.")
        return 0

    try:
        with tempfile.TemporaryDirectory() as tmp:
            if args.fichier:
                fichier, source = args.fichier, str(args.fichier)
            else:
                fichier, source = telecharger(annee, mois, Path(tmp))

            new_data = normalize_data(fichier, debut, fin)
            reference_df = charger_referentiel(args.referentiel)
            diag = comparer_referentiel(new_data, reference_df)
            geojson = construire_geojson(new_data, reference_df)
            verifier_geojson(geojson, len(new_data))
    except FichierNonPublie as e:
        print(f"PAS ENCORE PUBLIÉ : {e}")
        return 3
    except ErreurStructure as e:
        print(f"ERREUR : {e}")
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"ERREUR : téléchargement impossible ({e}).")
        return 1

    print(f"Lignes dans le fichier     : {diag['nb_nouvelles']}")
    print(f"Établissements sur la carte: {len(geojson['features'])}")
    if diag["absents_referentiel"]:
        print(f"ATTENTION : {len(diag['absents_referentiel'])} établissement(s) "
              "absent(s) du référentiel, donc absents de la carte :")
        for etab, quartier in diag["absents_referentiel"]:
            print(f"  - {etab} ({quartier})")
    if diag["absents_nouvelles_donnees"]:
        print(f"Info : {len(diag['absents_nouvelles_donnees'])} établissement(s) du "
              "référentiel absents des données de ce mois.")

    if args.strict and diag["absents_referentiel"]:
        print("Mode strict : aucune donnée écrite.")
        return 2

    SORTIE_GEOJSON.write_text(
        json.dumps(replace_nan(geojson), indent=4, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    maj_titre(annee, mois)
    maj_date_template(aujourdhui)
    meta = {
        "mois_donnees": f"{annee}-{mois:02d}",
        "source": source if not args.fichier else Path(source).name,
        "date_traitement": aujourdhui.isoformat(),
        "nb_etablissements_carte": len(geojson["features"]),
        **diag,
    }
    SORTIE_META.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")

    print(f"Écrit : {SORTIE_GEOJSON.relative_to(RACINE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
