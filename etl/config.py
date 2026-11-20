"""Configuración del ETL de AWS Clima V2.

Todo lo que podrías querer cambiar (ciudades, fechas, nombres de tablas)
está en este archivo. Los scripts no tienen contraseñas ni claves: las
credenciales de AWS se leen de tu perfil local y las de las bases de datos
se piden al ejecutar, sin mostrarse ni guardarse.
"""

REGION = "us-east-1"

# --- Fuente de datos reales -------------------------------------------------
# Open-Meteo Historical Forecast API: gratuita, sin API key, uso no comercial.
# Licencia de los datos: CC BY 4.0 (hay que citar a Open-Meteo).
API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
API_ATRIBUCION = "Weather data by Open-Meteo.com (CC BY 4.0)"
# Se prueba el modelo predeterminado (None); si le faltan datos (>5 %), se prueba GFS,
# que incluye visibilidad e índice UV.
MODELOS = [None, "gfs_seamless"]
MAX_FALTANTES = 0.05

FECHA_INICIO = "2026-07-01"   # incluida
FECHA_FIN = "2026-09-30"      # incluida (92 días = 2,208 horas por ciudad)
HORAS_POR_UBICACION = 2000    # 5 ubicaciones x 2,000 = 10,000 registros

# Variable de Open-Meteo -> campo del contrato V2 (contract.py)
VARIABLES = {
    "temperature_2m": "temperature_c",
    "apparent_temperature": "feels_like_c",
    "relative_humidity_2m": "humidity_pct",
    "pressure_msl": "pressure_hpa",
    "wind_speed_10m": "wind_speed_kmh",
    "wind_direction_10m": "wind_direction",      # grados -> N, NNE, NE...
    "precipitation": "precipitation_mm",
    "cloud_cover": "cloud_cover_pct",
    "visibility": "visibility_km",                # metros -> km
    "uv_index": "uv_index",
    "dew_point_2m": "dew_point_c",
    "weather_code": "condition_id",               # código WMO -> catálogo
}
PERIODO_PRECIPITACION_MIN = 60   # Open-Meteo: precipitación acumulada de la hora

# --- Catálogos (se cargan en MySQL climate_transactional) -------------------
SOURCE_ID = "SRC-OPENMETEO"

FUENTES = [
    dict(source_id=SOURCE_ID, name="Open-Meteo Historical Forecast API",
         source_type="API", priority=1),
    dict(source_id="SRC-MANUAL", name="Captura manual desde el portal",
         source_type="MANUAL", priority=2),
]

# Una estación virtual por ubicación (station_type VIRTUAL, cada 60 min).
UBICACIONES = [
    dict(location_id="LOC-CDMX", station_id="ST-CDMX-OM", station_code="OM-CDMX",
         name="Ciudad de México - Centro", city="Ciudad de México",
         state="Ciudad de México", country="México",
         latitude="19.4326", longitude="-99.1332", timezone_name="America/Mexico_City"),
    dict(location_id="LOC-GDL", station_id="ST-GDL-OM", station_code="OM-GDL",
         name="Guadalajara - Centro", city="Guadalajara", state="Jalisco", country="México",
         latitude="20.6597", longitude="-103.3496", timezone_name="America/Mexico_City"),
    dict(location_id="LOC-MTY", station_id="ST-MTY-OM", station_code="OM-MTY",
         name="Monterrey - Centro", city="Monterrey", state="Nuevo León", country="México",
         latitude="25.6866", longitude="-100.3161", timezone_name="America/Monterrey"),
    dict(location_id="LOC-MID", station_id="ST-MID-OM", station_code="OM-MID",
         name="Mérida - Centro", city="Mérida", state="Yucatán", country="México",
         latitude="20.9674", longitude="-89.5926", timezone_name="America/Merida"),
    dict(location_id="LOC-TIJ", station_id="ST-TIJ-OM", station_code="OM-TIJ",
         name="Tijuana - Centro", city="Tijuana", state="Baja California", country="México",
         latitude="32.5149", longitude="-117.0382", timezone_name="America/Tijuana"),
]
INTERVALO_ESPERADO_MIN = 60

# Códigos WMO (weather_code de Open-Meteo) agrupados en condiciones del catálogo.
CONDICIONES = [
    dict(condition_id="COND-CLEAR", code="CLEAR", name="Despejado",
         description="Cielo despejado", wmo=[0]),
    dict(condition_id="COND-PARTLY-CLOUDY", code="PARTLY_CLOUDY", name="Parcialmente nublado",
         description="Mayormente despejado o parcialmente nublado", wmo=[1, 2]),
    dict(condition_id="COND-OVERCAST", code="OVERCAST", name="Nublado",
         description="Cielo cubierto", wmo=[3]),
    dict(condition_id="COND-FOG", code="FOG", name="Niebla",
         description="Niebla o niebla con escarcha", wmo=[45, 48]),
    dict(condition_id="COND-DRIZZLE", code="DRIZZLE", name="Llovizna",
         description="Llovizna ligera, moderada, densa o helada", wmo=[51, 53, 55, 56, 57]),
    dict(condition_id="COND-RAIN", code="RAIN", name="Lluvia",
         description="Lluvia o chubascos", wmo=[61, 63, 65, 66, 67, 80, 81, 82]),
    dict(condition_id="COND-SNOW", code="SNOW", name="Nieve",
         description="Nevada o chubascos de nieve", wmo=[71, 73, 75, 77, 85, 86]),
    dict(condition_id="COND-STORM", code="STORM", name="Tormenta",
         description="Tormenta eléctrica, con o sin granizo", wmo=[95, 96, 99]),
]
REGLA_CONDICIONES = "wmo-4677-v1"

# --- AWS -------------------------------------------------------------------
READINGS_TABLE = "ClimateReadingsV2"
LAMBDA_CREATE = "ClimateCreateReadingV2"
MYSQL_INSTANCE = "climate-mysql"
MYSQL_DB = "climate_transactional"
POSTGRES_INSTANCE = "climate-postgres"
POSTGRES_DB = "climate_analytics"
POSTGRES_SCHEMA = "climate_analytics"
PIPELINE_VERSION = "etl-v1.0"
