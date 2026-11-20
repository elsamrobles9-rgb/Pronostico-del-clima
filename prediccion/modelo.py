"""Pronóstico estadístico de 7 días con desglose por hora para la Ciudad de México.

Idea (se explica en una frase): "lo normal de la temporada + la desviación de hoy, que se
va apagando día a día", y después se reparte cada día en 24 horas con el ciclo típico del día.

1. NORMAL de un día: promedio de los mismos días del año (±10 días) del año anterior.
   Si todavía no hay un año de historia, se usa el promedio de los últimos 30 días.
2. SESGO del mes: cuánto se ha desviado lo real de lo normal en los últimos 30 días.
3. ANOMALÍA de hoy: la desviación de los últimos 3 días.
4. Pronóstico del día k:   normal(k) + sesgo + (anomalía - sesgo) * phi^k
   phi (0 a 0.95) mide cuánto "dura" una anomalía; se estima con la historia.
   Día 1 se parece a hoy; hacia el día 7 se acerca a lo normal de la temporada.
5. LLUVIA: la frecuencia de días con lluvia de la temporada, corregida por lo que llovió
   la última semana (con el mismo apagado). Milímetros = probabilidad x lluvia típica.
6. POR HORA: el ciclo típico de los últimos 30 días (más fresco en la madrugada, máximo
   por la tarde) se estira entre la mínima y la máxima pronosticadas; la lluvia se
   reparte en las horas en que suele llover. Las horas siempre cuadran con el día.

Solo usa la biblioteca estándar. Horas locales = UTC-6 (la Ciudad de México no usa horario
de verano desde octubre de 2022).
"""
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

DESFASE = timedelta(hours=-6)
VARIABLES = ("temperature_mean_c", "temperature_min_c", "temperature_max_c", "humidity_mean_pct")
UMBRAL_LLUVIA_DIA = 0.5     # mm en el día (mismo umbral que sp_evaluar_predicciones)
UMBRAL_LLUVIA_HORA = 0.1    # mm en la hora
VENTANA = 10                # días alrededor de la misma fecha del año anterior
DIAS_SESGO = 30
DIAS_ANOMALIA = 3
DIAS_PERFIL = 30
HORAS_MINIMAS = 20          # un día cuenta como completo con 20 de 24 horas
PHI_DEFECTO = 0.6


def a_local(ts_utc):
    """datetime UTC (con o sin zona) -> datetime local sin zona."""
    if ts_utc.tzinfo is not None:
        ts_utc = ts_utc.astimezone(timezone.utc).replace(tzinfo=None)
    return ts_utc + DESFASE


def a_utc(local):
    return (local - DESFASE).replace(tzinfo=timezone.utc)


def promedio(valores, defecto=None):
    valores = [v for v in valores if v is not None]
    return sum(valores) / len(valores) if valores else defecto


def preparar(horas):
    """horas: iterable de dicts con ts_utc, temperature_c, humidity_pct, precipitation_mm."""
    salida = []
    for h in horas:
        salida.append(dict(local=a_local(h["ts_utc"]), t=float(h["temperature_c"]),
                           hum=float(h["humidity_pct"]), lluvia=float(h["precipitation_mm"])))
    salida.sort(key=lambda x: x["local"])
    return salida


def resumen_diario(horas):
    grupos = defaultdict(list)
    for h in horas:
        grupos[h["local"].date()].append(h)
    dias = {}
    for dia, filas in grupos.items():
        if len(filas) < HORAS_MINIMAS:
            continue
        temperaturas = [f["t"] for f in filas]
        dias[dia] = dict(temperature_mean_c=sum(temperaturas) / len(temperaturas),
                         temperature_min_c=min(temperaturas), temperature_max_c=max(temperaturas),
                         humidity_mean_pct=promedio([f["hum"] for f in filas]),
                         precipitation_total_mm=sum(f["lluvia"] for f in filas))
    return dias


def dia_del_anio(dia):
    return min(dia.timetuple().tm_yday, 365)


def distancia_circular(a, b):
    d = abs(a - b)
    return min(d, 365 - d)


