"""PASO 2 · Transformar y validar los datos descargados.

Qué hace:
  1. Convierte cada hora de Open-Meteo al formato del contrato V2.
  2. La valida con el MISMO contract.py que usan el portal y las Lambdas.
  3. Toma las primeras 2,000 horas válidas de cada ubicación (10,000 en total).
  4. Guarda las filas rechazadas con su motivo.
  5. Genera el SQL de catálogos para MySQL (paso 3) y catalogo.json.

No usa internet ni AWS. Se puede repetir las veces que quieras.

Ejecutar desde la carpeta climate_portal_v3:
    .\\.venv\\Scripts\\python.exe etl\\paso2_transformar.py
"""
import json
import os
import sys
from collections import Counter
from decimal import Decimal

import config
from comun import (CATALOGO_JSON, CATALOGO_SQL, CRUDOS, LECTURAS, RECHAZADAS, escribir_jsonl,
                   id_estable, load_json, rumbo, sql_numero, sql_texto_mysql)
from contract import dumps, normalize_record

WMO = {codigo: c["condition_id"] for c in config.CONDICIONES for codigo in c["wmo"]}


def convertir(ubicacion, hora, valores):
    """Una hora de Open-Meteo -> registro del contrato V2 (aún sin validar)."""
    faltan = [v for v, x in valores.items() if x is None]
    if faltan:
        raise ValueError("Dato faltante en Open-Meteo: " + ", ".join(faltan))
    codigo = int(valores["weather_code"])
    if codigo not in WMO:
        raise ValueError(f"Código WMO no reconocido: {codigo}")
    registro = {
        "location_id": ubicacion["location_id"],
        "station_id": ubicacion["station_id"],
        "source_id": config.SOURCE_ID,
        "condition_id": WMO[codigo],
        "timestamp_utc": hora + "Z",   # Open-Meteo responde en GMT sin sufijo
        "precipitation_period_minutes": config.PERIODO_PRECIPITACION_MIN,
        "record_id": id_estable("openmeteo", ubicacion["location_id"], hora),
    }
    for variable, campo in config.VARIABLES.items():
        valor = valores[variable]
        if campo == "wind_direction":
            registro[campo] = rumbo(valor)
        elif campo == "visibility_km":
            registro[campo] = Decimal(valor) / 1000
        elif campo != "condition_id":
            registro[campo] = valor
    return normalize_record(registro)


def transformar_ubicacion(ubicacion):
    lid = ubicacion["location_id"]
    ruta = CRUDOS / f"{lid}.json"
    if not ruta.exists():
        raise FileNotFoundError(f"Falta {ruta.name}. Ejecuta primero paso1_descargar.py")
    cuerpo = load_json(ruta.read_text(encoding="utf-8"))
    horas = cuerpo["hourly"]
    n = len(horas["time"])
    columnas = {v: horas.get(v) or [None] * n for v in config.VARIABLES}
    validas, rechazadas = [], []
    for i, hora in enumerate(horas["time"]):
        valores = {v: columnas[v][i] for v in config.VARIABLES}
        try:
            validas.append(convertir(ubicacion, hora, valores))
        except (ValueError, TypeError, ArithmeticError) as error:
            rechazadas.append({"location_id": lid, "timestamp_utc": hora + "Z",
                               "motivo": str(error), "datos": valores})
    validas.sort(key=lambda r: r["timestamp_utc"])
    return validas, rechazadas, cuerpo.get("elevation")


def catalogo(elevaciones):
    ubicaciones = []
    for u in config.UBICACIONES:
        altitud = elevaciones.get(u["location_id"])
        ubicaciones.append({**u, "altitude_m": str(altitud if altitud is not None else 0)})
    return {"fuentes": config.FUENTES, "ubicaciones": ubicaciones, "condiciones": config.CONDICIONES,
            "intervalo_min": config.INTERVALO_ESPERADO_MIN}


