"""Utilidades compartidas por los pasos del ETL. No hace llamadas de red."""
import os
import ssl
import sys
import uuid
from decimal import Decimal
from pathlib import Path

ETL_DIR = Path(__file__).resolve().parent
PORTAL_DIR = ETL_DIR.parent
# Reutilizamos contract.py y services/ del portal: misma validación que las Lambdas.
if str(PORTAL_DIR) not in sys.path:
    sys.path.insert(0, str(PORTAL_DIR))

from contract import decimal_text, dumps, load_json  # noqa: E402

DATOS = ETL_DIR / "datos"
CRUDOS = DATOS / "crudos"
LECTURAS = DATOS / "lecturas.jsonl"
RECHAZADAS = DATOS / "rechazadas.jsonl"
CATALOGO_JSON = DATOS / "catalogo.json"
CATALOGO_SQL = DATOS / "paso3_catalogos_mysql.sql"
CA_BUNDLE = ETL_DIR / "global-bundle.pem"

# Espacio de nombres fijo: el mismo dato produce siempre el mismo identificador.
NAMESPACE = uuid.UUID("6f1c9a52-3d0e-4c8e-9a51-0c1a2b3c4d5e")

PUNTOS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
          "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def id_estable(*partes):
    """UUID determinista (uuid5) a partir de texto."""
    return str(uuid.uuid5(NAMESPACE, "|".join(str(p) for p in partes)))


def rumbo(grados):
    """Convierte grados (0-360) al punto cardinal de 16 direcciones."""
    valor = Decimal(str(grados)) % 360
    return PUNTOS[int((valor / Decimal("22.5")) + Decimal("0.5")) % 16]


def leer_jsonl(ruta):
    with open(ruta, encoding="utf-8") as archivo:
        return [load_json(linea) for linea in archivo if linea.strip()]


def escribir_jsonl(ruta, filas):
    Path(ruta).parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8", newline="\n") as archivo:
        for fila in filas:
            archivo.write(dumps(fila) + "\n")


def confirmar(pregunta, automatico=False):
    if automatico:
        return True
    respuesta = input(f"{pregunta} (s/n): ").strip().lower()
    return respuesta in ("s", "si", "sí", "y", "yes")


def contexto_ssl():
    """Conexión cifrada a RDS. Verifica el certificado si existe global-bundle.pem."""
    if CA_BUNDLE.exists():
        return ssl.create_default_context(cafile=str(CA_BUNDLE))
    print("Aviso: no se encontró etl/global-bundle.pem. La conexión irá cifrada,\n"
          "       pero sin verificar el certificado del servidor (ver README).")
    contexto = ssl.create_default_context()
    contexto.check_hostname = False
    contexto.verify_mode = ssl.CERT_NONE
    return contexto


def sql_texto(valor):
    """Literal de texto SQL estándar (comillas simples duplicadas)."""
    if valor is None:
        return "NULL"
    return "'" + str(valor).replace("'", "''") + "'"


def sql_texto_mysql(valor):
    """Literal de texto para MySQL, que además trata la barra invertida como escape."""
    if valor is None:
        return "NULL"
    return "'" + str(valor).replace("\\", "\\\\").replace("'", "''") + "'"


def sql_numero(valor):
    if valor is None:
        return "NULL"
    return decimal_text(Decimal(str(valor)))


def pedir(variable, mensaje):
    """Lee un dato de una variable de entorno o lo pregunta en la terminal."""
    return os.environ.get(variable) or input(mensaje).strip()