class Modelo:
    """Se construye con TODA la historia; cada pronóstico solo mira lo anterior a su emisión."""

    def __init__(self, horas, phi=None):
        self.horas = preparar(horas)
        if not self.horas:
            raise ValueError("No hay lecturas para entrenar el modelo.")
        self.dias = resumen_diario(self.horas)
        self.fechas = sorted(self.dias)
        if not self.fechas:
            raise ValueError("No hay ningún día completo en la historia.")
        self.por_dia_anio = defaultdict(list)
        for dia in self.fechas:
            self.por_dia_anio[dia_del_anio(dia)].append(dia)
        self.horas_por_dia = defaultdict(list)
        for h in self.horas:
            self.horas_por_dia[h["local"].date()].append(h)
        self.phi = phi or self.estimar_phi(self.fechas[-1])

    # ------------------------------------------------------------------ piezas del día
    def ultimo_dia(self):
        return self.fechas[-1]

    def dias_previos(self, emision, cantidad):
        return [d for d in (emision - timedelta(days=i) for i in range(cantidad)) if d in self.dias]

    def climatologia(self, dia, emision):
        """Días de la misma época del año anterior (al menos 300 días antes de la emisión)."""
        limite = emision - timedelta(days=300)
        objetivo = dia_del_anio(dia)
        encontrados = []
        for desfase in range(-VENTANA, VENTANA + 1):
            clave = (objetivo + desfase - 1) % 365 + 1
            encontrados.extend(d for d in self.por_dia_anio.get(clave, ()) if d <= limite)
        return encontrados

    def normal(self, var, dia, emision):
        similares = self.climatologia(dia, emision)
        if len(similares) >= 5:
            return promedio(self.dias[d][var] for d in similares)
        recientes = self.dias_previos(emision, DIAS_SESGO)
        return promedio((self.dias[d][var] for d in recientes), 0.0)

    def desviacion(self, var, emision, cantidad):
        dias = self.dias_previos(emision, cantidad)
        return promedio((self.dias[d][var] - self.normal(var, d, emision) for d in dias), 0.0)

    def frecuencia_lluvia(self, dias):
        dias = list(dias)
        if not dias:
            return None
        return sum(1 for d in dias if self.dias[d]["precipitation_total_mm"] >= UMBRAL_LLUVIA_DIA) / len(dias)

    def clima_lluvia(self, dia, emision):
        """Frecuencia de días con lluvia de la temporada (o del último mes) y los días usados."""
        similares = self.climatologia(dia, emision)
        if len(similares) >= 5:
            return self.frecuencia_lluvia(similares), similares
        recientes = self.dias_previos(emision, DIAS_SESGO)
        return self.frecuencia_lluvia(recientes) or 0.0, recientes

    def estimar_phi(self, hasta):
        """Autocorrelación de un día al siguiente de las desviaciones respecto a lo normal."""
        fechas = [d for d in self.fechas if d <= hasta]
        phi = {}
        for var in VARIABLES + ("lluvia",):
            pares = []
            anterior = None
            for d in fechas:
                if var == "lluvia":
                    clima = self.clima_lluvia(d, d)[0]
                    actual = (1.0 if self.dias[d]["precipitation_total_mm"] >= UMBRAL_LLUVIA_DIA else 0.0) - clima
                else:
                    actual = self.dias[d][var] - self.normal(var, d, d)
                if anterior is not None and anterior[0] == d - timedelta(days=1):
                    pares.append((anterior[1], actual))
                anterior = (d, actual)
            num = sum(a * b for a, b in pares)
            den = sum(a * a for a, _ in pares)
            phi[var] = round(max(0.0, min(0.95, num / den)), 4) if len(pares) >= 20 and den > 0 else PHI_DEFECTO
        return phi

    # ------------------------------------------------------------------ pronóstico diario
    def pronostico_dia(self, emision, dia):
        """Pronóstico del día `dia` con lo que se sabía al cierre del día `emision`."""
        k = (dia - emision).days
        if k < 1:
            raise ValueError("Solo se pronostican días posteriores a la emisión.")
        valores = {}
        for var in VARIABLES:
            sesgo = self.desviacion(var, emision, DIAS_SESGO)
            anomalia = self.desviacion(var, emision, DIAS_ANOMALIA)
            valores[var] = self.normal(var, dia, emision) + sesgo + (anomalia - sesgo) * self.phi[var] ** k
        tmin, tmed, tmax = sorted((valores["temperature_min_c"], valores["temperature_mean_c"],
                                   valores["temperature_max_c"]))
        valores.update(temperature_min_c=tmin, temperature_mean_c=tmed, temperature_max_c=tmax,
                       humidity_mean_pct=max(5.0, min(100.0, valores["humidity_mean_pct"])))
        clima, referencia = self.clima_lluvia(dia, emision)
        semana = self.frecuencia_lluvia(self.dias_previos(emision, 7))
        semana = clima if semana is None else semana
        prob = max(0.0, min(1.0, clima + (semana - clima) * self.phi["lluvia"] ** k))
        lluviosos = [self.dias[d]["precipitation_total_mm"] for d in referencia
                     if self.dias[d]["precipitation_total_mm"] >= UMBRAL_LLUVIA_DIA]
        tipica = promedio(lluviosos, 2.0)
        valores.update(rain_probability_pct=round(prob * 100, 2), precipitation_total_mm=round(prob * tipica, 3))
        return valores

    # ------------------------------------------------------------------ por hora
    def perfil(self, emision):
        """Ciclo típico del día con los últimos 30 días hasta la emisión."""
        posicion, humedad = defaultdict(list), defaultdict(list)
        lluvia_horas, lluvia_mm = defaultdict(int), defaultdict(float)
        horas_vistas = defaultdict(int)
        dias = self.dias_previos(emision, DIAS_PERFIL)
        for d in dias:
            resumen = self.dias[d]
            rango = resumen["temperature_max_c"] - resumen["temperature_min_c"]
            for h in self.horas_por_dia[d]:
                hora = h["local"].hour
                if rango >= 1:
                    posicion[hora].append((h["t"] - resumen["temperature_min_c"]) / rango)
                humedad[hora].append(h["hum"] - resumen["humidity_mean_pct"])
                horas_vistas[hora] += 1
                if h["lluvia"] >= UMBRAL_LLUVIA_HORA:
                    lluvia_horas[hora] += 1
                lluvia_mm[hora] += h["lluvia"]
        # La lluvia se reparte con la temporada del año anterior si la última semana fue seca.
        for d in self.climatologia(emision + timedelta(days=1), emision):
            for h in self.horas_por_dia[d]:
                hora = h["local"].hour
                horas_vistas[hora] += 1
                if h["lluvia"] >= UMBRAL_LLUVIA_HORA:
                    lluvia_horas[hora] += 1
                lluvia_mm[hora] += h["lluvia"]
        total_mm = sum(lluvia_mm.values())
        frecuencia = {h: lluvia_horas[h] / horas_vistas[h] if horas_vistas[h] else 0.0 for h in range(24)}
        pico = max(frecuencia.values()) or 1.0
        return dict(
            posicion={h: promedio(posicion[h], 0.5) for h in range(24)},
            humedad={h: promedio(humedad[h], 0.0) for h in range(24)},
            reparto={h: (lluvia_mm[h] / total_mm if total_mm > 0 else 1 / 24) for h in range(24)},
            peso_lluvia={h: frecuencia[h] / pico for h in range(24)})

    def pronostico_horas(self, dia_pronostico, perfil, dia):
        """24 horas de un día a partir de su pronóstico diario."""
        horas = []
        tmin, tmax = dia_pronostico["temperature_min_c"], dia_pronostico["temperature_max_c"]
        prob_dia = dia_pronostico["rain_probability_pct"]
        for hora in range(24):
            local = datetime.combine(dia, datetime.min.time()) + timedelta(hours=hora)
            temperatura = tmin + perfil["posicion"][hora] * (tmax - tmin)
            horas.append(dict(
                local=local, temperature_c=round(temperatura, 2),
                humidity_pct=round(max(5.0, min(100.0, dia_pronostico["humidity_mean_pct"] + perfil["humedad"][hora])), 2),
                rain_probability_pct=round(min(prob_dia, prob_dia * perfil["peso_lluvia"][hora]), 2),
                precipitation_mm=round(dia_pronostico["precipitation_total_mm"] * perfil["reparto"][hora], 3)))
        return horas

    # ------------------------------------------------------------------ todo junto
    def pronostico(self, desde_dia=None, dias=7):
        """Días desde `desde_dia` (por defecto el día siguiente a la última lectura) con sus horas."""
        emision = self.ultimo_dia()
        inicio = max(desde_dia or emision + timedelta(days=1), emision + timedelta(days=1))
        perfil = self.perfil(emision)
        salida = []
        for i in range(dias):
            dia = inicio + timedelta(days=i)
            diario = self.pronostico_dia(emision, dia)
            salida.append(dict(dia=dia, horizonte=(dia - emision).days, **diario,
                               horas=self.pronostico_horas(diario, perfil, dia)))
        return salida


