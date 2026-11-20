"""Catalog rules shared by the web form, the REST API and file imports."""
import re
from decimal import Decimal, InvalidOperation

AUTO_CONDITION = "AUTO"
RULE_VERSION = "reglas-v1"   # same thresholds as climate_analytics.fn_clasificar_condicion
FIXED_LOCATION = "LOC-CDMX"     # the professor authorised working only with Mexico City
FIXED_STATION = "ST-CDMX-OM"


def classify_condition(precipitation_mm, cloud_cover_pct, visibility_km, humidity_pct, temperature_c):
    """Python twin of the PostgreSQL function fn_clasificar_condicion (rule version reglas-v1)."""
    p, c, v, h, t = (Decimal(str(x)) for x in
                     (precipitation_mm, cloud_cover_pct, visibility_km, humidity_pct, temperature_c))
    if p >= Decimal("0.5") and t <= 0:
        return "COND-SNOW"
    if p >= Decimal("0.5"):
        return "COND-RAIN"
    if p >= Decimal("0.1"):
        return "COND-DRIZZLE"
    if v < 1 and h >= 95:
        return "COND-FOG"
    if c >= 85:
        return "COND-OVERCAST"
    if c >= 25:
        return "COND-PARTLY-CLOUDY"
    return "COND-CLEAR"


def resolve_condition(record):
    """Replace condition_id AUTO with the condition calculated from the measurements."""
    if record.get("condition_id") == AUTO_CONDITION:
        try:
            record["condition_id"] = classify_condition(
                record["precipitation_mm"], record["cloud_cover_pct"], record["visibility_km"],
                record["humidity_pct"], record["temperature_c"])
        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise ValueError("Cannot calculate the condition: check the numeric measurements.") from None
    return record


def fill_station(data, catalog=None):
    """The whole system works with Mexico City only: location and station are fixed."""
    data["location_id"] = data.get("location_id") or FIXED_LOCATION
    data["station_id"] = data.get("station_id") or FIXED_STATION
    if data["location_id"] != FIXED_LOCATION:
        raise ValueError(f"Only {FIXED_LOCATION} (Mexico City) is accepted.")
    return data


def check_record(record, catalog):
    """Location and station are always Mexico City; source and condition are checked against
    the MySQL catalog when it is available (AWS mode)."""
    if record["location_id"] != FIXED_LOCATION:
        raise ValueError(f"Only {FIXED_LOCATION} (Mexico City) is accepted.")
    if record["station_id"] != FIXED_STATION:
        raise ValueError(f"The station for {FIXED_LOCATION} is {FIXED_STATION}.")
    if not catalog:
        return record
    if record["source_id"] not in catalog["source_ids"]:
        raise ValueError(f"Source {record['source_id']} is not in the active catalog.")
    if record["condition_id"] not in catalog["condition_ids"]:
        raise ValueError(f"Condition {record['condition_id']} is not in the active catalog.")
    return record


def build_catalog(locations, sources, conditions):
    return {
        "locations": locations, "sources": sources, "conditions": conditions,
        "by_location": {row["location_id"]: row for row in locations},
        "source_ids": {row["source_id"] for row in sources},
        "condition_ids": {row["condition_id"] for row in conditions},
    }


def public_catalog(catalog):
    if not catalog:
        return {"locations": [], "sources": [], "conditions": [], "managed": False}
    return {"locations": catalog["locations"], "sources": catalog["sources"],
            "conditions": catalog["conditions"], "managed": True}
