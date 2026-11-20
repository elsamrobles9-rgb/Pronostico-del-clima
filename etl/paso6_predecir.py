"""PASO 6 · Pronóstico de 7 días con desglose por hora para la Ciudad de México.

Qué hace:
  1. Lee de PostgreSQL las lecturas por hora de CDMX (observations).
  2. Evalúa 3 métodos para los próximos 1 a 7 días (horizontes), sin hacer trampa:
     cada pronóstico solo usa lo que se sabía el día en que se emitió.
       - Climatología + anomalía amortiguada (el modelo publicado, prediccion/modelo.py):
         lo normal de la temporada + la desviación de hoy, que se va apagando.
       - Persistencia: "los próximos días serán iguales a hoy" (referencia).
       - Promedio móvil de 7 días (referencia).
     Validación = 4 semanas anteriores a la prueba; prueba = las últimas 4 semanas.
  3. Calcula MAE y RMSE por variable y por horizonte (día 1 a día 7) y el error por hora
     del día siguiente.
  4. Guarda en PostgreSQL (una sola transacción):
       prediction_models · training_runs · model_metrics (DAY por horizonte y HOUR)
       predictions: días de PRUEBA (horizonte 1) y el pronóstico nuevo (7 días + 168 horas)
     La evaluación de la prueba y sus métricas las calculan los SP sp_evaluar_predicciones
     y sp_calcular_metricas.

Repetirlo crea una corrida nueva (queda el historial). Es el reentrenamiento semanal.
Necesita la RDS climate-postgres encendida y credenciales de AWS vigentes.

Ejecutar desde la carpeta del proyecto:
    .\\.venv\\Scripts\\python.exe etl\\paso6_predecir.py
"""
import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import config
from comun import confirmar, id_estable, sql_texto
from prediccion import modelo as M

S = config.POSTGRES_SCHEMA
t = sql_texto
HORIZONTE = 7
DIAS_PRUEBA = 28
DIAS_VALIDACION = 28
PUBLICADO = "CLIMATOLOGY_ANOMALY"
UNIDADES = {"temperature_mean_c": "°C", "temperature_min_c": "°C", "temperature_max_c": "°C",
            "humidity_mean_pct": "%", "precipitation_total_mm": "mm", "temperature_c": "°C", "humidity_pct": "%"}
METODOS = {
    "CLIMATOLOGY_ANOMALY": dict(
        name="Climatología + anomalía amortiguada (7 días y por hora)",
        description="Normal de la temporada (mismas fechas del año anterior) + sesgo del último mes + "
                    "anomalía de los últimos 3 días que se apaga con phi^k; reparto horario con el ciclo "
                    "típico de los últimos 30 días."),
    "PERSISTENCE": dict(name="Persistencia (día anterior)",
                        description="Los próximos días serán iguales al último día real. Referencia mínima."),
    "MOVING_AVERAGE_7D": dict(name="Promedio móvil de 7 días",
                              description="Los próximos días serán como el promedio de los últimos 7 días."),
}


# --------------------------------------------------------------------------- datos
def leer_horas(destino):
    filas = destino.ejecutar(
        f"SELECT s.series_id::text, s.timezone_name, o.timestamp_utc::text, o.temperature_c::text,"
        f" o.humidity_pct::text, o.precipitation_mm::text"
        f" FROM {S}.observations o JOIN {S}.series s ON s.series_id = o.series_id"
        f" WHERE o.deleted_at_utc IS NULL AND s.location_id = {t(config.UBICACION_FIJA)}"
        f" ORDER BY o.timestamp_utc", filas=True)
    if not filas:
        return None, None, []
    horas = [dict(ts_utc=datetime.fromisoformat(ts), temperature_c=temp, humidity_pct=hum, precipitation_mm=lluvia)
             for _, _, ts, temp, hum, lluvia in filas]
    return filas[0][0], filas[0][1], horas


def d6(x):
    return str(Decimal(str(x)).quantize(Decimal("0.000001")))


def inicio_dia(dia, zona):
    return f"('{dia.isoformat()}'::date::timestamp AT TIME ZONE {t(zona)})"


def inicio_hora(local, zona):
    return f"('{local.isoformat(sep=' ')}'::timestamp AT TIME ZONE {t(zona)})"


def utc_texto(dia):
    """Medianoche local de CDMX en UTC (06:00Z) como texto ISO."""
    return (datetime.combine(dia, datetime.min.time()) - M.DESFASE).strftime("%Y-%m-%dT%H:%M:%SZ")


