"""Notices for the admin panel: what happened and what to do, in plain English.

Technical errors (from the Lambdas, the databases or validation) arrive as raw messages or field names.
explicar() turns them into a clear message and keeps the original text as the "technical detail".
"""
from datetime import datetime, timedelta, timezone

from flask import flash

MESES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DESFASE_CDMX = timedelta(hours=-6)      # Mexico City has not used daylight saving time since 2022

ETIQUETAS = {
    "location_id": {"nombre": "Location"},
    "station_id": {"nombre": "Station"},
    "timestamp_utc": {"nombre": "Date and time (UTC)"},
    "source_id": {"nombre": "Source"},
    "condition_id": {"nombre": "Condition"},
    "temperature_c": {"nombre": "Temperature", "unidad": "°C", "tipo": "number", "ayuda": "Between −100 and 70."},
    "feels_like_c": {"nombre": "Feels like", "unidad": "°C", "tipo": "number", "ayuda": "Between −120 and 100."},
    "humidity_pct": {"nombre": "Humidity", "unidad": "%", "tipo": "number", "ayuda": "Between 0 and 100."},
    "dew_point_c": {"nombre": "Dew point", "unidad": "°C", "tipo": "number",
                    "ayuda": "Cannot be higher than the temperature."},
    "pressure_hpa": {"nombre": "Pressure", "unidad": "hPa", "tipo": "number", "ayuda": "Between 100 and 1200."},
    "wind_speed_kmh": {"nombre": "Wind speed", "unidad": "km/h", "tipo": "number", "ayuda": "Between 0 and 500."},
    "wind_direction": {"nombre": "Wind direction", "ayuda": "One of the 16 compass points."},
    "precipitation_mm": {"nombre": "Rain", "unidad": "mm", "tipo": "number", "ayuda": "0 or more."},
    "precipitation_period_minutes": {"nombre": "Rain period", "unidad": "min", "tipo": "number",
                                     "ayuda": "Usually 60."},
    "cloud_cover_pct": {"nombre": "Cloud cover", "unidad": "%", "tipo": "number", "ayuda": "Between 0 and 100."},
    "visibility_km": {"nombre": "Visibility", "unidad": "km", "tipo": "number", "ayuda": "Between 0 and 1000."},
    "uv_index": {"nombre": "UV index", "tipo": "number", "ayuda": "0 or more."},
}
RUMBOS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
CONDICIONES = {"COND-CLEAR": "Clear", "COND-PARTLY-CLOUDY": "Partly cloudy", "COND-OVERCAST": "Overcast",
               "COND-FOG": "Fog", "COND-DRIZZLE": "Drizzle", "COND-RAIN": "Rain", "COND-STORM": "Storm",
               "COND-SNOW": "Snow", "AUTO": "Calculate automatically"}
ACCIONES = {"LOGIN": "Sign-in", "LOGOUT": "Sign-out", "READING_CREATE": "Reading created",
            "READING_UPDATE": "Reading edited", "READING_DELETE": "Reading deleted",
            "READING_IMPORT": "Reading imported", "ADMIN_CREATE": "Administrator created",
            "USER_CREATE": "User created", "PASSWORD_CHANGE": "Password changed",
            "PASSWORD_RESET": "Password changed", "USER_DISABLE": "Administrator disabled",
            "USER_ENABLE": "Administrator enabled", "AUDIT_ROTATE": "Audit log archived",
            "ACCESS_DENIED": "Access denied"}
RESULTADOS = {"SUCCESS": "Success", "FAILURE": "Failed", "BLOCKED": "Blocked", "REJECTED": "Rejected",
              "DENIED": "Denied"}
SIN_CONEXION = ("There is no connection to the databases or to AWS. The lab or the RDS databases may be stopped. "
                "Please try again later.")

