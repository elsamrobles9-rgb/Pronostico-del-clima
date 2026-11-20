"""INGESTA Y REENTRENAMIENTO SEMANAL (un solo comando).

Qué hace, en orden:
  1. Descarga de Open-Meteo la última semana completa (7 días hasta ayer) de la CDMX.
  2. Transforma y valida con contract.py (las mismas reglas que las Lambdas).
  3. Carga en DynamoDB ClimateReadingsV2 (los duplicados se rechazan, no se repiten).
  4. ETL incremental a PostgreSQL con los 10 stored procedures (solo los cambios nuevos).
  5. Reentrena y evalúa la predicción (MAE y RMSE) con toda la historia, incluida la semana nueva.

Los archivos de cada semana van a etl/datos_semanal/AAAA-MM-DD/; los 10,000 registros
originales de etl/datos/ no se tocan.

Ejecutar desde la carpeta climate_portal_v3, con el laboratorio y las dos RDS encendidas:
    .\\.venv\\Scripts\\python.exe etl\\semanal.py
Opciones:
    --hasta 2026-11-18     último día a descargar (por defecto: ayer en UTC)
    --dias 7               cuántos días (por defecto 7)
    --sin-prediccion       solo ingesta + ETL
    --si                   no pedir confirmaciones

Learner Lab: las credenciales de AWS caducan cada 4 horas y el laboratorio se apaga, así que
la ejecución semanal se lanza a mano (o con el Programador de tareas mientras el lab está
encendido). En producción se programaría con Amazon EventBridge Scheduler, por ejemplo
cron(0 6 ? * MON *), lanzando estos mismos pasos en una Lambda o tarea de ECS y leyendo
las contraseñas de AWS Secrets Manager.
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ETL = Path(__file__).resolve().parent


def paso(numero, titulo, script, *argumentos, entorno):
    print(f"\n{'=' * 70}\n{numero}. {titulo}\n{'=' * 70}", flush=True)
    inicio = time.time()
    resultado = subprocess.run([sys.executable, str(ETL / script), *argumentos], env=entorno, cwd=ETL.parent)
    if resultado.returncode != 0:
        print(f"\nEl paso {numero} terminó con error (código {resultado.returncode}). Se detiene la ejecución semanal.")
        print("Corrige el problema y vuelve a ejecutar semanal.py: los pasos son seguros de repetir.")
        sys.exit(resultado.returncode)
    print(f"   ({time.time() - inicio:.0f} s)")


def main():
    parser = argparse.ArgumentParser(description="Ingesta semanal + ETL + reentrenamiento")
    parser.add_argument("--hasta", help="último día (AAAA-MM-DD); por defecto ayer")
    parser.add_argument("--dias", type=int, default=7)
    parser.add_argument("--sin-prediccion", action="store_true")
    parser.add_argument("--si", action="store_true", help="no pedir confirmaciones")
    args = parser.parse_args()

    hoy = datetime.now(timezone.utc).date()
    fin = date.fromisoformat(args.hasta) if args.hasta else hoy - timedelta(days=1)
    if fin >= hoy:
        sys.exit("--hasta debe ser un día que ya terminó (anterior a hoy en UTC).")
    if not 1 <= args.dias <= 31:
        sys.exit("--dias debe estar entre 1 y 31.")
    inicio = fin - timedelta(days=args.dias - 1)
    carpeta = ETL / "datos_semanal" / fin.isoformat()

    entorno = dict(os.environ, CLIMA_FECHA_INICIO=inicio.isoformat(), CLIMA_FECHA_FIN=fin.isoformat(),
                   CLIMA_HORAS_POR_UBICACION=str(24 * args.dias), CLIMA_DATOS_DIR=str(carpeta),
                   PYTHONIOENCODING="utf-8")
    si = ["--si"] if args.si else []
    print(f"Ejecución semanal: {inicio} a {fin} ({args.dias} días, hasta {24 * args.dias:,} lecturas de la CDMX)")
    print(f"Archivos de esta semana: {carpeta}")

    paso(1, "Descargar la semana de Open-Meteo", "paso1_descargar.py", entorno=entorno)
    paso(2, "Transformar y validar", "paso2_transformar.py", entorno=entorno)
    paso(3, "Cargar en DynamoDB (ClimateReadingsV2)", "paso4_cargar_dynamodb.py", *si, entorno=entorno)
    paso(4, "ETL incremental a PostgreSQL (10 stored procedures)", "paso5_etl_postgres.py", *si, entorno=entorno)
    if not args.sin_prediccion:
        paso(5, "Reentrenar y evaluar la predicción (MAE / RMSE)", "paso6_predecir.py", *si, entorno=entorno)
    print("\nListo. Revisa en DBeaver: etl_batches (lote nuevo), forecast_runs (corrida nueva) y model_metrics.")


if __name__ == "__main__":
    main()