# ---------------------------------------------------------------------- métodos de referencia
def persistencia(modelo, emision, dia):
    hoy = modelo.dias[emision]
    llovio = hoy["precipitation_total_mm"] >= UMBRAL_LLUVIA_DIA
    return {**{v: hoy[v] for v in VARIABLES}, "rain_probability_pct": 100.0 if llovio else 0.0,
            "precipitation_total_mm": hoy["precipitation_total_mm"]}


def promedio_movil(modelo, emision, dia, ventana=7):
    dias = modelo.dias_previos(emision, ventana)
    valores = {v: promedio(modelo.dias[d][v] for d in dias) for v in VARIABLES}
    frecuencia = modelo.frecuencia_lluvia(dias) or 0.0
    valores.update(rain_probability_pct=round(frecuencia * 100, 2),
                   precipitation_total_mm=promedio(modelo.dias[d]["precipitation_total_mm"] for d in dias))
    return valores


# ---------------------------------------------------------------------- presentación
CONDICIONES_FUENTE = {
    "COND-CLEAR": ("despejado", "Despejado"), "COND-PARTLY-CLOUDY": ("nubes-claros", "Nubes y claros"),
    "COND-OVERCAST": ("nublado", "Nublado"), "COND-FOG": ("niebla", "Niebla"),
    "COND-DRIZZLE": ("lluvia-posible", "Llovizna"), "COND-RAIN": ("lluvia", "Lluvia"),
    "COND-STORM": ("tormenta", "Tormenta"), "COND-SNOW": ("nieve", "Nieve"),
}


