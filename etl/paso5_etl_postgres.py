"""PASO 5 · ETL: DynamoDB -> PostgreSQL (climate_analytics) con STORED PROCEDURES.

Python solo EXTRAE y deja los datos en la tabla de staging. La transformación y la
limpieza las hacen los procedimientos almacenados de etl/sql/procedimientos_postgres.sql:

  1. Lee ubicaciones, estaciones, fuentes y condiciones activas de MySQL.
  2. Lee los registros de DynamoDB (ClimateReadingsV2).
  3. CALL sp_iniciar_lote                -> lote RUNNING en etl_batches
  4. CALL sp_sincronizar_series          -> catálogo de MySQL a series
  5. Copia los registros a stg_observations (sin validar)
  6. CALL sp_validar_staging             -> limpieza: rechaza con motivo
  7. CALL sp_clasificar_condiciones      -> condición calculada si la fuente no la trae
  8. CALL sp_cargar_observaciones        -> inserta o actualiza observations
  9. CALL sp_marcar_borrados             -> borrado lógico (solo con --completo)
 10. CALL sp_calcular_agregados_diarios  -> climate_aggregates
 11. CALL sp_evaluar_predicciones        -> evalúa predicciones que ya tienen dato real
 12. CALL sp_cerrar_lote                 -> SUCCEEDED con conteos (o FAILED si algo falla)

Los pasos 4 a 12 ocurren en UNA transacción. Las filas rechazadas se quedan en
stg_observations como cola de registros anómalos.

Requisito: haber instalado etl/sql/procedimientos_postgres.sql (una sola vez, en DBeaver).

Ejecutar desde la carpeta climate_portal_v3 (con las dos RDS encendidas):
    .\\.venv\\Scripts\\python.exe etl\\paso5_etl_postgres.py
"""
import argparse
import csv
import getpass
import json
import sys
from decimal import Decimal, InvalidOperation

import config
from comun import CATALOGO_JSON, DATOS, confirmar, contexto_ssl, leer_jsonl, pedir, sql_texto
from contract import decimal_text, dumps, load_json

S = config.POSTGRES_SCHEMA
PIPELINE = "etl-v2.0-sp"
TAMANO_LOTE = 500
t = sql_texto
NUMERICOS = ["temperature_c", "feels_like_c", "humidity_pct", "pressure_hpa", "wind_speed_kmh",
             "precipitation_mm", "cloud_cover_pct", "visibility_km", "uv_index", "dew_point_c"]
COLUMNAS_STG = ["batch_id", "fila", "location_id", "station_id", "timestamp_utc", "source_record_id",
                "source_version", "content_hash", "last_event_id", "source_id", "condition_id",
                "condition_origin", *NUMERICOS, "wind_direction", "precipitation_period_minutes",
                "received_at_utc", "updated_at_utc"]


# --------------------------------------------------------------------------- destino
class Postgres:
    directo = True

    def __init__(self, conexion):
        self.conexion, self.cursor = conexion, conexion.cursor()

    def ejecutar(self, sql, filas=False):
        self.cursor.execute(sql)
        return self.cursor.fetchall() if filas else []

    def commit(self):
        self.conexion.commit()

    def rollback(self):
        self.conexion.rollback()

    def cerrar(self):
        self.conexion.close()


def endpoint(instancia):
    import boto3
    base = boto3.client("rds", region_name=config.REGION).describe_db_instances(
        DBInstanceIdentifier=instancia)["DBInstances"][0]
    if base["DBInstanceStatus"] != "available":
        print(f"La instancia {instancia} está '{base['DBInstanceStatus']}'. Enciéndela y espera a que diga 'available'.")
        sys.exit(1)
    return base["Endpoint"]["Address"], base["Endpoint"]["Port"]


def conectar_postgres():
    import pg8000.dbapi
    host, puerto = endpoint(config.POSTGRES_INSTANCE)
    usuario = pedir("CLIMA_PG_USER", "Usuario de PostgreSQL (el que usas en DBeaver): ")
    clave = getpass.getpass("Contraseña de PostgreSQL (no se muestra): ")
    conexion = pg8000.dbapi.connect(user=usuario, password=clave, host=host, port=puerto,
                                    database=config.POSTGRES_DB, ssl_context=contexto_ssl(), timeout=60)
    return Postgres(conexion)


