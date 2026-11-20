-- AWS Clima V2 · Consultas de la predicción de la CDMX (PostgreSQL, base climate_analytics)
-- En DBeaver: coloca el cursor dentro de una consulta y presiona Ctrl+Enter. Todas son de solo lectura.

-- 1. ¿Qué método se equivoca menos según cuántos días adelante se pronostica?
--    MAE de la temperatura media por horizonte (día 1 a día 7), en validación y prueba.
SELECT m.name AS metodo, mm.dataset_split AS conjunto,
       round(max(mm.value) FILTER (WHERE mm.horizon_minutes = 1440), 2) AS dia_1,
       round(max(mm.value) FILTER (WHERE mm.horizon_minutes = 2880), 2) AS dia_2,
       round(max(mm.value) FILTER (WHERE mm.horizon_minutes = 4320), 2) AS dia_3,
       round(max(mm.value) FILTER (WHERE mm.horizon_minutes = 5760), 2) AS dia_4,
       round(max(mm.value) FILTER (WHERE mm.horizon_minutes = 7200), 2) AS dia_5,
       round(max(mm.value) FILTER (WHERE mm.horizon_minutes = 8640), 2) AS dia_6,
       round(max(mm.value) FILTER (WHERE mm.horizon_minutes = 10080), 2) AS dia_7
FROM climate_analytics.model_metrics mm
JOIN climate_analytics.training_runs tr ON tr.training_run_id = mm.training_run_id
JOIN climate_analytics.prediction_models m ON m.model_id = tr.model_id
WHERE tr.version = (SELECT max(version) FROM climate_analytics.training_runs)
  AND mm.variable_name = 'temperature_mean_c' AND mm.metric_name = 'MAE' AND mm.granularity = 'DAY'
GROUP BY m.name, mm.dataset_split
ORDER BY mm.dataset_split DESC, dia_7;

-- 2. MAE y RMSE de todas las variables del modelo publicado (prueba, promedio de los 7 días)
SELECT mm.variable_name AS variable, mm.unit AS unidad,
       round(avg(mm.value) FILTER (WHERE mm.metric_name = 'MAE'), 2) AS mae,
       round(avg(mm.value) FILTER (WHERE mm.metric_name = 'RMSE'), 2) AS rmse,
       mm.granularity AS granularidad
FROM climate_analytics.model_metrics mm
JOIN climate_analytics.training_runs tr ON tr.training_run_id = mm.training_run_id
WHERE tr.version = (SELECT max(version) FROM climate_analytics.training_runs)
  AND tr.hyperparameters->>'publicado' = 'true' AND mm.dataset_split = 'TEST'
GROUP BY mm.variable_name, mm.unit, mm.granularity
ORDER BY mm.granularity, mm.variable_name;

-- 3. Pronóstico publicado: los 7 días (última corrida)
SELECT (p.target_start_utc AT TIME ZONE s.timezone_name)::date AS dia,
       round(p.temperature_min_c, 1) AS t_min, round(p.temperature_mean_c, 1) AS t_media,
       round(p.temperature_max_c, 1) AS t_max, round(p.humidity_pct) AS humedad_pct,
       round(p.rain_probability_pct) AS prob_lluvia_pct, round(p.rainfall_mm, 1) AS lluvia_mm
FROM climate_analytics.predictions p
JOIN climate_analytics.series s ON s.series_id = p.series_id
WHERE p.granularity = 'DAY' AND p.forecast_run_id = (
  SELECT forecast_run_id FROM climate_analytics.predictions WHERE granularity = 'HOUR'
  ORDER BY issued_at_utc DESC LIMIT 1)
ORDER BY dia;

-- 4. Pronóstico hora por hora del primer día (última corrida). Pestaña "Gráfico" para verlo.
SELECT (p.target_start_utc AT TIME ZONE s.timezone_name) AS hora_local,
       round(p.temperature_mean_c, 1) AS temperatura, round(p.humidity_pct) AS humedad_pct,
       round(p.rain_probability_pct) AS prob_lluvia_pct, round(p.rainfall_mm, 2) AS lluvia_mm
FROM climate_analytics.predictions p
JOIN climate_analytics.series s ON s.series_id = p.series_id
WHERE p.granularity = 'HOUR' AND p.forecast_run_id = (
  SELECT forecast_run_id FROM climate_analytics.predictions WHERE granularity = 'HOUR'
  ORDER BY issued_at_utc DESC LIMIT 1)
ORDER BY hora_local
LIMIT 24;

-- 5. Predicho vs. real en los días de prueba (el modelo NO vio estos días al pronosticar)
SELECT (e.starts_at_utc AT TIME ZONE s.timezone_name)::date AS dia,
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
ORDER BY dia;

-- 6. Resumen de la evaluación: error medio de temperatura y % de aciertos de "¿lloverá?"
SELECT count(*) AS dias_evaluados,
       round(avg((e.errors->>'temperature_mean_c')::numeric), 2) AS error_medio_t_media_c,
       round(100.0 * avg(CASE WHEN (e.errors->>'acierto_lluvia')::boolean THEN 1 ELSE 0 END), 0) AS aciertos_lluvia_pct
FROM climate_analytics.prediction_evaluations e
WHERE e.evaluated_at_utc = (SELECT max(evaluated_at_utc) FROM climate_analytics.prediction_evaluations);

-- 7. Para graficar (pestaña "Gráfico"): temperatura real vs. predicha en la CDMX
SELECT (e.starts_at_utc AT TIME ZONE s.timezone_name)::date AS dia,
       round((e.actual_values->>'temperature_mean_c')::numeric, 2) AS real,
       round(p.temperature_mean_c, 2) AS predicha
FROM climate_analytics.prediction_evaluations e
JOIN climate_analytics.predictions p ON p.prediction_id = e.prediction_id
JOIN climate_analytics.series s ON s.series_id = e.series_id
WHERE s.location_id = 'LOC-CDMX'
  AND e.evaluated_at_utc = (SELECT max(evaluated_at_utc) FROM climate_analytics.prediction_evaluations)
ORDER BY dia;
