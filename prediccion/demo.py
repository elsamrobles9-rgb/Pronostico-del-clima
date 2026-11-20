"""Historia sintética de la CDMX para el modo demo y las pruebas (no usa AWS).

Imita lo básico del clima de la ciudad: tardes más calientes, madrugadas frescas,
temporada de lluvias de junio a octubre con aguaceros por la tarde y meses secos.
Siempre genera los mismos datos (semilla fija) y termina ayer a las 23:00 hora local.
"""
import math
import random
from datetime import datetime, timedelta, timezone

from prediccion.modelo import DESFASE, a_local

DIRECCIONES = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def historia(dias=425, hasta=None, semilla=20251120):
    aleatorio = random.Random(semilla)
    ahora_local = a_local(hasta or datetime.now(timezone.utc))
    fin_local = datetime.combine(ahora_local.date(), datetime.min.time())   # hoy 00:00 local (excluido)
    inicio_local = fin_local - timedelta(days=dias)
    horas = []
    anomalia = 0.0
    for d in range(dias):
        dia = (inicio_local + timedelta(days=d)).date()
        doy = dia.timetuple().tm_yday
        lluvias = 152 <= doy <= 290                              # junio a mediados de octubre
        media = 17.5 + 3.0 * math.sin(2 * math.pi * (doy - 60) / 365)
        anomalia = 0.7 * anomalia + aleatorio.gauss(0, 1.0)
        amplitud = (5.0 if lluvias else 7.5) + aleatorio.uniform(-1, 1)
        llueve = aleatorio.random() < (0.55 if lluvias else 0.07)
        total = aleatorio.expovariate(1 / (7.0 if lluvias else 2.0)) if llueve else 0.0
        humedad_base = (68 if lluvias else 45) + aleatorio.uniform(-6, 6)
        for hora in range(24):
            fase = math.cos(2 * math.pi * (hora - 15) / 24)        # máximo a las 15:00
            temperatura = media + anomalia + amplitud * fase
            lluvia = 0.0
            if llueve and 15 <= hora <= 20:
                lluvia = round(total / 6 * aleatorio.uniform(0.5, 1.5), 1)
            humedad = max(15, min(100, humedad_base - 18 * fase + (20 if lluvia else 0)))
            local = datetime.combine(dia, datetime.min.time()) + timedelta(hours=hora)
            horas.append(dict(
                ts_utc=(local - DESFASE).replace(tzinfo=timezone.utc), temperature_c=round(temperatura, 1),
                humidity_pct=round(humedad), precipitation_mm=lluvia,
                feels_like_c=round(temperatura - (1.0 if lluvia else 0.0), 1),
                wind_speed_kmh=round(aleatorio.uniform(2, 18), 1), wind_direction=aleatorio.choice(DIRECCIONES),
                uv_index=round(max(0.0, 9 * fase) if 8 <= hora <= 17 and not lluvia else 0.0, 1),
                cloud_cover_pct=80 if lluvia else aleatorio.choice((5, 20, 40, 60)),
                condition_id="COND-RAIN" if lluvia else ("COND-PARTLY-CLOUDY" if hora % 5 else "COND-CLEAR")))
    return horas
