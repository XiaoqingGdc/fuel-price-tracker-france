"""Fuel Price Tracker France – ETL : API prix-carburants → BigQuery."""
import json
import os

import pandas as pd

PROJECT_ID = "fuel-price-tracker-fr"
DATASET = "fuel_prices"

CARBURANTS = ["gazole", "sp95", "sp98", "e10", "e85", "gplc"]
PRIX_COLS = [f"{c}_prix" for c in CARBURANTS]
MAJ_COLS = [f"{c}_maj" for c in CARBURANTS]
RUPTURE_COLS = [f"{c}_rupture_type" for c in CARBURANTS]
COLS = (["id", "pop", "adresse", "cp", "ville", "departement",
         "latitude", "longitude"] + PRIX_COLS + MAJ_COLS + RUPTURE_COLS)

URL = ("https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
       "prix-des-carburants-en-france-flux-instantane-v2/exports/csv"
       "?delimiter=%3B&select=" + ",".join(COLS))

# Seuils des contrôles qualité
NB_STATIONS_MIN = 5000          # ~9 800 stations attendues
PRIX_MIN, PRIX_MAX = 0.5, 3.5   # €/L plausibles
LAT_MIN, LAT_MAX = -22.0, 52.0  # métropole + outre-mer
LON_MIN, LON_MAX = -62.0, 56.0


class DataQualityError(Exception):
    """Levée quand un contrôle qualité échoue : rien n'est chargé dans BigQuery."""


def extract() -> pd.DataFrame:
    """Télécharge le flux instantané."""
    return pd.read_csv(URL, sep=";", encoding="utf-8-sig",
                       dtype={"id": str, "cp": str})


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Nettoie les types : coordonnées et fuseau horaire."""
    df = df.copy()
    df["latitude"] = df["latitude"] / 100000
    df["longitude"] = df["longitude"] / 100000
    # La source annonce UTC (+00:00) mais les heures sont en réalité celles de Paris
    for c in MAJ_COLS:
        df[c] = (pd.to_datetime(df[c], utc=True)
                   .dt.tz_localize(None)
                   .dt.tz_localize("Europe/Paris", ambiguous="NaT",
                                   nonexistent="shift_forward")
                   .dt.tz_convert("UTC"))
    df["date_collecte"] = pd.Timestamp.now(tz="UTC")
    return df


def check_quality(df: pd.DataFrame) -> None:
    """Contrôles qualité avant chargement. Lève DataQualityError en cas d'anomalie."""
    erreurs = []

    if len(df) < NB_STATIONS_MIN:
        erreurs.append(f"Trop peu de stations : {len(df)} < {NB_STATIONS_MIN}")

    if df["id"].duplicated().any():
        erreurs.append(f"{df['id'].duplicated().sum()} identifiants de station en double")

    prix = df[PRIX_COLS].stack()
    # Les prix à 0 sont déjà écartés par transform() ; on contrôle les prix réels
    hors_bornes = prix[(prix > 0) & ((prix < PRIX_MIN) | (prix > PRIX_MAX))]
    if len(hors_bornes):
        erreurs.append(f"{len(hors_bornes)} prix hors bornes [{PRIX_MIN}; {PRIX_MAX}] €/L")

    coords = df[["latitude", "longitude"]].dropna()
    coords_ko = (~coords["latitude"].between(LAT_MIN, LAT_MAX)
                 | ~coords["longitude"].between(LON_MIN, LON_MAX))
    if coords_ko.sum():
        erreurs.append(f"{coords_ko.sum()} stations avec des coordonnées hors France")

    # Une mise à jour ne peut pas être postérieure à la collecte (bug de fuseau horaire)
    maj_max = df[MAJ_COLS].max().max()
    if pd.notna(maj_max) and maj_max > df["date_collecte"].iloc[0]:
        erreurs.append(f"Mise à jour dans le futur : {maj_max} > date de collecte")

    if erreurs:
        raise DataQualityError(" | ".join(erreurs))


def transform(df: pd.DataFrame) -> pd.DataFrame:
    """Agrège les prix moyens par carburant et type de route."""
    long = (df.melt(id_vars=["id", "pop", "date_collecte"], value_vars=PRIX_COLS,
                    var_name="carburant", value_name="prix")
              .dropna(subset=["prix"]))
    long = long[long["prix"] > 0]
    long["carburant"] = long["carburant"].str.replace("_prix", "").str.upper()
    long["type_route"] = long["pop"].map({"A": "Autoroute"}).fillna("Route")
    agg = (long.groupby(["date_collecte", "carburant", "type_route"])
               .agg(prix_moyen=("prix", "mean"), nb_stations=("id", "count"))
               .reset_index()
               .rename(columns={"date_collecte": "horodatage"}))
    agg["prix_moyen"] = agg["prix_moyen"].round(3)
    return agg


def load(df: pd.DataFrame, agg: pd.DataFrame) -> None:
    """Écrit le snapshot (écrasé) et l'historique (ajouté) dans BigQuery."""
    from google.cloud import bigquery
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GCP_SA_KEY"]))
    client = bigquery.Client(project=PROJECT_ID, credentials=creds)

    client.load_table_from_dataframe(
        df, f"{PROJECT_ID}.{DATASET}.instantane",
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE"),
    ).result()
    client.load_table_from_dataframe(
        agg, f"{PROJECT_ID}.{DATASET}.historique",
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


if __name__ == "__main__":
    df = clean(extract())
    check_quality(df)  # stoppe le pipeline avant tout chargement si anomalie
    agg = transform(df)
    load(df, agg)
    print(f"OK : {len(df)} stations, {len(agg)} lignes ajoutées à historique")