def sql_modelos():
    valores = ",\n".join(
        f"({t(id_estable('modelo', clave))}, {t(m['name'])}, {t(clave)}, {t(json.dumps(list(M.VARIABLES_EVALUADAS)))}, "
        f"{t(m['description'])}, true)" for clave, m in METODOS.items())
    return (f"INSERT INTO {S}.prediction_models (model_id, name, algorithm, targets, description, is_active)"
            f" VALUES\n{valores}\nON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description,"
            f" algorithm = EXCLUDED.algorithm, targets = EXCLUDED.targets, is_active = true"
            f" RETURNING model_id::text, algorithm")


def huella(modelo):
    texto = "\n".join(f"{d}|" + "|".join(f"{modelo.dias[d][v]:.6f}" for v in M.VARIABLES_EVALUADAS)
                      for d in modelo.fechas)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def fila_prediccion(corrida, sid, pronostico_id, inicio_sql, fin_sql, granularidad, p):
    return (f"({t(str(uuid.uuid4()))}, {t(corrida)}, {t(sid)}, {t(pronostico_id)}, now(), {inicio_sql}, {fin_sql},"
            f" {t(granularidad)}, {d6(p['tmin'])}, {d6(p['tmax'])}, {d6(p['tmed'])}, {d6(p['hum'])},"
            f" {d6(p['prob'])}, {d6(p['mm'])})")


def de_dia(p):
    return dict(tmin=p["temperature_min_c"], tmax=p["temperature_max_c"], tmed=p["temperature_mean_c"],
                hum=p["humidity_mean_pct"], prob=p["rain_probability_pct"], mm=p["precipitation_total_mm"])


def de_hora(h):
    return dict(tmin=h["temperature_c"], tmax=h["temperature_c"], tmed=h["temperature_c"], hum=h["humidity_pct"],
                prob=h["rain_probability_pct"], mm=h["precipitation_mm"])


