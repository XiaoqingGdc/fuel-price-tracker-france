"""Fuel Price Tracker France – ETL : API prix-carburants → BigQuery."""
import json
import os

import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account

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


def extract() -> pd.DataFrame:
    """Télécharge le flux instantané et nettoie les types."""
    df = pd.read_csv(URL, sep=";", encoding="utf-8-sig",
                     dtype={"id": str, "cp": str})
    df["latitude"] = df["latitude"] / 100000
    df["longitude"] = df["longitude"] / 100000
    for c in MAJ_COLS:
        df[c] = pd.to_datetime(df[c], utc=True)
    df["date_collecte"] = pd.Timestamp.now(tz="Europe/Paris").tz_localize(None)
    return df


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
    df = extract()
    agg = transform(df)
    load(df, agg)
    print(f"OK : {len(df)} stations, {len(agg)} lignes ajoutées à historique")
