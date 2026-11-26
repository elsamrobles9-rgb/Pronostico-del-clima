"""JSON para la API de la opción A: conserva fechas, horas y decimales al viajar por HTTP.

Solo usa la biblioteca estándar: lo comparten el portal (Vercel o tu PC) y las Lambdas.
"""
import json
from datetime import date, datetime
from decimal import Decimal


def a_json(valor):
    """Convierte a algo que json.dumps entiende, marcando los tipos que JSON no tiene."""
    if isinstance(valor, datetime):
        return {"__dt__": valor.isoformat()}
    if isinstance(valor, date):
        return {"__date__": valor.isoformat()}
    if isinstance(valor, Decimal):
        return {"__dec__": str(valor)}
    if isinstance(valor, dict):
        return {str(k): a_json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple, set)):
        return [a_json(v) for v in valor]
    if isinstance(valor, (bytes, bytearray)):
        return valor.decode("utf-8", "replace")
    return valor


def de_json(valor):
    """Lo contrario de a_json."""
    if isinstance(valor, dict):
        if len(valor) == 1:
            (clave, dato), = valor.items()
            if clave == "__dt__":
                return datetime.fromisoformat(dato)
            if clave == "__date__":
                return date.fromisoformat(dato)
            if clave == "__dec__":
                return Decimal(dato)
        return {k: de_json(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [de_json(v) for v in valor]
    return valor


def codificar(valor):
    return json.dumps(a_json(valor), ensure_ascii=False, separators=(",", ":"))


def decodificar(texto):
    if isinstance(texto, (bytes, bytearray)):
        texto = texto.decode("utf-8")
    return de_json(json.loads(texto))


# ------------------------------------------------------------------ errores que viajan por la API
def error_a_json(exc):
    """Solo se envían errores pensados para el usuario; los demás salen genéricos."""
    nombre = type(exc).__name__
    if nombre == "IdempotencyError":
        return {"tipo": nombre, "mensaje": exc.message, "status": exc.status, "codigo": exc.code}
    if nombre == "ApiError":
        return {"tipo": nombre, "mensaje": str(exc), "status": getattr(exc, "status", 502)}
    if nombre in ("ValueError", "NoAutorizado", "DatabaseUnavailable"):
        return {"tipo": nombre, "mensaje": str(exc)}
    return {"tipo": "ErrorInterno", "mensaje": "Error interno del servicio. Revisa los logs de CloudWatch."}
