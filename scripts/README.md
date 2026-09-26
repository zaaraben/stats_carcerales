# Mise à jour mensuelle des données

Le script `maj_stats_carcerales.py` remplace le notebook
`20260315_densite_carcerale_optimized.ipynb`. Il produit le fichier chargé
par la carte, `stats_carcerales/data/stats_carcerales.json`.

## Source

Le ministère de la Justice publie chaque mois, le dernier jour du mois,
la situation au 1er de ce mois :

```
https://www.justice.gouv.fr/sites/default/files/AAAA-MM/statistique_etablissements_personnes_ecrouees_01MMAAAA.xlsx
```

Le script construit cette adresse lui-même : inutile de passer par la page
annuelle du site. Si le fichier n'est pas dans le dossier `AAAA-MM`, il
essaie le dossier du mois suivant.

## Installation

```bash
pip install -r scripts/requirements.txt
```

## Utilisation

Depuis la racine du dépôt :

```bash
# Cas normal : télécharge les données du mois précédent
python scripts/maj_stats_carcerales.py

# Un mois précis
python scripts/maj_stats_carcerales.py --mois 2026-08

# À partir d'un fichier Excel déjà téléchargé
python scripts/maj_stats_carcerales.py --fichier statistique_..._01082026.xlsx --mois 2026-08
```

Options utiles :

| Option | Effet |
|---|---|
| `--force` | Retraite un mois déjà publié |
| `--strict` | N'écrit rien si un établissement est absent du référentiel |
| `--onglets 14-23` | Onglets à lire, si le ministère change la numérotation |

## Ce que le script modifie

- `stats_carcerales/data/stats_carcerales.json` : les données de la carte
- `stats_carcerales.xml` : le titre (« … au 1er août 2026 »)
- `stats_carcerales/densite_carcerale.mst` : la date « Mise à jour : »
- `stats_carcerales/data/stats_carcerales.meta.json` : le suivi (mois traité,
  source, nombre d'établissements, établissements manquants)

## Contrôles automatiques

Le script s'arrête sans rien modifier si :

- un onglet `Tab14` à `Tab23` manque ;
- une colonne attendue a disparu ou changé de nom ;
- moins de 90 % des établissements du fichier sont retrouvés dans le
  référentiel (signe d'un changement de noms ou de structure) ;
- une coordonnée ou une densité est incohérente.

La ligne d'en-tête est repérée automatiquement : une ligne ajoutée en haut
d'un onglet ne fausse plus la lecture.

## Référentiel

`referentiel/referentiel_etablissements.xlsx` contient la position, l'adresse
et les identifiants de chaque établissement. Le rapprochement avec les
données du ministère se fait sur le couple *établissement + quartier*.

Quand le script signale un établissement absent du référentiel, il faut
l'ajouter dans ce fichier (avec ses coordonnées en EPSG:3857), puis relancer
le script avec `--force`.

## Codes de sortie

| Code | Signification |
|---|---|
| 0 | Mise à jour effectuée, ou déjà à jour |
| 1 | Erreur (structure du fichier, téléchargement…) |
| 2 | Établissements absents du référentiel (avec `--strict`) |
| 3 | Fichier du mois pas encore publié |
