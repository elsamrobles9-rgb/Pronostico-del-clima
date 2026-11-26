"""Capa analítica para el portal: página pública del clima y sincronización inmediata.

- La página pública (visitante, sin sesión) muestra la última lectura real de la CDMX y el
  pronóstico de 7 días con desglose por hora. El pronóstico se calcula al momento de la
  consulta con el mismo modelo que etl/paso6_predecir.py (prediccion/modelo.py), a partir de
  las lecturas de PostgreSQL; así "hoy" siempre empieza en la hora actual.
- Cuando un administrador crea, edita o importa lecturas, se llama al SP
  climate_analytics.sp_upsert_lecturas para que PostgreSQL las tenga sin esperar al ETL.

Modo demo: usa la historia sintética de prediccion/demo.py (sin AWS).
Modo demo con --datos-reales: usa las lecturas reales de etl/datos/lecturas.jsonl (sin AWS ni internet).
"""
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from contract import dumps, load_json
from prediccion import demo
from prediccion import modelo as M
from services.catalog import FIXED_LOCATION
from services.mysql_store import Pool, ssl_context

CACHE_SEGUNDOS = 600
DIAS_SEMANA = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
MESES_LARGOS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
                "octubre", "noviembre", "diciembre")
VIENTO = {"N": "Norte", "NNE": "Nornoreste", "NE": "Noreste", "ENE": "Estenoreste", "E": "Este",
          "ESE": "Estesureste", "SE": "Sureste", "SSE": "Sursureste", "S": "Sur", "SSW": "Sursuroeste",
          "SW": "Suroeste", "WSW": "Oestesuroeste", "W": "Oeste", "WNW": "Oestenoroeste", "NW": "Noroeste",
          "NNW": "Nornoroeste"}
RUMBOS = list(VIENTO)


def nivel_uv(uv):
    uv = float(uv or 0)
    if uv < 3:
        return "Bajo", "no"
    if uv < 6:
        return "Moderado", "15-25"
    if uv < 8:
        return "Alto", "30+"
    if uv < 11:
        return "Muy alto", "50+"
    return "Extremo", "50+"


def nombre_dia(dia, hoy):
    if dia == hoy:
        return "Hoy"
    if dia == hoy + timedelta(days=1):
        return "Mañana"
    return DIAS_SEMANA[dia.weekday()].capitalize()


def fecha_corta(dia):
    return f"{dia.day} {MESES[dia.month - 1]}"


def fecha_larga(dia):
    return f"{DIAS_SEMANA[dia.weekday()]} {dia.day} de {MESES_LARGOS[dia.month - 1]}"