def llamar(destino, procedimiento, *argumentos, devuelve=True):
    """CALL a un procedimiento almacenado; devuelve sus parámetros INOUT."""
    filas = destino.ejecutar(f"CALL {S}.{procedimiento}({', '.join(argumentos)})", filas=devuelve)
    return filas[0] if filas else ()


# --------------------------------------------------------------------------- catálogo
def conectar_mysql():
    import pymysql
    host, puerto = endpoint(config.MYSQL_INSTANCE)
    usuario = pedir("CLIMA_MYSQL_USER", "Usuario de MySQL (el que usas en DBeaver): ")
    clave = getpass.getpass("Contraseña de MySQL (no se muestra): ")
    return pymysql.connect(host=host, port=puerto, user=usuario, password=clave, database=config.MYSQL_DB,
                           ssl=contexto_ssl(), connect_timeout=15, charset="utf8mb4", autocommit=False,
                           cursorclass=pymysql.cursors.DictCursor)


def catalogo_mysql():
    conexion = conectar_mysql()
    try:
        with conexion.cursor() as cursor:
            cursor.execute(
                "SELECT l.location_id, l.name AS location_name, l.latitude, l.longitude, l.altitude_m,"
                " l.timezone_name, l.version, s.station_id, s.name AS station_name"
                " FROM locations l JOIN stations s ON s.location_id = l.location_id"
                " WHERE l.is_active = 1 AND s.is_active = 1")
            ubicaciones = cursor.fetchall()
            cursor.execute("SELECT source_id FROM data_sources WHERE is_active = 1")
            fuentes = sorted(r["source_id"] for r in cursor.fetchall())
            cursor.execute("SELECT condition_id FROM weather_conditions WHERE is_active = 1")
            condiciones = sorted(r["condition_id"] for r in cursor.fetchall())
    finally:
        conexion.close()
    return {"ubicaciones": ubicaciones, "fuentes": fuentes, "condiciones": condiciones}


def catalogo_local():
    """Mismo catálogo que generó el paso 2 (si MySQL no está disponible)."""
    cat = load_json(CATALOGO_JSON.read_text(encoding="utf-8"))
    ubicaciones = [dict(location_id=u["location_id"], location_name=u["name"], latitude=u["latitude"],
                        longitude=u["longitude"], altitude_m=u["altitude_m"], timezone_name=u["timezone_name"],
                        version=1, station_id=u["station_id"],
                        station_name="Estación virtual Open-Meteo " + u["city"]) for u in cat["ubicaciones"]]
    return {"ubicaciones": ubicaciones, "fuentes": sorted(f["source_id"] for f in cat["fuentes"]),
            "condiciones": sorted(c["condition_id"] for c in cat["condiciones"])}


# --------------------------------------------------------------------------- origen
def leer_dynamodb(desde):
    import boto3
    from boto3.dynamodb.conditions import Attr
    tabla = boto3.resource("dynamodb", region_name=config.REGION).Table(config.READINGS_TABLE)
    opciones = {"FilterExpression": Attr("updated_at_utc").gte(desde)} if desde else {}
    items = []
    while True:
        respuesta = tabla.scan(**opciones)
        items.extend(respuesta.get("Items", []))
        print(f"  leídos de DynamoDB: {len(items):,}", end="\r")
        if "LastEvaluatedKey" not in respuesta:
            break
        opciones["ExclusiveStartKey"] = respuesta["LastEvaluatedKey"]
    print()
    return items


# --------------------------------------------------------------------------- SQL de apoyo
def numero(valor):
    """Número tal como viene; si no es un número finito, NULL (lo rechazará el SP)."""
    try:
        d = Decimal(str(valor))
    except (InvalidOperation, ValueError, TypeError):
        return "NULL"
    return decimal_text(d) if valor is not None and d.is_finite() else "NULL"


def entero(valor):
    try:
        d = Decimal(str(valor))
        return str(int(d)) if d.is_finite() and d == d.to_integral_value() else "NULL"
    except (InvalidOperation, ValueError, TypeError):
        return "NULL"


def texto(valor):
    return "NULL" if valor is None or str(valor) == "" else t(str(valor))


