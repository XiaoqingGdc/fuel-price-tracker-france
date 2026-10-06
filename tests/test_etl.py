"""Tests unitaires du pipeline ETL – exécutés sans réseau ni BigQuery."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import etl  # noqa: E402


def make_raw(n=3, **overrides) -> pd.DataFrame:
    """Construit un petit flux brut au format de la source."""
    data = {
        "id": [f"4400{i}001" for i in range(n)],
        "pop": ["R"] * (n - 1) + ["A"],
        "adresse": ["rue test"] * n,
        "cp": ["44000"] * n,
        "ville": ["Nantes"] * n,
        "departement": ["Loire-Atlantique"] * n,
        "latitude": [4721800.0] * n,     # 47.218 en 1e-5 degré
        "longitude": [-155300.0] * n,    # -1.553
    }
    for c in etl.CARBURANTS:
        data[f"{c}_prix"] = [1.80] * n
        data[f"{c}_maj"] = ["2026-10-01T10:00:00+00:00"] * n
        data[f"{c}_rupture_type"] = [None] * n
    data.update(overrides)
    return pd.DataFrame(data)


# ---------- clean() ----------

def test_clean_convertit_les_coordonnees():
    df = etl.clean(make_raw())
    assert df["latitude"].iloc[0] == pytest.approx(47.218)
    assert df["longitude"].iloc[0] == pytest.approx(-1.553)


def test_clean_corrige_le_fuseau_horaire_ete():
    # "10:00+00:00" est en réalité 10:00 heure de Paris → 08:00 UTC en été (UTC+2)
    df = etl.clean(make_raw())
    assert df["gazole_maj"].iloc[0] == pd.Timestamp("2026-10-01 08:00", tz="UTC")


def test_clean_corrige_le_fuseau_horaire_hiver():
    raw = make_raw(gazole_maj=["2026-01-15T10:00:00+00:00"] * 3)
    df = etl.clean(raw)
    assert df["gazole_maj"].iloc[0] == pd.Timestamp("2026-01-15 09:00", tz="UTC")


def test_clean_ne_modifie_pas_le_dataframe_source():
    raw = make_raw()
    etl.clean(raw)
    assert raw["latitude"].iloc[0] == 4721800.0


# ---------- transform() ----------

def test_transform_ignore_prix_vides_et_nuls():
    raw = make_raw(gazole_prix=[1.80, np.nan, 0.0])
    agg = etl.transform(etl.clean(raw))
    gazole = agg[agg["carburant"] == "GAZOLE"]
    assert gazole["nb_stations"].sum() == 1


def test_transform_distingue_route_et_autoroute():
    raw = make_raw(gazole_prix=[1.70, 1.80, 1.90])  # la 3e station est sur autoroute
    agg = etl.transform(etl.clean(raw))
    gazole = agg[agg["carburant"] == "GAZOLE"].set_index("type_route")
    assert gazole.loc["Route", "prix_moyen"] == pytest.approx(1.75)
    assert gazole.loc["Autoroute", "prix_moyen"] == pytest.approx(1.90)


def test_transform_noms_de_carburant_en_majuscules():
    agg = etl.transform(etl.clean(make_raw()))
    assert set(agg["carburant"]) == {c.upper() for c in etl.CARBURANTS}


# ---------- check_quality() ----------

@pytest.fixture
def seuil_bas(monkeypatch):
    """Abaisse le nombre minimal de stations pour tester sur 3 lignes."""
    monkeypatch.setattr(etl, "NB_STATIONS_MIN", 1)


def test_quality_ok_sur_donnees_valides(seuil_bas):
    etl.check_quality(etl.clean(make_raw()))  # ne doit pas lever d'erreur


def test_quality_trop_peu_de_stations():
    with pytest.raises(etl.DataQualityError, match="Trop peu de stations"):
        etl.check_quality(etl.clean(make_raw()))


def test_quality_detecte_les_doublons(seuil_bas):
    raw = make_raw(id=["X", "X", "Y"])
    with pytest.raises(etl.DataQualityError, match="double"):
        etl.check_quality(etl.clean(raw))


def test_quality_detecte_prix_aberrant(seuil_bas):
    raw = make_raw(gazole_prix=[1.80, 18.0, 1.80])  # erreur de virgule
    with pytest.raises(etl.DataQualityError, match="hors bornes"):
        etl.check_quality(etl.clean(raw))


def test_quality_detecte_coordonnees_hors_france(seuil_bas):
    raw = make_raw(latitude=[4721800.0, 6500000.0, 4721800.0])  # 65°N
    with pytest.raises(etl.DataQualityError, match="coordonnées"):
        etl.check_quality(etl.clean(raw))


def test_quality_detecte_mise_a_jour_dans_le_futur(seuil_bas):
    raw = make_raw(gazole_maj=["2099-01-01T10:00:00+00:00"] * 3)
    with pytest.raises(etl.DataQualityError, match="futur"):
        etl.check_quality(etl.clean(raw))