# --------------------------------------------------------------------------- principal
def main():
    import paso5_etl_postgres as p5
    parser = argparse.ArgumentParser(description="Pronóstico de 7 días y por hora (CDMX)")
    parser.add_argument("--si", action="store_true", help="no pedir confirmación")
    args = parser.parse_args()

    destino = p5.conectar_postgres()
    p5.verificar_procedimientos(destino)
    sid, zona, horas = leer_horas(destino)
    destino.commit()
    if not horas:
        print(f"No hay lecturas de {config.UBICACION_FIJA} en PostgreSQL. Corre primero el paso 5.")
        destino.cerrar()
        sys.exit(1)

    modelo = M.Modelo(horas)
    ultimo = modelo.ultimo_dia()
    inicio_prueba = ultimo - timedelta(days=DIAS_PRUEBA)
    inicio_valid = inicio_prueba - timedelta(days=DIAS_VALIDACION)
    if len(modelo.fechas) < DIAS_PRUEBA + DIAS_VALIDACION + 60:
        print(f"Hay {len(modelo.fechas)} días completos; se necesitan al menos {DIAS_PRUEBA + DIAS_VALIDACION + 60}.")
        destino.cerrar()
        sys.exit(1)
    print(f"Lecturas por hora: {len(horas):,} · días completos: {len(modelo.fechas)}"
          f" ({modelo.fechas[0]} a {ultimo})")
    print(f"  entrenamiento hasta {inicio_valid - timedelta(days=1)}")
    print(f"  validación  emisiones {inicio_valid} a {inicio_prueba - timedelta(days=1)}")
    print(f"  prueba      emisiones {inicio_prueba} a {ultimo - timedelta(days=1)}\n")

    # 1) Evaluación sin trampa: phi estimado solo con el periodo de entrenamiento
    phi_final = dict(modelo.phi)
    modelo.phi = modelo.estimar_phi(inicio_valid - timedelta(days=1))
    emisiones = {
        "VALIDATION": [inicio_valid + timedelta(days=i) for i in range(DIAS_VALIDACION)],
        "TEST": [inicio_prueba + timedelta(days=i) for i in range(DIAS_PRUEBA)],
    }
    pares = {division: M.evaluar(modelo, dias, HORIZONTE) for division, dias in emisiones.items()}
    por_hora = M.evaluar_horas(modelo, emisiones["TEST"])
    prueba_h1 = [(e, e + timedelta(days=1), modelo.pronostico_dia(e, e + timedelta(days=1)))
                 for e in emisiones["TEST"] if e + timedelta(days=1) in modelo.dias]

    for division in ("VALIDATION", "TEST"):
        print(f"MAE temperatura media ({'validación' if division == 'VALIDATION' else 'prueba'}), °C por horizonte:")
        print(f"  {'Método':<40}" + "".join(f"  día {k}" for k in range(1, HORIZONTE + 1)))
        for metodo in METODOS:
            valores = [M.errores(pares[division][metodo]["temperature_mean_c"][k])[0] for k in range(1, HORIZONTE + 1)]
            print(f"  {METODOS[metodo]['name'][:40]:<40}" + "".join(f"{v:>7.2f}" if v is not None else "      -" for v in valores))
        print()
    mae_h, rmse_h, n_h = M.errores(por_hora["temperature_c"])
    print(f"Por hora (día siguiente, prueba): MAE {mae_h:.2f} °C · RMSE {rmse_h:.2f} °C en {n_h} horas\n")

    # 2) Pronóstico nuevo con toda la historia
    modelo.phi = phi_final
    futuro = modelo.pronostico()
    print("Pronóstico publicado (modelo de climatología + anomalía):")
    for d in futuro:
        print(f"  {d['dia']:%a %d/%m}  mín {d['temperature_min_c']:5.1f}  máx {d['temperature_max_c']:5.1f}"
              f"  lluvia {d['rain_probability_pct']:5.1f} % / {d['precipitation_total_mm']:.1f} mm"
              f"  humedad {d['humidity_mean_pct']:.0f} %")
    filas_horas = sum(len(d["horas"]) for d in futuro)
    if not confirmar(f"\n¿Guardar 3 modelos, sus métricas por horizonte, {len(prueba_h1)} predicciones de prueba y"
                     f" el pronóstico nuevo ({len(futuro)} días + {filas_horas} horas) en PostgreSQL?", args.si):
        print("Cancelado. No se escribió nada.")
        destino.cerrar()
        return

    # 3) Guardar (una transacción)
    try:
        version = datetime.now(timezone.utc).strftime("v%Y%m%d-%H%M%S")
        modelos = {alg: mid for mid, alg in destino.ejecutar(sql_modelos(), filas=destino.directo)} or {
            k: id_estable("modelo", k) for k in METODOS}
        destino.ejecutar(f"UPDATE {S}.prediction_models SET is_active = false"
                         f" WHERE algorithm NOT IN ({', '.join(t(k) for k in METODOS)})")
        dataset = huella(modelo)
        corridas = {}
        horas_entreno = sum(1 for h in modelo.horas if h["local"].date() < inicio_valid)
        for metodo in METODOS:
            corridas[metodo] = str(uuid.uuid4())
            hiper = {"horizonte_dias": HORIZONTE, "dias_validacion": DIAS_VALIDACION, "dias_prueba": DIAS_PRUEBA,
                     "publicado": metodo == PUBLICADO}
            if metodo == PUBLICADO:
                hiper.update(phi=phi_final, ventana_dias=M.VENTANA, dias_sesgo=M.DIAS_SESGO,
                             dias_anomalia=M.DIAS_ANOMALIA, dias_perfil_horario=M.DIAS_PERFIL)
            caracteristicas = (["normal_temporada_anio_anterior", "sesgo_30_dias", "anomalia_3_dias", "perfil_horario_30_dias"]
                               if metodo == PUBLICADO else ["historial_diario"])
            destino.ejecutar(
                f"INSERT INTO {S}.training_runs (training_run_id, model_id, version, status, started_at_utc,"
                f" finished_at_utc, data_from_utc, data_until_utc, train_until_utc, validation_until_utc,"
                f" test_until_utc, record_count, dataset_hash, dataset_uri, artifact_uri, hyperparameters, features)"
                f" VALUES ({t(corridas[metodo])}, {t(modelos[metodo])}, {t(version)}, 'SUCCEEDED', now(),"
                f" clock_timestamp(), {t(utc_texto(modelo.fechas[0]))}, {t(utc_texto(ultimo + timedelta(days=1)))},"
                f" {t(utc_texto(inicio_valid))}, {t(utc_texto(inicio_prueba))}, {t(utc_texto(ultimo + timedelta(days=1)))},"
                f" {max(horas_entreno, 1)}, {t(dataset)},"
                f" {t(f'postgresql://{config.POSTGRES_DB}/{S}.observations?location_id={config.UBICACION_FIJA}')},"
                f" NULL, {t(json.dumps(hiper, ensure_ascii=False))}, {t(json.dumps(caracteristicas))})")

        metricas = []
        for division, resultado in pares.items():
            for metodo in METODOS:
                for var in M.VARIABLES_EVALUADAS:
                    for k, lista in resultado[metodo][var].items():
                        if metodo == PUBLICADO and division == "TEST" and k == 1:
                            continue   # la calculan los SP 9 y 10 desde las predicciones guardadas
                        mae, rmse, n = M.errores(lista)
                        if not n:
                            continue
                        dias = [e + timedelta(days=k) for e in emisiones[division]]
                        for nombre, valor in (("MAE", mae), ("RMSE", rmse)):
                            metricas.append(
                                f"({t(str(uuid.uuid4()))}, {t(corridas[metodo])}, {t(sid)}, {t(var)}, {t(nombre)},"
                                f" {t(division)}, {1440 * k}, 'DAY', {t(utc_texto(min(dias)))},"
                                f" {t(utc_texto(max(dias) + timedelta(days=1)))}, {d6(valor)}, {n}, {t(UNIDADES[var])})")
        for var, lista in por_hora.items():
            mae, rmse, n = M.errores(lista)
            if n:
                for nombre, valor in (("MAE", mae), ("RMSE", rmse)):
                    metricas.append(
                        f"({t(str(uuid.uuid4()))}, {t(corridas[PUBLICADO])}, {t(sid)}, {t(var)}, {t(nombre)},"
                        f" 'TEST', 1440, 'HOUR', {t(utc_texto(inicio_prueba + timedelta(days=1)))},"
                        f" {t(utc_texto(ultimo + timedelta(days=1)))}, {d6(valor)}, {n}, {t(UNIDADES[var])})")
        destino.ejecutar(
            f"INSERT INTO {S}.model_metrics (metric_id, training_run_id, series_id, variable_name, metric_name,"
            f" dataset_split, horizon_minutes, granularity, starts_at_utc, ends_at_utc, value, sample_count, unit)"
            f" VALUES\n" + ",\n".join(metricas))

        corrida_prueba, corrida_futuro = str(uuid.uuid4()), str(uuid.uuid4())
        predicciones = [fila_prediccion(corridas[PUBLICADO], sid, corrida_prueba, inicio_dia(dia, zona),
                                        inicio_dia(dia + timedelta(days=1), zona), "DAY", de_dia(p))
                        for _, dia, p in prueba_h1]
        for d in futuro:
            predicciones.append(fila_prediccion(corridas[PUBLICADO], sid, corrida_futuro, inicio_dia(d["dia"], zona),
                                                inicio_dia(d["dia"] + timedelta(days=1), zona), "DAY", de_dia(d)))
            for h in d["horas"]:
                predicciones.append(fila_prediccion(corridas[PUBLICADO], sid, corrida_futuro,
                                                    inicio_hora(h["local"], zona),
                                                    inicio_hora(h["local"] + timedelta(hours=1), zona), "HOUR", de_hora(h)))
        for i in range(0, len(predicciones), 200):
            destino.ejecutar(
                f"INSERT INTO {S}.predictions (prediction_id, training_run_id, series_id, forecast_run_id,"
                f" issued_at_utc, target_start_utc, target_end_utc, granularity, temperature_min_c, temperature_max_c,"
                f" temperature_mean_c, humidity_pct, rain_probability_pct, rainfall_mm) VALUES\n"
                + ",\n".join(predicciones[i:i + 200]))
        (evaluadas,) = p5.llamar(destino, "sp_evaluar_predicciones", t(corrida_prueba), "NULL")
        (filas_metricas,) = p5.llamar(destino, "sp_calcular_metricas", t(corrida_prueba), "'TEST'", "NULL")
        destino.commit()
    except Exception as error:
        destino.rollback()
        destino.cerrar()
        print(f"\nNo se guardó nada (se revirtió): {type(error).__name__}: {error}")
        sys.exit(1)
    destino.cerrar()
    print(f"\nGuardado ({version}): 3 corridas de entrenamiento, {len(metricas)} métricas MAE/RMSE por horizonte,"
          f" {len(prueba_h1)} predicciones de prueba y el pronóstico nuevo ({len(futuro)} días + {filas_horas} horas).")
    print(f"Stored procedures: sp_evaluar_predicciones evaluó {int(evaluadas)} días de prueba;"
          f" sp_calcular_metricas guardó {int(filas_metricas)} métricas del día 1.")
    print(f"Corrida del pronóstico (forecast_run_id): {corrida_futuro}")
    print("Revisa los resultados con etl/consultas_prediccion.sql en DBeaver.")


if __name__ == "__main__":
    main()
