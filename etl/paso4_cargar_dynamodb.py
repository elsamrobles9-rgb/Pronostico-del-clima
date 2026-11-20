"""PASO 4 · Cargar los registros validados en DynamoDB (ClimateReadingsV2).

Qué hace: envía cada registro de etl/datos/lecturas.jsonl usando el MISMO
service.py que ejecutan tus Lambdas (ReadingService), así que se aplican las
mismas reglas: validación, versión 1, content_hash, fechas de auditoría y
escritura condicional (nunca sobrescribe un registro existente).

Es seguro repetirlo: si un registro ya existe con los mismos datos, se cuenta
como "ya existía" y no se modifica.

Modos:
  --via directo  (por defecto) escribe en DynamoDB con service.py desde tu PC.
  --via lambda   invoca la Lambda ClimateCreateReadingV2 por cada registro.

Ejecutar desde la carpeta climate_portal_v3 (primero una prueba con 20):
    .\\.venv\\Scripts\\python.exe etl\\paso4_cargar_dynamodb.py --limite 20
    .\\.venv\\Scripts\\python.exe etl\\paso4_cargar_dynamodb.py
"""
import argparse
import csv
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from comun import DATOS, LECTURAS, confirmar, leer_jsonl
from contract import dumps, load_json

RESULTADOS = DATOS / "resultado_carga.csv"
_local = threading.local()


def clasificar(estado, mensaje):
    if estado == 201:
        return "creado"
    if estado == 409 and "same information" in mensaje:
        return "ya_existia"
    if estado in (400, 404, 409, 413):
        return "rechazado"
    return "error"


def enviar_directo(registro):
    if not hasattr(_local, "servicio"):
        import boto3
        from botocore.config import Config
        from services.reading_service import ReadingService
        recurso = boto3.resource("dynamodb", region_name=config.REGION,
                                 config=Config(retries={"mode": "standard", "max_attempts": 8}))
        _local.servicio = ReadingService(recurso.Table(config.READINGS_TABLE))
    respuesta = _local.servicio.handle("create", {"body": dumps(registro)})
    return respuesta["statusCode"], load_json(respuesta["body"]).get("message", "")


def enviar_lambda(registro):
    if not hasattr(_local, "cliente"):
        import boto3
        from botocore.config import Config
        _local.cliente = boto3.client("lambda", region_name=config.REGION,
                                      config=Config(read_timeout=30, retries={"mode": "standard", "max_attempts": 8}))
    resultado = _local.cliente.invoke(FunctionName=config.LAMBDA_CREATE, InvocationType="RequestResponse",
                                      Payload=dumps({"body": dumps(registro)}).encode("utf-8"))
    carga = load_json(resultado["Payload"].read())
    if resultado.get("FunctionError"):
        return 500, "La Lambda falló: " + str(carga.get("errorMessage", ""))[:200]
    return int(carga["statusCode"]), load_json(carga["body"]).get("message", "")


def verificar_aws():
    """Comprobaciones de solo lectura antes de escribir."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        identidad = boto3.client("sts", region_name=config.REGION).get_caller_identity()["Arn"]
        tabla = boto3.client("dynamodb", region_name=config.REGION).describe_table(
            TableName=config.READINGS_TABLE)["Table"]
    except (BotoCoreError, ClientError) as error:
        print("No se pudo acceder a AWS:", error)
        print("Revisa que tu sesión del laboratorio esté activa y que copiaste credenciales nuevas en ~/.aws/credentials.")
        sys.exit(1)
    return identidad, tabla


def main():
    parser = argparse.ArgumentParser(description="Carga por lotes en ClimateReadingsV2")
    parser.add_argument("--via", choices=("directo", "lambda"), default="directo")
    parser.add_argument("--hilos", type=int, default=8, help="envíos en paralelo (1-16)")
    parser.add_argument("--limite", type=int, default=0, help="cargar solo los primeros N (prueba)")
    parser.add_argument("--si", action="store_true", help="no pedir confirmación")
    args = parser.parse_args()

    if not LECTURAS.exists():
        print("Falta etl/datos/lecturas.jsonl. Ejecuta primero paso2_transformar.py")
        sys.exit(1)
    registros = leer_jsonl(LECTURAS)
    if args.limite:
        registros = registros[:args.limite]
    hilos = max(1, min(args.hilos, 16))

    identidad, tabla = verificar_aws()
    print(f"Cuenta/rol: {identidad}")
    print(f"Tabla: {tabla['TableName']} ({tabla['TableStatus']}), ~{tabla.get('ItemCount', 0)} registros antes de cargar")
    print(f"Se enviarán {len(registros):,} registros vía {args.via} con {hilos} hilos.")
    if not confirmar("¿Continuar con la escritura en DynamoDB?", args.si):
        print("Cancelado. No se escribió nada.")
        return

    enviar = enviar_directo if args.via == "directo" else enviar_lambda
    conteo, filas = Counter(), []
    inicio = time.time()
    with ThreadPoolExecutor(max_workers=hilos) as pool:
        futuros = {pool.submit(enviar, r): r for r in registros}
        for i, futuro in enumerate(as_completed(futuros), 1):
            r = futuros[futuro]
            try:
                estado, mensaje = futuro.result()
            except Exception as error:  # red, credenciales vencidas, etc.
                estado, mensaje = 0, f"{type(error).__name__}: {error}"[:300]
            resultado = clasificar(estado, mensaje)
            conteo[resultado] += 1
            filas.append([r["location_id"], r["timestamp_utc"], resultado, estado, mensaje])
            if i % 500 == 0 or i == len(registros):
                print(f"  {i:>6,}/{len(registros):,} · {dict(conteo)} · {time.time() - inicio:.0f} s")

    DATOS.mkdir(exist_ok=True)
    with open(RESULTADOS, "w", newline="", encoding="utf-8") as archivo:
        escritor = csv.writer(archivo)
        escritor.writerow(["location_id", "timestamp_utc", "resultado", "estado_http", "mensaje"])
        escritor.writerows(sorted(filas))

    print(f"\nTerminado en {time.time() - inicio:.0f} s")
    for clave in ("creado", "ya_existia", "rechazado", "error"):
        print(f"  {clave:<11} {conteo[clave]:>6,}")
    print(f"Detalle por registro: {RESULTADOS.relative_to(RESULTADOS.parents[2])}")
    if conteo["error"]:
        print("\nHubo errores (ver el CSV). Si dicen ExpiredToken, renueva credenciales y vuelve a ejecutar:"
              " los registros ya cargados se cuentan como 'ya_existia'.")
    print("Siguiente: paso5_etl_postgres.py")


if __name__ == "__main__":
    main()