class BaseAnalytics:
    kind = "base"

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = None   # (instante, clave, Modelo)

    def historial(self):
        raise NotImplementedError

    def ultima_lectura(self):
        raise NotImplementedError

    def upsert(self, records, origen, fuentes, condiciones):
        return None

    def invalidar(self):
        with self.lock:
            self.cache = None

    def modelo(self):
        with self.lock:
            if self.cache and time.time() - self.cache[0] < CACHE_SEGUNDOS:
                return self.cache[2]
        horas = self.historial()
        clave = (len(horas), horas[-1]["ts_utc"] if horas else None)
        with self.lock:
            if self.cache and self.cache[1] == clave:
                self.cache = (time.time(), clave, self.cache[2])
                return self.cache[2]
        nuevo = M.Modelo(horas)
        with self.lock:
            self.cache = (time.time(), clave, nuevo)
        return nuevo

    def pagina(self, dia_elegido=None, ahora_local=None):
        """Todo lo que muestra la página pública."""
        ahora = ahora_local or M.hoy_local()
        hoy = ahora.date()
        modelo = self.modelo()
        dias = modelo.pronostico(desde_dia=hoy, dias=7)
        disponibles = [d["dia"] for d in dias]
        seleccionado = dia_elegido if dia_elegido in disponibles else disponibles[0]
        tarjetas = []
        for d in dias:
            icono, texto = M.condicion_pronostico(d["rain_probability_pct"], d["precipitation_total_mm"],
                                                  d["humidity_mean_pct"])
            tarjetas.append(dict(
                dia=d["dia"], nombre=nombre_dia(d["dia"], hoy), fecha=fecha_corta(d["dia"]), icono=icono,
                texto=texto, maxima=round(d["temperature_max_c"]), minima=round(d["temperature_min_c"]),
                prob=round(d["rain_probability_pct"]), mm=round(d["precipitation_total_mm"], 1),
                humedad=round(d["humidity_mean_pct"]), elegido=d["dia"] == seleccionado))
        elegido = next(d for d in dias if d["dia"] == seleccionado)
        horas = []
        for h in elegido["horas"]:
            if seleccionado == hoy and h["local"].hour < ahora.hour:
                continue
            icono, texto = M.condicion_pronostico(h["rain_probability_pct"], h["precipitation_mm"],
                                                  h["humidity_pct"], por_hora=True)
            horas.append(dict(hora=f"{h['local'].hour:02d}:00", icono=icono, texto=texto,
                              dia_luz=M.es_de_dia(h["local"].hour), temperatura=round(h["temperature_c"]),
                              prob=round(h["rain_probability_pct"]), mm=round(h["precipitation_mm"], 1),
                              humedad=round(h["humidity_pct"])))
        return dict(actual=self.actual(), dias=tarjetas, horas=horas, seleccionado=seleccionado,
                    titulo_dia=fecha_larga(seleccionado), hoy=hoy, phi=modelo.phi,
                    historia=dict(dias=len(modelo.fechas), desde=modelo.fechas[0], hasta=modelo.fechas[-1]))

    # ------------------------------------------------------------------ registros públicos (visitante)
    def rango_dias(self):
        """(primer día, último día) locales con lecturas, o (None, None)."""
        raise NotImplementedError

    def horas_de(self, dia):
        """Lecturas (formato del modelo) de un día local de la CDMX, en orden."""
        raise NotImplementedError

    def lecturas_dia(self, dia=None):
        """Lo que muestra /registros: las lecturas por hora de un día (por omisión, el último con datos)."""
        primero, ultimo = self.rango_dias()
        if primero is None:
            return dict(dia=dia, filas=[], primero=None, ultimo=None)
        dia = dia or ultimo
        filas = [fila_publica(h) for h in self.horas_de(dia)] if primero <= dia <= ultimo else []
        return dict(dia=dia, filas=filas, primero=primero, ultimo=ultimo)

    def actual(self):
        lectura = self.ultima_lectura()
        if not lectura:
            return None
        local = M.a_local(lectura["ts_utc"])
        icono, texto = M.condicion_lectura(lectura.get("condition_id"))
        uv_texto, fps = nivel_uv(lectura.get("uv_index"))
        rumbo = lectura.get("wind_direction") or "N"
        return dict(
            hora=f"{local.day} {MESES[local.month - 1]} {local.year}, {local.hour:02d}:{local.minute:02d}",
            icono=icono, texto=texto, dia_luz=M.es_de_dia(local.hour),
            temperatura=round(float(lectura["temperature_c"])), sensacion=round(float(lectura["feels_like_c"])),
            humedad=round(float(lectura["humidity_pct"])), viento=round(float(lectura["wind_speed_kmh"])),
            rumbo=VIENTO.get(rumbo, rumbo), giro=(RUMBOS.index(rumbo) * 22.5 + 180) % 360 if rumbo in RUMBOS else 0,
            uv=round(float(lectura.get("uv_index") or 0), 1), uv_texto=uv_texto, fps=fps,
            lluvia=round(float(lectura.get("precipitation_mm") or 0), 1))


CAMPOS_HORA = ("temperature_c", "feels_like_c", "humidity_pct", "precipitation_mm", "wind_speed_kmh",
               "wind_direction", "uv_index", "cloud_cover_pct", "condition_id")


