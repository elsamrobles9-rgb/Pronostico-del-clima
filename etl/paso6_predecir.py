"""PASO 6 · Predicción del clima diario con los datos de PostgreSQL.

Qué hace:
  1. Lee los agregados diarios (climate_aggregates) de las 5 ciudades.
  2. Divide los días en ENTRENAMIENTO (70 %), VALIDACIÓN (15 %) y PRUEBA (15 %).
  3. Compara 3 métodos que predicen el día siguiente:
       - Persistencia:      "mañana será igual que hoy" (referencia mínima).
       - Promedio 7 días:   "mañana será como la última semana".
       - Regresión lineal:  combina el día anterior y el promedio de 7 días.
  4. Mide el error de cada método (MAE) y elige el que menos se equivoca en VALIDACIÓN.
  5. Guarda todo en PostgreSQL (la evaluación y las métricas de prueba del ganador las
     calculan los stored procedures sp_evaluar_predicciones y sp_calcular_metricas):
       prediction_models · training_runs · model_metrics
       predictions (días de PRUEBA y 7 días futuros) · prediction_evaluations (PRUEBA vs. real)

Todo ocurre en una sola transacción. Repetirlo crea una nueva corrida (historial).
Necesita la RDS climate-postgres encendida y credenciales de AWS vigentes.

Ejecutar desde la carpeta climate_portal_v3:
    .\\.venv\\Scripts\\python.exe etl\\paso6_predecir.py
"""
import argparse
import hashlib
import json
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import config
from comun import confirmar, id_estable, sql_texto

S = config.POSTGRES_SCHEMA
t = sql_texto
VENTANA = 7
RIDGE = 0.01
DIAS_FUTUROS = 7
UMBRAL_LLUVIA_MM = 0.5
VARIABLES = {  # columna de climate_aggregates -> unidad
    "temperature_mean_c": "°C", "temperature_min_c": "°C", "temperature_max_c": "°C",
    "humidity_mean_pct": "%", "precipitation_total_mm": "mm",
}
METODOS = {
    "PERSISTENCE": dict(name="Persistencia (día anterior)",
                        description="Predice que mañana será igual que hoy. Es la referencia mínima a superar."),
    "MOVING_AVERAGE_7D": dict(name="Promedio móvil de 7 días",
                              description="Predice el promedio de los últimos 7 días."),
    "LINEAR_REGRESSION": dict(name="Regresión lineal (día anterior + promedio 7 días)",
                              description="Mínimos cuadrados con regularización ridge, ajustada por ciudad y variable."),
}


# --------------------------------------------------------------------------- datos
def leer_agregados(destino):
    filas = destino.ejecutar(
        f"SELECT s.series_id::text, s.location_id, s.timezone_name,"
        f" (a.starts_at_utc AT TIME ZONE s.timezone_name)::date::text, a.starts_at_utc::text, a.ends_at_utc::text,"
        f" a.version, a.coverage_pct::text, " + ", ".join(f"a.{v}::text" for v in VARIABLES) +
        f" FROM {S}.climate_aggregates a JOIN {S}.series s ON s.series_id = a.series_id"
        f" WHERE a.granularity = 'DAY' ORDER BY s.location_id, a.starts_at_utc", filas=True)
    series = defaultdict(list)
    for sid, lid, zona, dia, inicio, fin, version, cobertura, *valores in filas:
        if Decimal(str(cobertura)) < 100:   # días incompletos (inicio y fin del periodo)
            continue
        series[sid].append(dict(series_id=sid, location_id=lid, zona=zona, dia=date.fromisoformat(str(dia)[:10]),
                                inicio=str(inicio), fin=str(fin), version=int(version),
                                **{v: float(Decimal(str(x))) for v, x in zip(VARIABLES, valores)}))
    return dict(series)


def dividir(series):
    dias = sorted({d["dia"] for filas in series.values() for d in filas})
    if len(dias) < 30:
        raise RuntimeError(f"Solo hay {len(dias)} días completos; se necesitan al menos 30. Ejecuta antes el paso 5.")
    return dias[0], dias[int(len(dias) * 0.70)], dias[int(len(dias) * 0.85)], dias[-1] + timedelta(days=1)


# --------------------------------------------------------------------------- métodos
def resolver(a, b):
    """Resuelve a·x = b (3x3) por eliminación de Gauss."""
    n = len(b)
    m = [fila[:] + [b[i]] for i, fila in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            raise ArithmeticError("matriz singular")
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def rasgos(historia, var):
    return [1.0, historia[-1][var], sum(h[var] for h in historia[-VENTANA:]) / VENTANA]


def ajustar(filas, var):
    """Regresión lineal ridge: y = b0 + b1·ayer + b2·promedio7."""
    xs = [rasgos(filas[:i], var) for i in range(VENTANA, len(filas))]
    ys = [filas[i][var] for i in range(VENTANA, len(filas))]
    xtx = [[sum(x[i] * x[j] for x in xs) + (RIDGE * len(xs) if i == j and i > 0 else 0) for j in range(3)]
           for i in range(3)]
    xty = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(3)]
    return resolver(xtx, xty)


