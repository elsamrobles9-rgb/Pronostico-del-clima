-- AWS Clima V2 · Consultas de la predicción (PostgreSQL, base climate_analytics)
-- En DBeaver: coloca el cursor dentro de una consulta y presiona Ctrl+Enter. Todas son de solo lectura.

-- 1. ¿Qué método se equivoca menos? MAE y RMSE (promedio de las 5 ciudades), en validación y prueba
SELECT m.name AS metodo, mm.dataset_split AS conjunto, mm.variable_name AS variable,
       round(avg(mm.value) FILTER (WHERE mm.metric_name = 'MAE'), 2) AS mae,
       round(avg(mm.value) FILTER (WHERE mm.metric_name = 'RMSE'), 2) AS rmse,
       mm.unit AS unidad, sum(mm.sample_count) FILTER (WHERE mm.metric_name = 'MAE') AS dias_evaluados
FROM climate_analytics.model_metrics mm
JOIN climate_analytics.training_runs tr ON tr.training_run_id = mm.training_run_id
JOIN climate_analytics.prediction_models m ON m.model_id = tr.model_id
WHERE tr.version = (SELECT max(version) FROM climate_analytics.training_runs)
  AND mm.variable_name IN ('temperature_mean_c', 'temperature_max_c', 'precipitation_total_mm')
GROUP BY m.name, mm.dataset_split, mm.variable_name, mm.unit
ORDER BY mm.variable_name, mm.dataset_split DESC, mae;

-- 2. Pronóstico de los próximos 7 días por ciudad (última corrida)
SELECT s.location_name AS ciudad, (p.target_start_utc AT TIME ZONE s.timezone_name)::date AS dia,
       round(p.temperature_min_c, 1) AS t_min, round(p.temperature_mean_c, 1) AS t_media,
       round(p.temperature_max_c, 1) AS t_max, round(p.humidity_pct) AS humedad_pct,
       round(p.rain_probability_pct) AS prob_lluvia_pct, round(p.rainfall_mm, 1) AS lluvia_mm
FROM climate_analytics.predictions p
JOIN climate_analytics.series s ON s.series_id = p.series_id
WHERE p.forecast_run_id = (
  SELECT p2.forecast_run_id FROM climate_analytics.predictions p2
  WHERE NOT EXISTS (SELECT 1 FROM climate_analytics.prediction_evaluations e WHERE e.prediction_id = p2.prediction_id)
  ORDER BY p2.issued_at_utc DESC LIMIT 1)
ORDER BY ciudad, dia;

-- 3. Predicho vs. real en los días de prueba (el modelo NO vio estos días al entrenar)
SELECT s.location_name AS ciudad, (e.starts_at_utc AT TIME ZONE s.timezone_name)::date AS dia,
       round(p.temperature_mean_c, 1) AS t_media_predicha,
       round((e.actual_values->>'temperature_mean_c')::numeric, 1) AS t_media_real,
       round((e.errors->>'temperature_mean_c')::numeric, 2) AS error_c,
       round(p.rain_probability_pct) AS prob_lluvia_pct, e.actual_values->>'llovio' AS llovio
FROM climate_analytics.prediction_evaluations e
JOIN climate_analytics.predictions p ON p.prediction_id = e.prediction_id
JOIN climate_analytics.series s ON s.series_id = e.series_id
WHERE p.forecast_run_id = (
  SELECT p2.forecast_run_id FROM climate_analytics.predictions p2
  JOIN climate_analytics.prediction_evaluations e2 ON e2.prediction_id = p2.prediction_id
  ORDER BY p2.issued_at_utc DESC LIMIT 1)
ORDER BY ciudad, dia;

-- 4. Resumen de la evaluación: error medio de temperatura y % de aciertos de "¿lloverá?"
SELECT s.location_name AS ciudad, count(*) AS dias_evaluados,
       round(avg((e.errors->>'temperature_mean_c')::numeric), 2) AS error_medio_t_media_c,
       round(100.0 * avg(CASE WHEN (e.errors->>'acierto_lluvia')::boolean THEN 1 ELSE 0 END), 0) AS aciertos_lluvia_pct
FROM climate_analytics.prediction_evaluations e
JOIN climate_analytics.series s ON s.series_id = e.series_id
WHERE e.evaluated_at_utc = (SELECT max(evaluated_at_utc) FROM climate_analytics.prediction_evaluations)
GROUP BY s.location_name
ORDER BY s.location_name;

-- 5. Para graficar en DBeaver (pestaña "Gráfico"): temperatura real vs. predicha en Mérida
SELECT (e.starts_at_utc AT TIME ZONE s.timezone_name)::date AS dia,
       round((e.actual_values->>'temperature_mean_c')::numeric, 2) AS real,
       round(p.temperature_mean_c, 2) AS predicha
FROM climate_analytics.prediction_evaluations e
JOIN climate_analytics.predictions p ON p.prediction_id = e.prediction_id
JOIN climate_analytics.series s ON s.series_id = e.series_id
WHERE s.location_id = 'LOC-MID'
  AND e.evaluated_at_utc = (SELECT max(evaluated_at_utc) FROM climate_analytics.prediction_evaluations)
ORDER BY dia;