def sql_catalogos(cat, modelos):
    s = sql_texto_mysql
    configuracion = {
        config.SOURCE_ID: {"api": config.API_URL, "modelos_usados": sorted(set(modelos.values())),
                           "variables": list(config.VARIABLES), "zona_horaria": "GMT",
                           "periodo": [config.FECHA_INICIO, config.FECHA_FIN],
                           "atribucion": config.API_ATRIBUCION},
        "SRC-MANUAL": {"origen": "portal Flask", "pantalla": "Add record / Import records"},
    }
    lineas = [
        "-- AWS Clima V2 · PASO 3 · Catálogos de MySQL (generado por paso2_transformar.py)",
        "-- Ejecutar en DBeaver sobre la conexión MySQL (base climate_transactional) con Alt+X.",
        "-- Se puede ejecutar más de una vez: si un registro ya existe, no se duplica ni se modifica.",
        f"USE {config.MYSQL_DB};",
        "START TRANSACTION;",
        "",
        "INSERT INTO data_sources (source_id, name, source_type, priority, configuration, secret_reference, is_active) VALUES",
        ",\n".join(f"  ({s(f['source_id'])}, {s(f['name'])}, {s(f['source_type'])}, {f['priority']}, "
                   f"{s(json.dumps(configuracion[f['source_id']], ensure_ascii=False))}, NULL, 1)"
                   for f in cat["fuentes"]),
        "ON DUPLICATE KEY UPDATE source_id = source_id;",
        "",
        "INSERT INTO weather_conditions (condition_id, code, name, description, rule_version, rule_definition, is_active) VALUES",
        ",\n".join(f"  ({s(c['condition_id'])}, {s(c['code'])}, {s(c['name'])}, {s(c['description'])}, "
                   f"{s(config.REGLA_CONDICIONES)}, {s(json.dumps({'fuente': 'WMO weather_code', 'codigos': c['wmo']}))}, 1)"
                   for c in cat["condiciones"]),
        "ON DUPLICATE KEY UPDATE condition_id = condition_id;",
        "",
        "INSERT INTO locations (location_id, name, city, state, country, latitude, longitude, altitude_m,"
        " timezone_name, is_active, version, created_at_utc, updated_at_utc) VALUES",
        ",\n".join(f"  ({s(u['location_id'])}, {s(u['name'])}, {s(u['city'])}, {s(u['state'])}, {s(u['country'])}, "
                   f"{sql_numero(u['latitude'])}, {sql_numero(u['longitude'])}, {sql_numero(u['altitude_m'])}, "
                   f"{s(u['timezone_name'])}, 1, 1, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))"
                   for u in cat["ubicaciones"]),
        "ON DUPLICATE KEY UPDATE location_id = location_id;",
        "",
        "INSERT INTO stations (station_id, location_id, code, name, station_type, expected_interval_minutes,"
        " is_active, created_at_utc) VALUES",
        ",\n".join(f"  ({s(u['station_id'])}, {s(u['location_id'])}, {s(u['station_code'])}, "
                   f"{s('Estación virtual Open-Meteo ' + u['city'])}, 'VIRTUAL', {cat['intervalo_min']}, 1, UTC_TIMESTAMP(6))"
                   for u in cat["ubicaciones"]),
        "ON DUPLICATE KEY UPDATE station_id = station_id;",
        "",
        "COMMIT;",
        "",
        "-- Verificación: debe mostrar 2 fuentes, 8 condiciones, 1 ubicación (CDMX) y 1 estación.",
        "SELECT 'data_sources' AS tabla, COUNT(*) AS filas FROM data_sources",
        "UNION ALL SELECT 'weather_conditions', COUNT(*) FROM weather_conditions",
        "UNION ALL SELECT 'locations', COUNT(*) FROM locations",
        "UNION ALL SELECT 'stations', COUNT(*) FROM stations;",
        "",
    ]
    return "\n".join(lineas)


def main():
    todas, todas_rechazadas, elevaciones, modelos = [], [], {}, {}
    print("Transformando y validando con contract.py...\n")
    for ubicacion in config.UBICACIONES:
        lid = ubicacion["location_id"]
        try:
            validas, rechazadas, elevacion = transformar_ubicacion(ubicacion)
        except FileNotFoundError as error:
            print(error)
            sys.exit(1)
        meta = CRUDOS / f"{lid}.meta.json"
        modelos[lid] = json.loads(meta.read_text(encoding="utf-8"))["modelo"] if meta.exists() else "desconocido"
        elevaciones[lid] = elevacion
        elegidas = validas[-config.HORAS_POR_UBICACION:]   # las horas más recientes
        todas.extend(elegidas)
        todas_rechazadas.extend(rechazadas)
        aviso = "" if len(elegidas) == config.HORAS_POR_UBICACION else "  <-- faltan horas válidas"
        print(f"  {lid:<9} {len(validas) + len(rechazadas)} horas · {len(validas)} válidas · "
              f"{len(rechazadas)} rechazadas · usadas {len(elegidas)}{aviso}")

    escribir_jsonl(LECTURAS, todas)
    escribir_jsonl(RECHAZADAS, todas_rechazadas)
    cat = catalogo(elevaciones)
    CATALOGO_JSON.write_text(dumps(cat), encoding="utf-8")
    CATALOGO_SQL.write_text(sql_catalogos(cat, modelos), encoding="utf-8")

    motivos = Counter(r["motivo"].split(":")[0] for r in todas_rechazadas)
    condiciones = Counter(r["condition_id"] for r in todas)
    print(f"\nRegistros listos para cargar: {len(todas):,}  -> {LECTURAS.relative_to(LECTURAS.parents[2])}")
    print(f"Filas rechazadas:             {len(todas_rechazadas):,}  -> {RECHAZADAS.relative_to(RECHAZADAS.parents[2])}")
    for motivo, n in motivos.most_common(5):
        print(f"    {n:>5} · {motivo}")
    print("Condiciones del clima: " + ", ".join(f"{k} {v}" for k, v in condiciones.most_common()))
    if todas:
        print(f"Periodo: {min(r['timestamp_utc'] for r in todas)} a {max(r['timestamp_utc'] for r in todas)}")
    print(f"\nSQL de catálogos para DBeaver: {CATALOGO_SQL.relative_to(CATALOGO_SQL.parents[2])}")
    if os.environ.get("CLIMA_DATOS_DIR"):
        return   # ejecución semanal: semanal.py sigue con la carga
    if len(todas) < config.HORAS_POR_UBICACION * len(config.UBICACIONES):
        print("\nAviso: hay menos de 10,000 registros. Amplía FECHA_FIN en config.py y repite los pasos 1 y 2.")
    print("Siguiente: paso 3 en DBeaver (catálogos) y luego paso4_cargar_dynamodb.py")


if __name__ == "__main__":
    main()