def predecir(metodo, historia, var, coef=None):
    if metodo == "PERSISTENCE":
        valor = historia[-1][var]
    elif metodo == "MOVING_AVERAGE_7D":
        valor = sum(h[var] for h in historia[-VENTANA:]) / VENTANA
    else:
        valor = sum(c * x for c, x in zip(coef[var], rasgos(historia, var)))
    if var == "humidity_mean_pct":
        valor = min(100.0, valor)
    return max(0.0, valor) if var in ("humidity_mean_pct", "precipitation_total_mm") else valor


def pronostico(metodo, historia, coef):
    p = {v: predecir(metodo, historia, v, coef) for v in VARIABLES}
    p["temperature_min_c"] = min(p["temperature_min_c"], p["temperature_mean_c"])
    p["temperature_max_c"] = max(p["temperature_max_c"], p["temperature_mean_c"])
    lluviosos = sum(1 for h in historia[-VENTANA:] if h["precipitation_total_mm"] >= UMBRAL_LLUVIA_MM)
    p["rain_probability_pct"] = 100.0 * lluviosos / VENTANA
    return p


def evaluar(series, metodo, coefs, desde, hasta):
    """Predicción a 1 día para cada día en [desde, hasta). Devuelve lista de (fila_real, pronóstico)."""
    pares = []
    for sid, filas in series.items():
        for i, real in enumerate(filas):
            if desde <= real["dia"] < hasta and i >= VENTANA and filas[i - 1]["dia"] == real["dia"] - timedelta(days=1):
                pares.append((real, pronostico(metodo, filas[:i], coefs.get(sid))))
    return pares


def mae(pares, var, sid=None):
    errores = [abs(p[var] - r[var]) for r, p in pares if sid is None or r["series_id"] == sid]
    return (sum(errores) / len(errores), len(errores)) if errores else (None, 0)


def rmse(pares, var, sid=None):
    errores = [(p[var] - r[var]) ** 2 for r, p in pares if sid is None or r["series_id"] == sid]
    return ((sum(errores) / len(errores)) ** 0.5, len(errores)) if errores else (None, 0)


# --------------------------------------------------------------------------- SQL
def d6(x):
    return str(Decimal(str(x)).quantize(Decimal("0.000001")))


def medianoche_utc(dia):
    return f"{dia.isoformat()}T00:00:00Z"


def sql_modelos():
    valores = ",\n".join(
        f"({t(id_estable('modelo', clave))}, {t(m['name'])}, {t(clave)}, {t(json.dumps(list(VARIABLES)))}, "
        f"{t(m['description'])}, true)" for clave, m in METODOS.items())
    return (f"INSERT INTO {S}.prediction_models (model_id, name, algorithm, targets, description, is_active)"
            f" VALUES\n{valores}\nON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description,"
            f" algorithm = EXCLUDED.algorithm, targets = EXCLUDED.targets RETURNING model_id::text, algorithm")