def fila_publica(h):
    """Una lectura (formato del modelo) -> fila de la página pública de registros (solo lectura)."""
    local = M.a_local(h["ts_utc"])
    icono, texto = M.condicion_lectura(h.get("condition_id"))

    def numero(valor, decimales=1):
        return None if valor is None else round(float(valor), decimales)

    def entero(valor):
        return None if valor is None else int(round(float(valor)))
    return dict(hora=f"{local.hour:02d}:{local.minute:02d}", icono=icono, texto=texto, dia_luz=M.es_de_dia(local.hour),
                temperatura=numero(h.get("temperature_c")), sensacion=numero(h.get("feels_like_c")),
                humedad=entero(h.get("humidity_pct")), lluvia=numero(h.get("precipitation_mm")),
                viento=entero(h.get("wind_speed_kmh")), rumbo=h.get("wind_direction") or "",
                uv=numero(h.get("uv_index")), nubes=entero(h.get("cloud_cover_pct")))


def hora_desde_lectura(lectura):
    """Lectura del contrato V2 (como en DynamoDB o en lecturas.jsonl) -> fila que usa el modelo."""
    texto = str(lectura["timestamp_utc"]).replace("Z", "+00:00")
    fila = {"ts_utc": datetime.fromisoformat(texto).astimezone(timezone.utc)}
    for campo in CAMPOS_HORA:
        valor = lectura.get(campo)
        fila[campo] = float(valor) if isinstance(valor, (Decimal, int)) and not isinstance(valor, bool) else valor
    return fila


def leer_lecturas(ruta, ubicacion=FIXED_LOCATION):
    """Lee etl/datos/lecturas.jsonl (lo generó el paso 2) y deja solo las lecturas de la CDMX."""
    lecturas = []
    with open(ruta, encoding="utf-8") as archivo:
        for linea in archivo:
            if linea.strip():
                lectura = load_json(linea)
                if isinstance(lectura, dict) and lectura.get("location_id") == ubicacion:
                    lecturas.append(lectura)
    if not lecturas:
        raise ValueError(f"{ruta} no tiene lecturas de {ubicacion}.")
    lecturas.sort(key=lambda r: hora_desde_lectura(r)["ts_utc"])
    return lecturas


class DemoAnalytics(BaseAnalytics):
    kind = "demo"

    def __init__(self, horas=None, datos_reales=False):
        super().__init__()
        self.datos_reales = datos_reales
        self.horas = sorted(horas, key=lambda h: h["ts_utc"]) if horas else demo.historia()
        self.sincronizadas = []

    @classmethod
    def desde_lecturas(cls, lecturas):
        """Página pública con las lecturas reales (mismo modelo que en AWS, sin conexión)."""
        return cls([hora_desde_lectura(r) for r in lecturas], datos_reales=True)

    def historial(self):
        return self.horas

    def ultima_lectura(self):
        return self.horas[-1]

    def rango_dias(self):
        if not self.horas:
            return None, None
        return M.a_local(self.horas[0]["ts_utc"]).date(), M.a_local(self.horas[-1]["ts_utc"]).date()

    def horas_de(self, dia):
        return [h for h in self.horas if M.a_local(h["ts_utc"]).date() == dia]

    def upsert(self, records, origen, fuentes, condiciones):
        self.sincronizadas.append((origen, len(records)))
        if self.datos_reales and records:
            # Hace lo que sp_upsert_lecturas hace en AWS: el pronóstico toma en cuenta lo nuevo o editado.
            with self.lock:
                por_hora = {h["ts_utc"]: h for h in self.horas}
                for record in records:
                    if record.get("location_id", FIXED_LOCATION) == FIXED_LOCATION:
                        fila = hora_desde_lectura(record)
                        por_hora[fila["ts_utc"]] = fila
                self.horas = sorted(por_hora.values(), key=lambda h: h["ts_utc"])
            self.invalidar()
        return None   # en demo no hay PostgreSQL


