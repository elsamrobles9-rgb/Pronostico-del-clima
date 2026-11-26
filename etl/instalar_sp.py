"""Instala (o reinstala) los stored procedures en las dos RDS con un solo comando.

  PostgreSQL climate_analytics:  etl/sql/procedimientos_postgres.sql   (SP 1 a 11, 14 y 15)
  MySQL climate_transactional:   sql/mysql_procedimientos.sql          (SP 12 y 13 + audit_events_history)

Es seguro repetirlo: usa CREATE OR REPLACE, IF NOT EXISTS y DROP PROCEDURE IF EXISTS.
No borra datos. Pide las contraseñas sin mostrarlas.

Ejecutar desde la carpeta del proyecto:
    .\\.venv\\Scripts\\python.exe etl\\instalar_sp.py
Opciones: --solo-postgres, --solo-mysql
"""
import argparse
import sys
from pathlib import Path

import config

ETL = Path(__file__).resolve().parent
ARCHIVO_PG = ETL / "sql" / "procedimientos_postgres.sql"
ARCHIVO_MYSQL = ETL.parent / "sql" / "mysql_procedimientos.sql"


def sentencias_postgres(texto):
    """Divide en sentencias respetando comillas, comentarios y cuerpos $$ ... $$."""
    partes, actual, i, n = [], [], 0, len(texto)
    etiqueta = None
    while i < n:
        c = texto[i]
        if etiqueta:
            if texto.startswith(etiqueta, i):
                actual.append(etiqueta)
                i += len(etiqueta)
                etiqueta = None
                continue
            actual.append(c)
            i += 1
            continue
        if texto.startswith("--", i):
            fin = texto.find("\n", i)
            fin = n if fin < 0 else fin
            i = fin
            continue
        if texto.startswith("/*", i):
            fin = texto.find("*/", i + 2)
            i = n if fin < 0 else fin + 2
            continue
        if c == "'":
            fin = i + 1
            while fin < n:
                if texto[fin] == "'" and not texto.startswith("''", fin):
                    break
                fin += 2 if texto.startswith("''", fin) else 1
            actual.append(texto[i:fin + 1])
            i = fin + 1
            continue
        if c == "$":
            cierre = texto.find("$", i + 1)
            posible = texto[i:cierre + 1] if cierre > 0 else ""
            if posible and (posible == "$$" or posible[1:-1].replace("_", "").isalnum()):
                etiqueta = posible
                actual.append(posible)
                i = cierre + 1
                continue
        if c == ";":
            sentencia = "".join(actual).strip()
            if sentencia:
                partes.append(sentencia)
            actual = []
            i += 1
            continue
        actual.append(c)
        i += 1
    resto = "".join(actual).strip()
    if resto:
        partes.append(resto)
    return partes


def sentencias_mysql(texto):
    """Divide respetando las líneas DELIMITER (igual que el cliente mysql)."""
    partes, actual, delimitador = [], [], ";"
    for linea in texto.splitlines():
        limpia = linea.strip()
        if limpia.upper().startswith("DELIMITER "):
            delimitador = limpia.split(None, 1)[1]
            continue
        if not actual and (not limpia or limpia.startswith("--")):
            continue
        actual.append(linea)
        if limpia.endswith(delimitador):
            sentencia = "\n".join(actual).rstrip()
            sentencia = sentencia[: len(sentencia) - len(delimitador)].strip()
            if sentencia:
                partes.append(sentencia)
            actual = []
    resto = "\n".join(actual).strip()
    if resto:
        partes.append(resto)
    return partes


def instalar_postgres():
    import paso5_etl_postgres as p5
    print("\n== PostgreSQL climate_analytics ==")
    destino = p5.conectar_postgres()
    try:
        sentencias = sentencias_postgres(ARCHIVO_PG.read_text(encoding="utf-8"))
        for sentencia in sentencias:
            destino.cursor.execute(sentencia)
        destino.commit()
        filas = destino.ejecutar(
            "SELECT routine_type, routine_name FROM information_schema.routines"
            f" WHERE routine_schema = '{config.POSTGRES_SCHEMA}' ORDER BY routine_type DESC, routine_name",
            filas=True)
        destino.commit()
    except Exception as error:
        destino.rollback()
        print(f"No se instaló nada en PostgreSQL (se revirtió): {type(error).__name__}: {error}")
        sys.exit(1)
    finally:
        destino.cerrar()
    procedimientos = [nombre for tipo, nombre in filas if tipo == "PROCEDURE"]
    print(f"Instalado. {len(procedimientos)} procedimientos y {len(filas) - len(procedimientos)} funciones:")
    print("  " + ", ".join(procedimientos))
    for nombre in ("sp_upsert_lecturas", "sp_calcular_agregados_horarios", "sp_promedio_movil"):
        if nombre not in procedimientos:
            print(f"AVISO: falta {nombre}.")


def instalar_mysql():
    import paso5_etl_postgres as p5
    print("\n== MySQL climate_transactional ==")
    conexion = p5.conectar_mysql()
    try:
        with conexion.cursor() as cursor:
            for sentencia in sentencias_mysql(ARCHIVO_MYSQL.read_text(encoding="utf-8")):
                cursor.execute(sentencia)
            conexion.commit()
            cursor.execute("SELECT ROUTINE_NAME AS nombre FROM information_schema.ROUTINES"
                           " WHERE ROUTINE_SCHEMA = DATABASE() AND ROUTINE_TYPE = 'PROCEDURE' ORDER BY 1")
            procedimientos = [fila["nombre"] for fila in cursor.fetchall()]
            cursor.execute("SELECT COUNT(*) AS n FROM information_schema.TABLES"
                           " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'audit_events_history'")
            historial = int(cursor.fetchone()["n"])
    except Exception as error:
        conexion.rollback()
        print(f"Error en MySQL: {type(error).__name__}: {error}")
        sys.exit(1)
    finally:
        conexion.close()
    print(f"Instalado. Procedimientos: {', '.join(procedimientos) or '(ninguno)'}")
    print("Tabla audit_events_history: " + ("lista" if historial else "NO EXISTE"))


def main():
    parser = argparse.ArgumentParser(description="Instala los stored procedures en PostgreSQL y MySQL")
    parser.add_argument("--solo-postgres", action="store_true")
    parser.add_argument("--solo-mysql", action="store_true")
    args = parser.parse_args()
    if not args.solo_mysql:
        instalar_postgres()
    if not args.solo_postgres:
        instalar_mysql()
    print("\nListo. Total esperado: 13 SP en PostgreSQL + 2 SP en MySQL = 15.")


if __name__ == "__main__":
    main()