def huella(series):
    texto = "\n".join(f"{d['series_id']}|{d['dia']}|" + "|".join(f"{d[v]:.6f}" for v in VARIABLES)
                      for filas in series.values() for d in filas)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def main():
    import paso5_etl_postgres as p5
    parser = argparse.ArgumentParser(description="Predicción diaria con climate_aggregates")
    parser.add_argument("--si", action="store_true", help="no pedir confirmación")
    args = parser.parse_args()

    destino = p5.conectar_postgres()
    p5.verificar_procedimientos(destino)
    series = leer_agregados(destino)
    destino.commit()
    desde, fin_entreno, fin_valid, hasta = dividir(series)
    print(f"Días completos: {sum(len(f) for f in series.values())} en {len(series)} ciudades")
    print(f"  entrenamiento {desde} a {fin_entreno - timedelta(days=1)}")
    print(f"  validación    {fin_entreno} a {fin_valid - timedelta(days=1)}")
    print(f"  prueba        {fin_valid} a {hasta - timedelta(days=1)}\n")

    # 1) Ajuste con ENTRENAMIENTO y comparación en VALIDACIÓN y PRUEBA
    resultados = {}
    for metodo in METODOS:
        coefs = {}
        if metodo == "LINEAR_REGRESSION":
            for sid, filas in series.items():
                entreno = [d for d in filas if d["dia"] < fin_entreno]
                coefs[sid] = {v: ajustar(entreno, v) for v in VARIABLES}
        resultados[metodo] = dict(coefs=coefs, VALIDATION=evaluar(series, metodo, coefs, fin_entreno, fin_valid),
                                  TEST=evaluar(series, metodo, coefs, fin_valid, hasta))
    print(f"{'Método':<52} {'MAE val. T media':>17} {'MAE prueba T media':>19} {'RMSE prueba T media':>20}")
    for metodo, r in resultados.items():
        print(f"{METODOS[metodo]['name']:<52} {mae(r['VALIDATION'], 'temperature_mean_c')[0]:>15.2f} °C"
              f" {mae(r['TEST'], 'temperature_mean_c')[0]:>17.2f} °C {rmse(r['TEST'], 'temperature_mean_c')[0]:>18.2f} °C")
    temperaturas = ("temperature_mean_c", "temperature_min_c", "temperature_max_c")
    ganador = min(resultados, key=lambda m: sum(mae(resultados[m]["VALIDATION"], v)[0] for v in temperaturas))
    print(f"\nGanador (menor error en validación): {METODOS[ganador]['name']}")

    # 2) Modelo final: se reajusta con TODOS los días para pronosticar el futuro
    coefs_finales = {sid: {v: ajustar(filas, v) for v in VARIABLES} for sid, filas in series.items()} \
        if ganador == "LINEAR_REGRESSION" else {}
    futuros = {}
    for sid, filas in series.items():
        historia = list(filas)
        futuros[sid] = []
        for k in range(DIAS_FUTUROS):
            dia = historia[-1]["dia"] + timedelta(days=1)
            p = pronostico(ganador, historia, coefs_finales.get(sid))
            futuros[sid].append((dia, p))
            historia.append({**p, "dia": dia})   # pronóstico recursivo: lo predicho alimenta el día siguiente

    print("\nPronóstico de los próximos 7 días (temperatura media °C / prob. lluvia %):")
    for sid, dias in futuros.items():
        lid = series[sid][0]["location_id"]
        print(f"  {lid:<9} " + "  ".join(f"{d.strftime('%d/%m')} {p['temperature_mean_c']:.1f}/{p['rain_probability_pct']:.0f}%"
                                     for d, p in dias))
    total = sum(len(r["TEST"]) for r in resultados.values()) // len(METODOS)
    if not confirmar(f"\n¿Guardar modelos, métricas, {total} predicciones de prueba con su evaluación "
                     f"y {DIAS_FUTUROS * len(series)} predicciones futuras en PostgreSQL?", args.si):
        print("Cancelado. No se escribió nada.")
        destino.cerrar()
        return

    # 3) Guardar en PostgreSQL (una transacción)
    try:
        version = datetime.now(timezone.utc).strftime("v%Y%m%d-%H%M%S")
        modelos = {alg: mid for mid, alg in destino.ejecutar(sql_modelos(), filas=destino.directo)} or {
            k: id_estable("modelo", k) for k in METODOS}
        dataset = huella(series)
        corridas = {}
        for metodo, r in resultados.items():
            corridas[metodo] = str(uuid.uuid4())
            hiper = {"ventana_dias": VENTANA, "ridge": RIDGE, "horizonte_dias": 1, "umbral_lluvia_mm": UMBRAL_LLUVIA_MM}
            if metodo == "LINEAR_REGRESSION":
                fuente = coefs_finales if metodo == ganador else r["coefs"]
                hiper["coeficientes_b0_ayer_prom7"] = {
                    series[sid][0]["location_id"]: {v: [round(c, 6) for c in cs] for v, cs in vs.items()}
                    for sid, vs in fuente.items()}
            registros = sum(1 for filas in series.values() for d in filas if d["dia"] < fin_entreno)
            destino.ejecutar(
                f"INSERT INTO {S}.training_runs (training_run_id, model_id, version, status, started_at_utc,"
                f" finished_at_utc, data_from_utc, data_until_utc, train_until_utc, validation_until_utc,"
                f" test_until_utc, record_count, dataset_hash, dataset_uri, artifact_uri, hyperparameters, features)"
                f" VALUES ({t(corridas[metodo])}, {t(modelos[metodo])}, {t(version)}, 'SUCCEEDED', now(),"
                f" clock_timestamp(), {t(medianoche_utc(desde))}, {t(medianoche_utc(hasta))},"
                f" {t(medianoche_utc(fin_entreno))}, {t(medianoche_utc(fin_valid))}, {t(medianoche_utc(hasta))},"
                f" {registros}, {t(dataset)}, {t(f'postgresql://{config.POSTGRES_DB}/{S}.climate_aggregates?granularity=DAY')},"
                f" NULL, {t(json.dumps(hiper, ensure_ascii=False))},"
                f" {t(json.dumps(['valor_dia_anterior', 'promedio_7_dias'] if metodo == 'LINEAR_REGRESSION' else ['historial_diario']))})")
            metricas = []
            for division in ("VALIDATION", "TEST"):
                if division == "TEST" and metodo == ganador:
                    continue   # la prueba del ganador la calculan los SP 9 y 10 desde PostgreSQL
                pares = r[division]
                for sid in series:
                    propios = [(a, b) for a, b in pares if a["series_id"] == sid]
                    if not propios:
                        continue
                    inicio, fin = min(a["inicio"] for a, _ in propios), max(a["fin"] for a, _ in propios)
                    for var, unidad in VARIABLES.items():
                        for nombre, funcion in (("MAE", mae), ("RMSE", rmse)):
                            valor, n = funcion(propios, var)
                            metricas.append(f"({t(str(uuid.uuid4()))}, {t(corridas[metodo])}, {t(sid)}, {t(var)},"
                                            f" {t(nombre)}, {t(division)}, 1440, 'DAY', {t(inicio)}, {t(fin)},"
                                            f" {d6(valor)}, {n}, {t(unidad)})")
            destino.ejecutar(
                f"INSERT INTO {S}.model_metrics (metric_id, training_run_id, series_id, variable_name, metric_name,"
                f" dataset_split, horizon_minutes, granularity, starts_at_utc, ends_at_utc, value, sample_count, unit)"
                f" VALUES\n" + ",\n".join(metricas))

        # Predicciones de PRUEBA (del ganador); su evaluación y métricas las calculan los SP 9 y 10
        corrida_prueba, corrida_futuro = str(uuid.uuid4()), str(uuid.uuid4())
        predicciones = []
        for real, p in resultados[ganador]["TEST"]:
            predicciones.append(fila_prediccion(str(uuid.uuid4()), corridas[ganador], real["series_id"],
                                                corrida_prueba, t(real["inicio"]), t(real["fin"]), p))
        # Predicciones FUTURAS (se evaluarán cuando lleguen los datos reales)
        for sid, dias in futuros.items():
            zona = series[sid][0]["zona"]
            for dia, p in dias:
                inicio = f"('{dia.isoformat()}'::date::timestamp AT TIME ZONE {t(zona)})"
                fin = f"('{(dia + timedelta(days=1)).isoformat()}'::date::timestamp AT TIME ZONE {t(zona)})"
                predicciones.append(fila_prediccion(str(uuid.uuid4()), corridas[ganador], sid, corrida_futuro,
                                                    inicio, fin, p))
        destino.ejecutar(
            f"INSERT INTO {S}.predictions (prediction_id, training_run_id, series_id, forecast_run_id, issued_at_utc,"
            f" target_start_utc, target_end_utc, granularity, temperature_min_c, temperature_max_c,"
            f" temperature_mean_c, humidity_pct, rain_probability_pct, rainfall_mm) VALUES\n" + ",\n".join(predicciones))
        (evaluadas,) = p5.llamar(destino, "sp_evaluar_predicciones", t(corrida_prueba), "NULL")
        (filas_metricas,) = p5.llamar(destino, "sp_calcular_metricas", t(corrida_prueba), "'TEST'", "NULL")
        destino.commit()
    except Exception as error:
        destino.rollback()
        destino.cerrar()
        print(f"\nNo se guardó nada (se revirtió): {type(error).__name__}: {error}")
        sys.exit(1)
    destino.cerrar()
    print(f"\nGuardado: 3 modelos, 3 corridas de entrenamiento ({version}), métricas MAE y RMSE,"
          f" {DIAS_FUTUROS * len(series)} predicciones futuras.")
    print(f"Stored procedures: sp_evaluar_predicciones evaluó {int(evaluadas)} predicciones de prueba;"
          f" sp_calcular_metricas guardó {int(filas_metricas)} métricas (MAE y RMSE) del ganador.")
    print(f"Corrida futura (forecast_run_id): {corrida_futuro}")
    print("Revisa los resultados con etl/consultas_prediccion.sql en DBeaver.")


def fila_prediccion(pid, corrida, sid, pronostico_id, inicio_sql, fin_sql, p):
    return (f"({t(pid)}, {t(corrida)}, {t(sid)}, {t(pronostico_id)}, now(), {inicio_sql}, {fin_sql}, 'DAY',"
            f" {d6(p['temperature_min_c'])}, {d6(p['temperature_max_c'])}, {d6(p['temperature_mean_c'])},"
            f" {d6(p['humidity_mean_pct'])}, {d6(p['rain_probability_pct'])}, {d6(p['precipitation_total_mm'])})")


if __name__ == "__main__":
    main()
