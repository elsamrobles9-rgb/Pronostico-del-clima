"""RESPALDA y luego BORRA todo lo que no sea la Ciudad de México (LOC-CDMX).

Por qué: el profesor autorizó trabajar solo con CDMX. Este script quita las lecturas de las
otras ciudades (Guadalajara, Monterrey, Mérida, Tijuana) y cualquier registro de prueba con otra
ubicación (por ejemplo LOC-001) de las tres bases:

  DynamoDB  ClimateReadingsV2                 las lecturas
  PostgreSQL climate_analytics                observaciones, promedios, predicciones, métricas,
                                              evaluaciones, staging y la serie de cada ciudad
  MySQL     climate_transactional             estaciones y ubicaciones del catálogo

Pasos:
  1. Cuenta lo que se va a borrar y te lo muestra. Todavía no cambia nada.
  2. Guarda un RESPALDO completo en etl/respaldos/<fecha-hora>/ (archivos .jsonl).
  3. Te pide escribir BORRAR para confirmar.
  4. Borra en PostgreSQL y en MySQL (cada uno en una transacción) y luego en DynamoDB.

Si algo falla a la mitad, vuelve a ejecutarlo: solo borra lo que todavía exista.
Opción --solo-respaldo: genera el respaldo y termina sin borrar nada.

Ejecutar desde la carpeta del proyecto (laboratorio y las dos RDS encendidos):
    .\\.venv\\Scripts\\python.exe etl\\purgar_otras_ciudades.py
"""
import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import config
from comun import ETL_DIR, sql_texto
from contract import dumps

S = config.POSTGRES_SCHEMA
CDMX = config.UBICACION_FIJA
t = sql_texto
# Orden de borrado en PostgreSQL (primero lo que depende de otras tablas).
TABLAS_PG = ["prediction_evaluations", "model_metrics", "predictions", "climate_aggregates", "observations"]


def leer_dynamodb():
    import boto3
    from boto3.dynamodb.conditions import Attr
    tabla = boto3.resource("dynamodb", region_name=config.REGION).Table(config.READINGS_TABLE)
    items, inicio = [], None
    while True:
        argumentos = {"FilterExpression": Attr("location_id").ne(CDMX)}
        if inicio:
            argumentos["ExclusiveStartKey"] = inicio
        respuesta = tabla.scan(**argumentos)
        items.extend(respuesta.get("Items", []))
        inicio = respuesta.get("LastEvaluatedKey")
        if not inicio:
            return tabla, items


def series_a_borrar(destino):
    filas = destino.ejecutar(f"SELECT series_id::text, location_id FROM {S}.series WHERE location_id <> {t(CDMX)}",
                             filas=True)
    return {sid: lid for sid, lid in filas}


def condicion_pg(tabla, ids):
    lista = "ARRAY[" + ", ".join(t(i) for i in ids) + "]::uuid[]"
    if tabla == "stg_observations":
        return f"location_id IS DISTINCT FROM {t(CDMX)} OR series_id = ANY({lista})"
    if tabla == "series":
        return f"series_id = ANY({lista})"
    return f"series_id = ANY({lista})"


def contar_pg(destino, ids):
    conteo = {}
    for tabla in TABLAS_PG + ["stg_observations", "series"]:
        if not ids and tabla != "stg_observations":
            conteo[tabla] = 0
            continue
        donde = condicion_pg(tabla, ids) if ids else f"location_id IS DISTINCT FROM {t(CDMX)}"
        conteo[tabla] = int(destino.ejecutar(f"SELECT count(*) FROM {S}.{tabla} WHERE {donde}", filas=True)[0][0])
    destino.commit()
    return conteo


def contar_mysql(conexion):
    with conexion.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM stations WHERE location_id <> %s", (CDMX,))
        estaciones = int(cursor.fetchone()["n"])
        cursor.execute("SELECT COUNT(*) AS n FROM locations WHERE location_id <> %s", (CDMX,))
        ubicaciones = int(cursor.fetchone()["n"])
        cursor.execute("SELECT COUNT(*) AS n FROM locations WHERE location_id = %s", (CDMX,))
        tiene_cdmx = int(cursor.fetchone()["n"])
    conexion.commit()
    return estaciones, ubicaciones, tiene_cdmx


def respaldar(carpeta, items, destino, ids, conexion):
    carpeta.mkdir(parents=True, exist_ok=True)
    with open(carpeta / f"dynamodb_{config.READINGS_TABLE}.jsonl", "w", encoding="utf-8") as archivo:
        for item in items:
            archivo.write(dumps(item) + "\n")
    for tabla in TABLAS_PG + ["stg_observations", "series"]:
        if not ids and tabla != "stg_observations":
            continue
        donde = condicion_pg(tabla, ids) if ids else f"location_id IS DISTINCT FROM {t(CDMX)}"
        filas = destino.ejecutar(f"SELECT row_to_json(x)::text FROM {S}.{tabla} x WHERE {donde}", filas=True)
        with open(carpeta / f"postgres_{tabla}.jsonl", "w", encoding="utf-8") as archivo:
            for (fila,) in filas:
                archivo.write(fila + "\n")
    destino.commit()
    with conexion.cursor() as cursor:
        for tabla in ("stations", "locations"):
            cursor.execute(f"SELECT * FROM {tabla} WHERE location_id <> %s", (CDMX,))
            with open(carpeta / f"mysql_{tabla}.jsonl", "w", encoding="utf-8") as archivo:
                for fila in cursor.fetchall():
                    archivo.write(json.dumps(fila, default=str, ensure_ascii=False) + "\n")
    conexion.commit()