def sql_staging(items, lote, inicio):
    filas = []
    for k, it in enumerate(items, start=inicio):
        partes = [t(lote), str(k), texto(it.get("location_id")), texto(it.get("station_id")),
                  texto(it.get("timestamp_utc")), texto(it.get("record_id")), entero(it.get("version")),
                  texto(it.get("content_hash")), texto(it.get("transaction_id")), texto(it.get("source_id")),
                  texto(it.get("condition_id")), texto(it.get("condition_origin"))]
        partes += [numero(it.get(c)) for c in NUMERICOS]
        partes += [texto(it.get("wind_direction")), entero(it.get("precipitation_period_minutes")),
                   texto(it.get("created_at_utc")), texto(it.get("updated_at_utc"))]
        filas.append("(" + ", ".join(partes) + ")")
    return f"INSERT INTO {S}.stg_observations ({', '.join(COLUMNAS_STG)}) VALUES\n" + ",\n".join(filas)


def arreglo(valores):
    return "ARRAY[" + ", ".join(t(v) for v in valores) + "]::text[]"


def ultimo_punto(destino):
    filas = destino.ejecutar(
        f"SELECT end_checkpoint->>'max_updated_at_utc' FROM {S}.etl_batches WHERE status = 'SUCCEEDED'"
        " AND end_checkpoint->>'max_updated_at_utc' IS NOT NULL ORDER BY finished_at_utc DESC LIMIT 1", filas=True)
    destino.commit()
    return filas[0][0] if filas else None


def verificar_procedimientos(destino):
    filas = destino.ejecutar(
        "SELECT count(*) FROM information_schema.routines"
        f" WHERE routine_schema = '{S}' AND routine_type = 'PROCEDURE' AND routine_name LIKE 'sp\\_%'", filas=True)
    destino.commit()
    if int(filas[0][0]) < 10:
        print("Faltan los procedimientos almacenados. Ejecuta primero etl/sql/procedimientos_postgres.sql"
              " en DBeaver (conexión PostgreSQL, Alt+X).")
        destino.cerrar()
        sys.exit(1)


# --------------------------------------------------------------------------- principal
def ejecutar_etl(destino, cat, items, modo, desde):
    """Devuelve (lote, resultados, rechazos)."""
    (lote,) = llamar(destino, "sp_iniciar_lote", t(modo), texto(desde), t(PIPELINE), "NULL")
    lote = str(lote)
    destino.commit()
    print(f"4) Lote {lote}")
    try:
        catalogo = [{k: u[k] for k in ("location_id", "station_id", "location_name", "station_name", "latitude",
                                       "longitude", "altitude_m", "timezone_name", "version")}
                    for u in cat["ubicaciones"]]
        (series,) = llamar(destino, "sp_sincronizar_series", t(dumps(catalogo)) + "::jsonb", "NULL")
        for i in range(0, len(items), TAMANO_LOTE):
            destino.ejecutar(sql_staging(items[i:i + TAMANO_LOTE], lote, i + 1))
            print(f"   staging: {min(i + TAMANO_LOTE, len(items)):,}/{len(items):,}", end="\r")
        print()
        validos, rechazados = map(int, llamar(destino, "sp_validar_staging", t(lote), arreglo(cat["fuentes"]),
                                              arreglo(cat["condiciones"]), "NULL", "NULL"))
        (calculadas,) = map(int, llamar(destino, "sp_clasificar_condiciones", t(lote), "false", "NULL"))
        insertados, actualizados, omitidos = map(int, llamar(destino, "sp_cargar_observaciones", t(lote),
                                                             "NULL", "NULL", "NULL"))
        borrados = int(llamar(destino, "sp_marcar_borrados", t(lote), "NULL")[0]) if modo == "completo" else 0
        dias_nuevos, dias_actualizados = map(int, llamar(destino, "sp_calcular_agregados_diarios", "NULL", "NULL"))
        (evaluadas,) = map(int, llamar(destino, "sp_evaluar_predicciones", "NULL", "NULL"))
        rechazos = destino.ejecutar(
            f"SELECT location_id, timestamp_utc, motivo_rechazo FROM {S}.stg_observations"
            f" WHERE batch_id = {t(lote)} AND NOT es_valido ORDER BY fila", filas=True)
        maximo = max((str(it.get("updated_at_utc", "")) for it in items), default="") or desde
        resultados = dict(leidos=len(items), series=int(series), validos=validos, rechazados=rechazados,
                          calculadas=calculadas, insertados=insertados, actualizados=actualizados,
                          omitidos=omitidos, borrados=borrados, dias_nuevos=dias_nuevos,
                          dias_actualizados=dias_actualizados, evaluadas=evaluadas)
        punto = {"modo": modo, "max_updated_at_utc": maximo, **resultados}
        llamar(destino, "sp_cerrar_lote", t(lote), "'SUCCEEDED'", str(len(items)), str(insertados),
               str(actualizados), str(omitidos), str(rechazados), t(json.dumps(punto)) + "::jsonb", "NULL",
               devuelve=False)
        destino.commit()
        return lote, resultados, rechazos
    except Exception as error:
        destino.rollback()
        llamar(destino, "sp_cerrar_lote", t(lote), "'FAILED'", str(len(items)), "0", "0", "0", "0",
               t(json.dumps({"modo": modo})) + "::jsonb", t(f"{type(error).__name__}: {error}"), devuelve=False)
        destino.commit()
        raise