# (words that appear in the technical error, clear message). The first match wins.
# Some patterns are in Spanish because the Lambdas and the stored procedures answer in Spanish.
REGLAS = [
    (("user with that", "user with this"), "An administrator with that email already exists. To change their "
                                           "password use the same form: it is replaced."),
    (("user not found",), "That administrator does not exist."),
    (("valid email",), "Enter a valid email (for example name@example.com)."),
    (("username must contain",), "The username must be 3 to 80 characters: letters, numbers, _, ., @ or -."),
    (("days must be between",), "Days must be between 0 and 3650."),
    (("iso 8601", "invalid utc offset", "timezone missing"), "The date and time are not valid. Use the ISO format "
                                                             "with a time zone, for example 2026-11-19T00:00:00Z."),
    (("missing fields",), "Some required fields are missing."),
    (("invalid record.",), "The reading has incomplete or invalid data. Check the date and the measurements."),
    (("unknown fields",), "Some fields do not exist in the readings format."),
    (("wind direction",), "The wind direction must be one of the 16 compass points (N, NNE, NE…)."),
    (("precipitation period",), "The rain period must be a whole number between 1 and 1440 minutes."),
    (("cannot calculate the condition",), "The condition could not be calculated: check the measurements."),
    (("lambda execution failed", "cannot access aws lambda", "cannot read the api response",
      "invalid service response", "invalid response from the aws api", "aws api answered with an error"),
     "AWS did not answer as expected. The lab may be stopped or the credentials may "
                                   "have expired. Please try again later."),
    (("version conflict", "reload the record"), "Someone else saved this reading before you. Open it again to see "
                                                "the current version and repeat your change."),
    (("expired or changed",), "The form expired (it lasts 30 minutes) or the reading changed. Open the reading again."),
    (("expected_version is required", "positive integer expected_version"), "That hour already exists and is not "
                                                                             "overwritten. To change it, open it in "
                                                                             "Readings and edit it."),
    (("already exists", "conditional"), "There is already a reading at that date and time. To change it, open it in "
                                         "Readings and edit it."),
    (("only loc-cdmx",), "Only Mexico City readings are accepted (LOC-CDMX)."),
    (("keys cannot be changed",), "The location, station and time of a reading cannot be changed."),
    (("unknown form fields", "duplicate form fields"), "The form had unexpected data. Reload the page and try again."),
    (("invalid form token",), "For security, the form expired. Reload the page and try again."),
    (("not found", "does not exist", "no such record"), "That reading does not exist. Someone may have deleted it."),
    (("dew point", "dew_point"), "The dew point cannot be higher than the temperature."),
    (("do not match",), "The passwords do not match. Type them again."),
    (("current password",), "The current password is not correct."),
    (("own account",), "You cannot disable your own account. Ask another administrator."),
    (("only administrators", "not an admin", "not an administrator"), "That account is not an administrator."),
    (("sesión", "session", "sign in again"), "Your session is no longer valid. Please sign in again."),
    (("cannot connect", "could not connect", "could not reach", "no se pudo contactar", "unavailable", "timed out", "timeout",
      "no se pudo conectar"), SIN_CONEXION),
    (("petición inválida", "parámetros inválidos", "método desconocido", "operación de lectura inválida",
      "demasiado grande"), "The request was not valid. Reload the page and try again."),
    (("de 1 a 500",), "Send 1 to 500 readings at a time."),
    (("falta clima_jwt_secret",), "The ClimateDbV2 Lambda is missing its CLIMA_JWT_SECRET setting."),
    (("import expired",), "The import review expired (it lasts 1 hour). Upload the file again."),
    (("fix all validation errors",), "Some rows have errors. Fix them in your file and upload it again."),
    (("days must be a number", "invalid literal"), "Enter a number of days."),
    (("utf-8", "decode"), "The file is not UTF-8. Save it as UTF-8 and upload it again."),
    (("too large", "256 kb", "request entity"), "The file is too large: the limit is 256 KB."),
    (("1-50", "1 to 50", "between 1 and 50", "at most 50"), "The file must have 1 to 50 readings."),
]


def explicar(texto):
    """(clear message, technical detail or None). Unknown text is shown as is."""
    original = str(texto or "").strip()
    bajo = original.lower()
    for palabras, mensaje in REGLAS:
        if any(p in bajo for p in palabras):
            return mensaje, (original if original and original != mensaje else None)
    if "password" in bajo or "contraseña" in bajo:
        return "The password must be 12 to 128 characters long.", original
    for clave, etiqueta in ETIQUETAS.items():
        if clave in original:
            return f"Check the “{etiqueta['nombre']}” field: it is empty, not valid or out of range.", original
    return (original or "The action could not be completed."), None


def avisar(categoria, mensaje, detalle=None):
    """categoria: success, error, warning or info. The technical detail is shown in small print."""
    flash(f"{mensaje}||{detalle}" if detalle else mensaje, categoria)


def avisar_error(exc, categoria="error"):
    mensaje, detalle = explicar(exc)
    avisar(categoria, mensaje, detalle)


# ------------------------------------------------------------------ filtros de las plantillas
def a_utc(valor):
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    texto = str(valor or "").strip().replace("Z", "+00:00").replace(" ", "T", 1)
    fecha = datetime.fromisoformat(texto)
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def hora_local(valor, parte=None):
    """'2026-11-18T20:00:00Z' -> 'Nov 18, 2026, 14:00' (Mexico City time)."""
    try:
        local = a_utc(valor).astimezone(timezone.utc) + DESFASE_CDMX
    except (TypeError, ValueError):
        return valor if valor not in (None, "") else "—"
    fecha = f"{MESES[local.month - 1]} {local.day}, {local.year}"
    hora = f"{local.hour:02d}:{local.minute:02d}"
    return fecha if parte == "fecha" else hora if parte == "hora" else f"{fecha}, {hora}"


def condicion(codigo):
    return CONDICIONES.get(codigo or "", codigo or "—")


def accion(codigo):
    return ACCIONES.get(codigo or "", codigo or "—")


def resultado(codigo):
    return RESULTADOS.get(codigo or "", codigo or "—")


def explicar_texto(texto):
    mensaje, detalle = explicar(texto)
    return f"{mensaje} ({detalle})" if detalle else mensaje


def resultado_fila(texto):
    """Result of an imported row."""
    texto = str(texto or "")
    if texto == "Imported":
        return "Saved"
    if texto.startswith("Not confirmed:"):
        motivo = texto[len("Not confirmed:"):].replace("Check the record before retrying.", "").strip()
        return "Not saved: " + explicar_texto(motivo)
    return texto


def registrar(app):
    app.jinja_env.filters.update(hora_local=hora_local, condicion=condicion, accion=accion, resultado=resultado,
                                 explicar=explicar_texto, resultado_fila=resultado_fila)