def borrar_pg(destino, ids):
    borrados = {}
    try:
        for tabla in TABLAS_PG + ["stg_observations", "series"]:
            if not ids and tabla != "stg_observations":
                continue
            donde = condicion_pg(tabla, ids) if ids else f"location_id IS DISTINCT FROM {t(CDMX)}"
            filas = destino.ejecutar(f"WITH d AS (DELETE FROM {S}.{tabla} WHERE {donde} RETURNING 1)"
                                     f" SELECT count(*) FROM d", filas=True)
            borrados[tabla] = int(filas[0][0])
        destino.commit()
    except Exception:
        destino.rollback()
        raise
    return borrados


def borrar_mysql(conexion):
    try:
        with conexion.cursor() as cursor:
            estaciones = cursor.execute("DELETE FROM stations WHERE location_id <> %s", (CDMX,))
            ubicaciones = cursor.execute("DELETE FROM locations WHERE location_id <> %s", (CDMX,))
        conexion.commit()
    except Exception:
        conexion.rollback()
        raise
    return estaciones, ubicaciones


def borrar_dynamodb(tabla, items):
    with tabla.batch_writer() as lote:
        for k, item in enumerate(items, start=1):
            lote.delete_item(Key={"location_id": item["location_id"], "timestamp_utc": item["timestamp_utc"]})
            if k % 500 == 0:
                print(f"   DynamoDB: {k:,}/{len(items):,}", end="\r")
    print(f"   DynamoDB: {len(items):,}/{len(items):,}")
    return len(items)


def main():
    import paso5_etl_postgres as p5
    parser = argparse.ArgumentParser(description="Respalda y borra todo lo que no sea LOC-CDMX")
    parser.add_argument("--solo-respaldo", action="store_true", help="solo guarda el respaldo; no borra nada")
    args = parser.parse_args()

    print(f"1) Buscando en DynamoDB ({config.READINGS_TABLE}) lecturas que no son {CDMX}...")
    tabla_dynamo, items = leer_dynamodb()
    por_ubicacion = Counter(item["location_id"] for item in items)
    print("   " + (", ".join(f"{lid}: {n:,}" for lid, n in sorted(por_ubicacion.items())) or "ninguna"))

    print("\n2) PostgreSQL")
    destino = p5.conectar_postgres()
    series = series_a_borrar(destino)
    conteo_pg = contar_pg(destino, list(series))
    print(f"   series a borrar: {', '.join(sorted(series.values())) or 'ninguna'}")
    for tabla, n in conteo_pg.items():
        print(f"   {tabla:<24} {n:>8,}")

    print("\n3) MySQL")
    conexion = p5.conectar_mysql()
    estaciones, ubicaciones, tiene_cdmx = contar_mysql(conexion)
    print(f"   stations {estaciones} · locations {ubicaciones} (CDMX se queda: {'sí' if tiene_cdmx else 'NO EXISTE'})")
    if not tiene_cdmx:
        print("\nLOC-CDMX no está en el catálogo de MySQL. No se borra nada; revisa el catálogo primero.")
        sys.exit(1)

    total = len(items) + sum(conteo_pg.values()) + estaciones + ubicaciones
    if total == 0:
        print("\nNo hay nada que borrar: el sistema ya tiene solo CDMX.")
        return

    carpeta = ETL_DIR / "respaldos" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    print(f"\n4) Respaldo en {carpeta} ...")
    respaldar(carpeta, items, destino, list(series), conexion)
    print("   Respaldo guardado.")
    if args.solo_respaldo:
        print("\n--solo-respaldo: no se borró nada.")
        return

    print(f"\nSe van a BORRAR {len(items):,} lecturas de DynamoDB, {sum(conteo_pg.values()):,} filas de PostgreSQL"
          f" y {estaciones + ubicaciones} filas del catálogo de MySQL. No se puede deshacer (salvo con el respaldo).")
    if input("Escribe BORRAR para confirmar: ").strip() != "BORRAR":
        print("Cancelado. No se borró nada; el respaldo se queda en la carpeta.")
        return

    print("\n5) Borrando...")
    borrados_pg = borrar_pg(destino, list(series))
    print("   PostgreSQL: " + ", ".join(f"{k} {v:,}" for k, v in borrados_pg.items()))
    estaciones_b, ubicaciones_b = borrar_mysql(conexion)
    print(f"   MySQL: stations {estaciones_b}, locations {ubicaciones_b}")
    borrar_dynamodb(tabla_dynamo, items)
    destino.cerrar()
    conexion.close()

    restantes = len(leer_dynamodb()[1])
    print(f"\nListo. Lecturas que no son {CDMX} en DynamoDB: {restantes}.")
    print("Siguiente: pasos 1, 2 y 4 (13 meses de CDMX) y paso 5 con --completo.")


if __name__ == "__main__":
    main()