class PostgresAnalytics(BaseAnalytics):
    kind = "postgres"
    SCHEMA = "climate_analytics"

    def __init__(self, factory):
        super().__init__()
        self.db = Pool(factory)

    def historial(self):
        with self.db.tx() as cur:
            cur.execute(f"SELECT o.timestamp_utc, o.temperature_c, o.humidity_pct, o.precipitation_mm"
                        f" FROM {self.SCHEMA}.observations o JOIN {self.SCHEMA}.series s ON s.series_id = o.series_id"
                        f" WHERE s.location_id = %s AND o.deleted_at_utc IS NULL ORDER BY o.timestamp_utc",
                        (FIXED_LOCATION,))
            filas = cur.fetchall()
        if not filas:
            raise ValueError("PostgreSQL does not have Mexico City readings yet. Run the ETL (step 5).")
        return [dict(ts_utc=f[0], temperature_c=f[1], humidity_pct=f[2], precipitation_mm=f[3]) for f in filas]

    def ultima_lectura(self):
        columnas = ("ts_utc", "temperature_c", "feels_like_c", "humidity_pct", "wind_speed_kmh", "wind_direction",
                    "uv_index", "precipitation_mm", "condition_id")
        with self.db.tx() as cur:
            cur.execute(f"SELECT o.timestamp_utc, o.temperature_c, o.feels_like_c, o.humidity_pct, o.wind_speed_kmh,"
                        f" o.wind_direction, o.uv_index, o.precipitation_mm, o.condition_id"
                        f" FROM {self.SCHEMA}.observations o JOIN {self.SCHEMA}.series s ON s.series_id = o.series_id"
                        f" WHERE s.location_id = %s AND o.deleted_at_utc IS NULL"
                        f" ORDER BY o.timestamp_utc DESC LIMIT 1", (FIXED_LOCATION,))
            fila = cur.fetchone()
        return dict(zip(columnas, fila)) if fila else None

    COLUMNAS_PUBLICAS = ("ts_utc", "temperature_c", "feels_like_c", "humidity_pct", "precipitation_mm",
                         "wind_speed_kmh", "wind_direction", "uv_index", "cloud_cover_pct", "condition_id")

    def rango_dias(self):
        with self.db.tx() as cur:
            cur.execute(f"SELECT MIN(o.timestamp_utc), MAX(o.timestamp_utc)"
                        f" FROM {self.SCHEMA}.observations o JOIN {self.SCHEMA}.series s ON s.series_id = o.series_id"
                        f" WHERE s.location_id = %s AND o.deleted_at_utc IS NULL", (FIXED_LOCATION,))
            fila = cur.fetchone()
        if not fila or fila[0] is None:
            return None, None
        return M.a_local(fila[0]).date(), M.a_local(fila[1]).date()

    def horas_de(self, dia):
        inicio = (datetime.combine(dia, datetime.min.time()) - M.DESFASE).replace(tzinfo=timezone.utc)
        with self.db.tx() as cur:
            cur.execute(f"SELECT o.timestamp_utc, o.temperature_c, o.feels_like_c, o.humidity_pct, o.precipitation_mm,"
                        f" o.wind_speed_kmh, o.wind_direction, o.uv_index, o.cloud_cover_pct, o.condition_id"
                        f" FROM {self.SCHEMA}.observations o JOIN {self.SCHEMA}.series s ON s.series_id = o.series_id"
                        f" WHERE s.location_id = %s AND o.deleted_at_utc IS NULL"
                        f" AND o.timestamp_utc >= %s AND o.timestamp_utc < %s ORDER BY o.timestamp_utc",
                        (FIXED_LOCATION, inicio, inicio + timedelta(days=1)))
            filas = cur.fetchall()
        return [dict(zip(self.COLUMNAS_PUBLICAS, f)) for f in filas]

    def upsert(self, records, origen, fuentes, condiciones):
        """CALL climate_analytics.sp_upsert_lecturas (SP 11)."""
        with self.db.tx() as cur:
            cur.execute(f"CALL {self.SCHEMA}.sp_upsert_lecturas(CAST(%s AS jsonb), %s, CAST(%s AS text[]),"
                        f" CAST(%s AS text[]), NULL, NULL, NULL, NULL, NULL)",
                        (dumps(records), origen, list(fuentes), list(condiciones)))
            fila = cur.fetchone()
        self.invalidar()
        return dict(batch_id=str(fila[0]), insertados=int(fila[1]), actualizados=int(fila[2]),
                    omitidos=int(fila[3]), rechazados=int(fila[4]))


def pg_factory(host, port, user, password, database="climate_analytics"):
    import pg8000.dbapi

    def connect():
        return pg8000.dbapi.connect(user=user, password=password, host=host, port=int(port), database=database,
                                    ssl_context=ssl_context(), timeout=20)
    return connect