def main():
    parser = argparse.ArgumentParser(description="ETL DynamoDB -> PostgreSQL con stored procedures")
    parser.add_argument("--completo", action="store_true",
                        help="leer toda la tabla y detectar borrados (por defecto: solo cambios desde el último lote)")
    parser.add_argument("--catalogo-local", action="store_true", help="usar datos/catalogo.json en vez de MySQL")
    parser.add_argument("--items-archivo", metavar="JSONL", help=argparse.SUPPRESS)  # pruebas sin AWS
    parser.add_argument("--si", action="store_true", help="no pedir confirmación")
    args = parser.parse_args()

    print("1) Catálogo:", "datos/catalogo.json" if args.catalogo_local else f"MySQL {config.MYSQL_DB}")
    cat = catalogo_local() if args.catalogo_local else catalogo_mysql()
    print(f"   {len(cat['ubicaciones'])} ubicaciones · {len(cat['fuentes'])} fuentes · {len(cat['condiciones'])} condiciones")

    print(f"2) Destino: PostgreSQL {config.POSTGRES_DB}.{S}")
    destino = conectar_postgres()
    verificar_procedimientos(destino)
    desde = None if args.completo else ultimo_punto(destino)
    modo = "incremental" if desde else "completo"
    print(f"   modo {modo}" + (f" (cambios desde {desde})" if desde else ""))

    print("3) Origen: DynamoDB", config.READINGS_TABLE)
    items = leer_jsonl(args.items_archivo) if args.items_archivo else leer_dynamodb(desde)
    print(f"   {len(items):,} registros leídos")
    if not confirmar(f"¿Procesar {len(items):,} registros con los stored procedures (modo {modo})?", args.si):
        print("Cancelado. No se escribió nada.")
        destino.cerrar()
        return

    try:
        lote, r, rechazos = ejecutar_etl(destino, cat, items, modo, desde)
    except Exception as error:
        print(f"\nEl lote falló y se revirtió: {type(error).__name__}: {error}")
        print("Quedó registrado como FAILED en etl_batches.")
        destino.cerrar()
        sys.exit(1)
    destino.cerrar()

    print("\nResultado del lote (calculado por los stored procedures):")
    for clave, nombre in (("leidos", "leídos de DynamoDB"), ("validos", "válidos (sp_validar_staging)"),
                          ("rechazados", "rechazados -> cola de anómalos"),
                          ("calculadas", "condiciones calculadas"), ("insertados", "insertados"),
                          ("actualizados", "actualizados"), ("omitidos", "omitidos (sin cambios)"),
                          ("borrados", "borrados lógicos"), ("evaluadas", "predicciones evaluadas")):
        print(f"  {nombre:<32} {r[clave]:>7,}")
    print(f"  {'promedios diarios':<32} {r['dias_nuevos']:,} nuevos · {r['dias_actualizados']:,} actualizados")
    if rechazos:
        ruta = DATOS / f"etl_rechazados_{lote[:8]}.csv"
        with open(ruta, "w", newline="", encoding="utf-8") as archivo:
            escritor = csv.writer(archivo)
            escritor.writerow(["location_id", "timestamp_utc", "motivo"])
            escritor.writerows(rechazos)
        print(f"Motivos de rechazo: {ruta.relative_to(ruta.parents[2])} (y en la tabla stg_observations)")
    print("Verifica con etl/consultas_verificacion.sql en DBeaver.")


if __name__ == "__main__":
    main()