def es_de_dia(hora):
    return 7 <= hora < 19


def condicion_pronostico(probabilidad, milimetros, humedad, por_hora=False):
    """Ícono y texto a partir de lo que guarda la tabla predictions (lluvia y humedad)."""
    limite_mm = 0.2 if por_hora else UMBRAL_LLUVIA_DIA
    if probabilidad >= 60 and milimetros >= limite_mm:
        return "lluvia", "Lluvia"
    if probabilidad >= 35:
        return "lluvia-posible", "Posible lluvia"
    if humedad >= 85:
        return "nublado", "Nublado"
    if probabilidad >= 15 or humedad >= 70:
        return "nubes-claros", "Nubes y claros"
    return "despejado", "Despejado"


def condicion_lectura(condition_id):
    return CONDICIONES_FUENTE.get(condition_id, ("nubes-claros", condition_id or "—"))


def errores(pares):
    """MAE y RMSE de una lista de (predicho, real)."""
    if not pares:
        return None, None, 0
    diferencias = [p - r for p, r in pares]
    mae = sum(abs(d) for d in diferencias) / len(diferencias)
    rmse = math.sqrt(sum(d * d for d in diferencias) / len(diferencias))
    return mae, rmse, len(diferencias)


def hoy_local():
    return a_local(datetime.now(timezone.utc))


def fecha(texto):
    return date.fromisoformat(texto)


# ---------------------------------------------------------------------- evaluación
METODOS = {
    "CLIMATOLOGY_ANOMALY": lambda m, emision, dia: m.pronostico_dia(emision, dia),
    "PERSISTENCE": persistencia,
    "MOVING_AVERAGE_7D": promedio_movil,
}
VARIABLES_EVALUADAS = VARIABLES + ("precipitation_total_mm",)


def evaluar(modelo, emisiones, horizonte=7):
    """{método: {variable: {k: [(predicho, real), ...]}}} para cada día de emisión."""
    pares = {m: {v: defaultdict(list) for v in VARIABLES_EVALUADAS} for m in METODOS}
    for emision in emisiones:
        if emision not in modelo.dias:
            continue
        for k in range(1, horizonte + 1):
            dia = emision + timedelta(days=k)
            if dia not in modelo.dias:
                continue
            real = modelo.dias[dia]
            for nombre, metodo in METODOS.items():
                predicho = metodo(modelo, emision, dia)
                for var in VARIABLES_EVALUADAS:
                    pares[nombre][var][k].append((predicho[var], real[var]))
    return pares


def evaluar_horas(modelo, emisiones):
    """Error por hora del día siguiente (temperatura y humedad) del modelo publicado."""
    pares = {"temperature_c": [], "humidity_pct": []}
    for emision in emisiones:
        dia = emision + timedelta(days=1)
        if emision not in modelo.dias or dia not in modelo.dias:
            continue
        horas = modelo.pronostico_horas(modelo.pronostico_dia(emision, dia), modelo.perfil(emision), dia)
        reales = {h["local"].hour: h for h in modelo.horas_por_dia[dia]}
        for h in horas:
            real = reales.get(h["local"].hour)
            if real:
                pares["temperature_c"].append((h["temperature_c"], real["t"]))
                pares["humidity_pct"].append((h["humidity_pct"], real["hum"]))
    return pares
