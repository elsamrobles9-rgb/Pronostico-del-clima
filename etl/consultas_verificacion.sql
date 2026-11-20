-- AWS Clima V2 · Consultas de verificación del ETL (PostgreSQL, base climate_analytics)
-- En DBeaver: coloca el cursor dentro de una consulta y presiona Ctrl+Enter.
-- Todas son de solo lectura.

-- 1. ¿Cuántas filas hay en cada tabla? (después de la carga: 5 series, 10,000 observaciones)
SELECT 'series' AS tabla, count(*) AS filas FROM climate_analytics.series
UNION ALL SELECT 'observations (activas)', count(*) FROM climate_analytics.observations WHERE deleted_at_utc IS NULL
UNION ALL SELECT 'observations (borradas)', count(*) FROM climate_analytics.observations WHERE deleted_at_utc IS NOT NULL
UNION ALL SELECT 'climate_aggregates', count(*) FROM climate_analytics.climate_aggregates
UNION ALL SELECT 'etl_batches', count(*) FROM climate_analytics.etl_batches;

-- 2. Historial de lotes del ETL: qué se cargó, cuándo y con qué resultado
SELECT started_at_utc, finished_at_utc - started_at_utc AS duracion, status, read_count AS leidos,
       inserted_count AS insertados, updated_count AS actualizados, skipped_count AS omitidos,
       rejected_count AS rechazados, end_checkpoint->>'modo' AS modo, error_summary
FROM climate_analytics.etl_batches
ORDER BY started_at_utc DESC;

-- 3. Resumen por ciudad: periodo cubierto y promedios
SELECT s.location_name AS ubicacion, count(*) AS observaciones,
       min(o.timestamp_utc) AS desde, max(o.timestamp_utc) AS hasta,
       round(avg(o.temperature_c), 1) AS temp_media_c, round(max(o.temperature_c), 1) AS temp_max_c,
       round(avg(o.humidity_pct), 1) AS humedad_media, round(sum(o.precipitation_mm), 1) AS lluvia_total_mm
FROM climate_analytics.observations o
JOIN climate_analytics.series s ON s.series_id = o.series_id
WHERE o.deleted_at_utc IS NULL
GROUP BY s.location_name
ORDER BY s.location_name;

-- 4. Trazabilidad: un mismo registro en PostgreSQL. Compara record_id, version y content_hash
--    con el ítem de DynamoDB (consola > ClimateReadingsV2 > Explore items > LOC-CDMX).
SELECT s.location_id, o.timestamp_utc, o.source_record_id AS record_id, o.source_version AS version,
       o.content_hash, o.temperature_c, o.humidity_pct, o.condition_id, o.batch_id
FROM climate_analytics.observations o
JOIN climate_analytics.series s ON s.series_id = o.series_id
WHERE s.location_id = 'LOC-CDMX'
ORDER BY o.timestamp_utc
LIMIT 5;

-- 5. Los 10 días más lluviosos (agregados diarios, día local de cada ciudad)
SELECT s.location_name AS ubicacion, (a.starts_at_utc AT TIME ZONE s.timezone_name)::date AS dia_local,
       a.precipitation_total_mm AS lluvia_mm, a.temperature_min_c AS t_min, a.temperature_max_c AS t_max,
       a.valid_samples AS horas, a.coverage_pct AS cobertura
FROM climate_analytics.climate_aggregates a
JOIN climate_analytics.series s ON s.series_id = a.series_id
WHERE a.granularity = 'DAY'
ORDER BY a.precipitation_total_mm DESC
LIMIT 10;

-- 6. Temperatura media diaria por ciudad (para graficar en DBeaver: pestaña "Gráfico" del resultado)
SELECT (a.starts_at_utc AT TIME ZONE s.timezone_name)::date AS dia_local, s.location_id,
       a.temperature_mean_c AS temp_media_c
FROM climate_analytics.climate_aggregates a
JOIN climate_analytics.series s ON s.series_id = a.series_id
WHERE a.granularity = 'DAY' AND a.coverage_pct = 100
ORDER BY dia_local, s.location_id;

-- 7. Los 10 stored procedures instalados (y las 2 funciones auxiliares que usan)
SELECT routine_type AS tipo, routine_name AS nombre
FROM information_schema.routines
WHERE routine_schema = 'climate_analytics'
ORDER BY routine_type DESC, routine_name;

-- 8. Cola de registros anómalos: lo que sp_validar_staging rechazó y por qué
SELECT cargado_at_utc, location_id, timestamp_utc, motivo_rechazo
FROM climate_analytics.stg_observations
WHERE NOT es_valido
ORDER BY cargado_at_utc DESC, fila
LIMIT 100;

-- 9. Condición del clima: la que dio la fuente vs. la que calcula la regla (fn_clasificar_condicion)
SELECT o.condition_id AS segun_la_fuente,
       climate_analytics.fn_clasificar_condicion(o.precipitation_mm, o.cloud_cover_pct, o.visibility_km,
                                                o.humidity_pct, o.temperature_c) AS segun_la_regla,
       count(*) AS registros
FROM climate_analytics.observations o
WHERE o.deleted_at_utc IS NULL
GROUP BY 1, 2
ORDER BY registros DESC;

-- 10. Porcentaje de coincidencia entre la condición de la fuente y la calculada
SELECT round(100.0 * avg(CASE WHEN o.condition_id = climate_analytics.fn_clasificar_condicion(
           o.precipitation_mm, o.cloud_cover_pct, o.visibility_km, o.humidity_pct, o.temperature_c)
       THEN 1 ELSE 0 END), 1) AS coincidencia_pct, count(*) AS registros
FROM climate_analytics.observations o
WHERE o.deleted_at_utc IS NULL;
