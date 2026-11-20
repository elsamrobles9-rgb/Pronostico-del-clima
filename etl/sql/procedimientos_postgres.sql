-- =====================================================================================
-- AWS Clima V2 · 10 Stored Procedures de la capa analítica (PostgreSQL, climate_analytics)
-- Transformación, limpieza, agregación, evaluación de predicciones y métricas.
--
-- Cómo instalar: DBeaver -> conexión PostgreSQL -> abrir este archivo -> Alt+X (ejecutar script).
-- Se puede ejecutar más de una vez: usa CREATE OR REPLACE e IF NOT EXISTS.
-- Requiere PostgreSQL 13 o superior (gen_random_uuid).
-- =====================================================================================

-- -------------------------------------------------------------------------------------
-- Tabla de staging: entrada del ETL y COLA DE REGISTROS ANÓMALOS.
-- Los datos llegan aquí tal como vienen de DynamoDB (texto y números sin validar).
-- Los procedimientos los limpian; los válidos pasan a observations y las filas
-- rechazadas se quedan aquí con su motivo para revisarlas.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS climate_analytics.stg_observations (
    batch_id uuid NOT NULL,
    fila int NOT NULL,
    location_id text, station_id text, timestamp_utc text,
    source_record_id text, source_version int, content_hash text, last_event_id text,
    source_id text, condition_id text, condition_origin text, condition_rule_version text,
    temperature_c numeric, feels_like_c numeric, humidity_pct numeric, pressure_hpa numeric,
    wind_speed_kmh numeric, wind_direction text, precipitation_mm numeric,
    precipitation_period_minutes int, cloud_cover_pct numeric, visibility_km numeric,
    uv_index numeric, dew_point_c numeric, received_at_utc text, updated_at_utc text,
    -- columnas que llenan los procedimientos
    series_id uuid, ts_utc timestamptz, recibido_utc timestamptz, actualizado_utc timestamptz,
    es_valido boolean, motivo_rechazo text,
    cargado_at_utc timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT stg_observations_pkey PRIMARY KEY (batch_id, fila)
);

-- -------------------------------------------------------------------------------------
-- Funciones auxiliares (no son procedimientos; las usan los procedimientos)
-- -------------------------------------------------------------------------------------
-- Convierte texto ISO 8601 a fecha sin fallar: si no es válido devuelve NULL.
CREATE OR REPLACE FUNCTION climate_analytics.fn_fecha_segura(p_texto text)
RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF p_texto IS NULL OR p_texto !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$' THEN
        RETURN NULL;
    END IF;
    RETURN p_texto::timestamptz;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

-- Regla de clasificación de la condición del clima a partir de variables numéricas (reglas-v1).
CREATE OR REPLACE FUNCTION climate_analytics.fn_clasificar_condicion(
    p_precipitacion_mm numeric, p_nubosidad_pct numeric, p_visibilidad_km numeric,
    p_humedad_pct numeric, p_temperatura_c numeric)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN p_precipitacion_mm >= 0.5 AND p_temperatura_c <= 0 THEN 'COND-SNOW'
        WHEN p_precipitacion_mm >= 0.5 THEN 'COND-RAIN'
        WHEN p_precipitacion_mm >= 0.1 THEN 'COND-DRIZZLE'
        WHEN p_visibilidad_km < 1 AND p_humedad_pct >= 95 THEN 'COND-FOG'
        WHEN p_nubosidad_pct >= 85 THEN 'COND-OVERCAST'
        WHEN p_nubosidad_pct >= 25 THEN 'COND-PARTLY-CLOUDY'
        ELSE 'COND-CLEAR'
    END
$$;

-- =====================================================================================
-- SP 1 · Abre un lote del ETL (estado RUNNING) y devuelve su identificador.
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_iniciar_lote(
    p_modo text, p_desde text, p_pipeline text, INOUT p_batch_id uuid)
LANGUAGE plpgsql AS $$
BEGIN
    IF p_modo NOT IN ('completo', 'incremental') THEN
        RAISE EXCEPTION 'Modo no válido: % (use completo o incremental)', p_modo;
    END IF;
    p_batch_id := gen_random_uuid();
    INSERT INTO climate_analytics.etl_batches
        (batch_id, started_at_utc, status, start_checkpoint, pipeline_version)
    VALUES (p_batch_id, clock_timestamp(), 'RUNNING',
            jsonb_build_object('modo', p_modo, 'desde_updated_at_utc', p_desde), p_pipeline);
END $$;

-- =====================================================================================
-- SP 2 · Sincroniza las series (ubicación + estación) con el catálogo de MySQL.
-- p_catalogo = arreglo JSON: [{location_id, station_id, location_name, station_name,
--              latitude, longitude, altitude_m, timezone_name, version}, ...]
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_sincronizar_series(
    p_catalogo jsonb, INOUT p_filas int)
LANGUAGE plpgsql AS $$
DECLARE
    v_zona text;
BEGIN
    IF p_catalogo IS NULL OR jsonb_array_length(p_catalogo) = 0 THEN
        RAISE EXCEPTION 'El catálogo de MySQL no tiene ubicaciones activas';
    END IF;
    SELECT c->>'timezone_name' INTO v_zona
    FROM jsonb_array_elements(p_catalogo) c
    WHERE NOT EXISTS (SELECT 1 FROM pg_timezone_names z WHERE z.name = c->>'timezone_name')
    LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'Zona horaria desconocida en el catálogo: %', v_zona;
    END IF;
    INSERT INTO climate_analytics.series AS s
        (series_id, location_id, station_id, location_name, station_name, latitude, longitude,
         altitude_m, timezone_name, catalog_version, synced_at_utc)
    SELECT gen_random_uuid(), c->>'location_id', c->>'station_id', c->>'location_name',
           c->>'station_name', (c->>'latitude')::numeric, (c->>'longitude')::numeric,
           (c->>'altitude_m')::numeric, c->>'timezone_name', (c->>'version')::int, clock_timestamp()
    FROM jsonb_array_elements(p_catalogo) c
    ON CONFLICT (location_id) DO UPDATE SET
        station_id = EXCLUDED.station_id, location_name = EXCLUDED.location_name,
        station_name = EXCLUDED.station_name, latitude = EXCLUDED.latitude,
        longitude = EXCLUDED.longitude, altitude_m = EXCLUDED.altitude_m,
        timezone_name = EXCLUDED.timezone_name, catalog_version = EXCLUDED.catalog_version,
        synced_at_utc = EXCLUDED.synced_at_utc;
    GET DIAGNOSTICS p_filas = ROW_COUNT;
END $$;

-- =====================================================================================
-- SP 3 · LIMPIEZA: valida cada fila del staging y anota el motivo de rechazo.
-- Reglas (las mismas de contract.py y de los CHECK de observations):
--   catálogo, datos faltantes, formatos, rangos, punto de rocío <= temperatura,
--   minuto exacto, máximo 6 decimales y duplicados dentro del lote.
-- p_fuentes / p_condiciones = IDs activos en MySQL (data_sources / weather_conditions).
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_validar_staging(
    p_batch_id uuid, p_fuentes text[], p_condiciones text[],
    INOUT p_validos int, INOUT p_rechazados int)
LANGUAGE plpgsql AS $$
BEGIN
    -- 0) Preparar: tipos y serie
    UPDATE climate_analytics.stg_observations st SET
        es_valido = NULL, motivo_rechazo = NULL,
        ts_utc = climate_analytics.fn_fecha_segura(st.timestamp_utc),
        recibido_utc = climate_analytics.fn_fecha_segura(st.received_at_utc),
        actualizado_utc = climate_analytics.fn_fecha_segura(st.updated_at_utc),
        series_id = (SELECT s.series_id FROM climate_analytics.series s WHERE s.location_id = st.location_id)
    WHERE st.batch_id = p_batch_id;

    -- 1) Catálogo
    UPDATE climate_analytics.stg_observations st SET motivo_rechazo =
        CASE
            WHEN st.series_id IS NULL THEN 'La ubicación ' || coalesce(st.location_id, '(vacía)') || ' no está activa en el catálogo de MySQL'
            WHEN st.station_id IS DISTINCT FROM (SELECT s.station_id FROM climate_analytics.series s WHERE s.series_id = st.series_id)
                THEN 'La estación ' || coalesce(st.station_id, '(vacía)') || ' no corresponde a ' || st.location_id
            WHEN st.source_id IS NULL OR NOT (st.source_id = ANY (p_fuentes))
                THEN 'La fuente ' || coalesce(st.source_id, '(vacía)') || ' no está en data_sources'
            WHEN st.condition_id IS NOT NULL AND NOT (st.condition_id = ANY (p_condiciones))
                THEN 'La condición ' || st.condition_id || ' no está en weather_conditions'
        END
    WHERE st.batch_id = p_batch_id;

    -- 2) Datos faltantes (no se permiten registros incompletos)
    UPDATE climate_analytics.stg_observations st SET motivo_rechazo = 'Dato faltante: ' || concat_ws(', ',
            CASE WHEN st.temperature_c IS NULL THEN 'temperature_c' END,
            CASE WHEN st.feels_like_c IS NULL THEN 'feels_like_c' END,
            CASE WHEN st.humidity_pct IS NULL THEN 'humidity_pct' END,
            CASE WHEN st.pressure_hpa IS NULL THEN 'pressure_hpa' END,
            CASE WHEN st.wind_speed_kmh IS NULL THEN 'wind_speed_kmh' END,
            CASE WHEN st.wind_direction IS NULL THEN 'wind_direction' END,
            CASE WHEN st.precipitation_mm IS NULL THEN 'precipitation_mm' END,
            CASE WHEN st.precipitation_period_minutes IS NULL THEN 'precipitation_period_minutes' END,
            CASE WHEN st.cloud_cover_pct IS NULL THEN 'cloud_cover_pct' END,
            CASE WHEN st.visibility_km IS NULL THEN 'visibility_km' END,
            CASE WHEN st.uv_index IS NULL THEN 'uv_index' END,
            CASE WHEN st.dew_point_c IS NULL THEN 'dew_point_c' END,
            CASE WHEN st.source_record_id IS NULL THEN 'record_id' END,
            CASE WHEN st.source_version IS NULL THEN 'version' END)
    WHERE st.batch_id = p_batch_id AND st.motivo_rechazo IS NULL
      AND num_nulls(st.temperature_c, st.feels_like_c, st.humidity_pct, st.pressure_hpa, st.wind_speed_kmh,
                    st.wind_direction, st.precipitation_mm, st.precipitation_period_minutes, st.cloud_cover_pct,
                    st.visibility_km, st.uv_index, st.dew_point_c, st.source_record_id, st.source_version) > 0;

    -- 3) Formatos
    UPDATE climate_analytics.stg_observations st SET motivo_rechazo =
        CASE
            WHEN st.ts_utc IS NULL THEN 'Fecha de medición no válida: ' || coalesce(st.timestamp_utc, '(vacía)')
            WHEN st.recibido_utc IS NULL OR st.actualizado_utc IS NULL THEN 'Fecha de auditoría no válida'
            WHEN coalesce(st.last_event_id, '') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN 'transaction_id no es un UUID válido'
            WHEN coalesce(st.content_hash, '') !~ '^[0-9a-f]{64}$' THEN 'content_hash no es un SHA-256 válido'
            WHEN st.wind_direction NOT IN ('N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW')
                THEN 'Dirección del viento no válida: ' || st.wind_direction
            WHEN st.condition_origin IS NOT NULL AND st.condition_origin NOT IN ('SOURCE', 'CALCULATED')
                THEN 'condition_origin no válido'
        END
    WHERE st.batch_id = p_batch_id AND st.motivo_rechazo IS NULL;

    -- 4) Rangos y reglas físicas
    UPDATE climate_analytics.stg_observations st SET motivo_rechazo =
        CASE
            WHEN extract(second FROM st.ts_utc) <> 0 THEN 'La fecha debe ser a minuto exacto (sin segundos)'
            WHEN st.temperature_c NOT BETWEEN -100 AND 70 THEN 'temperature_c fuera de rango (-100 a 70)'
            WHEN st.feels_like_c NOT BETWEEN -120 AND 100 THEN 'feels_like_c fuera de rango (-120 a 100)'
            WHEN st.humidity_pct NOT BETWEEN 0 AND 100 THEN 'humidity_pct fuera de rango (0 a 100)'
            WHEN st.pressure_hpa NOT BETWEEN 100 AND 1200 THEN 'pressure_hpa fuera de rango (100 a 1200)'
            WHEN st.wind_speed_kmh NOT BETWEEN 0 AND 500 THEN 'wind_speed_kmh fuera de rango (0 a 500)'
            WHEN st.precipitation_mm NOT BETWEEN 0 AND 5000 THEN 'precipitation_mm fuera de rango (0 a 5000)'
            WHEN st.precipitation_period_minutes NOT BETWEEN 1 AND 1440 THEN 'precipitation_period_minutes fuera de rango (1 a 1440)'
            WHEN st.cloud_cover_pct NOT BETWEEN 0 AND 100 THEN 'cloud_cover_pct fuera de rango (0 a 100)'
            WHEN st.visibility_km NOT BETWEEN 0 AND 1000 THEN 'visibility_km fuera de rango (0 a 1000)'
            WHEN st.uv_index < 0 OR st.uv_index > 999999 THEN 'uv_index fuera de rango'
            WHEN st.dew_point_c NOT BETWEEN -120 AND 70 THEN 'dew_point_c fuera de rango (-120 a 70)'
            WHEN st.dew_point_c > st.temperature_c THEN 'El punto de rocío supera la temperatura'
            WHEN st.source_version <= 0 THEN 'version debe ser mayor que 0'
            WHEN greatest(scale(st.temperature_c), scale(st.feels_like_c), scale(st.humidity_pct),
                          scale(st.pressure_hpa), scale(st.wind_speed_kmh), scale(st.precipitation_mm),
                          scale(st.cloud_cover_pct), scale(st.visibility_km), scale(st.uv_index),
                          scale(st.dew_point_c)) > 6 THEN 'Más de 6 decimales en una medición'
        END
    WHERE st.batch_id = p_batch_id AND st.motivo_rechazo IS NULL;

    -- 5) Duplicados dentro del lote: se queda la versión más reciente
    UPDATE climate_analytics.stg_observations st SET motivo_rechazo = 'Duplicado dentro del lote (misma ubicación y fecha)'
    FROM (
        SELECT batch_id, fila, row_number() OVER (
                   PARTITION BY series_id, ts_utc ORDER BY actualizado_utc DESC, fila DESC) AS orden
        FROM climate_analytics.stg_observations
        WHERE batch_id = p_batch_id AND motivo_rechazo IS NULL
    ) d
    WHERE st.batch_id = d.batch_id AND st.fila = d.fila AND d.orden > 1;

    -- 6) Resultado
    UPDATE climate_analytics.stg_observations st SET es_valido = (st.motivo_rechazo IS NULL)
    WHERE st.batch_id = p_batch_id;
    SELECT count(*) FILTER (WHERE es_valido), count(*) FILTER (WHERE NOT es_valido)
      INTO p_validos, p_rechazados
    FROM climate_analytics.stg_observations WHERE batch_id = p_batch_id;
END $$;

-- =====================================================================================
-- SP 4 · Calcula la condición del clima con reglas sobre variables numéricas.
-- Si la fuente ya trae una condición se respeta (valor por defecto de la fuente),
-- salvo que p_forzar = true. La condición calculada queda como CALCULATED + reglas-v1.
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_clasificar_condiciones(
    p_batch_id uuid, p_forzar boolean, INOUT p_calculadas int)
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE climate_analytics.stg_observations st SET
        condition_id = climate_analytics.fn_clasificar_condicion(
            st.precipitation_mm, st.cloud_cover_pct, st.visibility_km, st.humidity_pct, st.temperature_c),
        condition_origin = 'CALCULATED',
        condition_rule_version = 'reglas-v1'
    WHERE st.batch_id = p_batch_id AND st.es_valido
      AND (st.condition_id IS NULL OR coalesce(p_forzar, false));
    GET DIAGNOSTICS p_calculadas = ROW_COUNT;
    -- Las que vienen de la fuente quedan marcadas como SOURCE
    UPDATE climate_analytics.stg_observations st SET condition_origin = 'SOURCE'
    WHERE st.batch_id = p_batch_id AND st.es_valido AND st.condition_origin IS NULL;
END $$;

-- =====================================================================================
-- SP 5 · Carga las filas válidas en observations (insertar o actualizar).
-- Solo actualiza si el dato de DynamoDB es más reciente, o si estaba marcado como borrado.
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_cargar_observaciones(
    p_batch_id uuid, INOUT p_insertados int, INOUT p_actualizados int, INOUT p_omitidos int)
LANGUAGE plpgsql AS $$
DECLARE
    v_validos int;
BEGIN
    SELECT count(*) INTO v_validos FROM climate_analytics.stg_observations
    WHERE batch_id = p_batch_id AND es_valido;

    WITH cambios AS (
        INSERT INTO climate_analytics.observations AS o
            (series_id, timestamp_utc, batch_id, last_event_id, source_record_id, source_version,
             content_hash, source_id, condition_id, condition_origin, condition_rule_version,
             temperature_c, feels_like_c, humidity_pct, pressure_hpa, wind_speed_kmh, wind_direction,
             precipitation_mm, precipitation_period_minutes, cloud_cover_pct, visibility_km, uv_index,
             dew_point_c, received_at_utc, updated_at_utc, deleted_at_utc)
        SELECT st.series_id, st.ts_utc, p_batch_id, st.last_event_id::uuid, st.source_record_id,
               st.source_version, st.content_hash, st.source_id, st.condition_id, st.condition_origin,
               st.condition_rule_version, st.temperature_c, st.feels_like_c, st.humidity_pct,
               st.pressure_hpa, st.wind_speed_kmh, st.wind_direction, st.precipitation_mm,
               st.precipitation_period_minutes, st.cloud_cover_pct, st.visibility_km, st.uv_index,
               st.dew_point_c, st.recibido_utc, st.actualizado_utc, NULL
        FROM climate_analytics.stg_observations st
        WHERE st.batch_id = p_batch_id AND st.es_valido
        ON CONFLICT (series_id, timestamp_utc) DO UPDATE SET
            batch_id = EXCLUDED.batch_id, last_event_id = EXCLUDED.last_event_id,
            source_record_id = EXCLUDED.source_record_id, source_version = EXCLUDED.source_version,
            content_hash = EXCLUDED.content_hash, source_id = EXCLUDED.source_id,
            condition_id = EXCLUDED.condition_id, condition_origin = EXCLUDED.condition_origin,
            condition_rule_version = EXCLUDED.condition_rule_version,
            temperature_c = EXCLUDED.temperature_c, feels_like_c = EXCLUDED.feels_like_c,
            humidity_pct = EXCLUDED.humidity_pct, pressure_hpa = EXCLUDED.pressure_hpa,
            wind_speed_kmh = EXCLUDED.wind_speed_kmh, wind_direction = EXCLUDED.wind_direction,
            precipitation_mm = EXCLUDED.precipitation_mm,
            precipitation_period_minutes = EXCLUDED.precipitation_period_minutes,
            cloud_cover_pct = EXCLUDED.cloud_cover_pct, visibility_km = EXCLUDED.visibility_km,
            uv_index = EXCLUDED.uv_index, dew_point_c = EXCLUDED.dew_point_c,
            updated_at_utc = EXCLUDED.updated_at_utc, deleted_at_utc = NULL
        WHERE o.updated_at_utc < EXCLUDED.updated_at_utc OR o.deleted_at_utc IS NOT NULL
        RETURNING (xmax = 0) AS insertado
    )
    SELECT count(*) FILTER (WHERE insertado), count(*) FILTER (WHERE NOT insertado)
      INTO p_insertados, p_actualizados
    FROM cambios;
    p_omitidos := v_validos - p_insertados - p_actualizados;
END $$;

-- =====================================================================================
-- SP 6 · Borrado lógico: marca deleted_at_utc en las observaciones que ya no existen
-- en DynamoDB (solo tiene sentido en un lote de modo completo).
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_marcar_borrados(
    p_batch_id uuid, INOUT p_borrados int)
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM climate_analytics.stg_observations
                   WHERE batch_id = p_batch_id AND series_id IS NOT NULL AND ts_utc IS NOT NULL) THEN
        RAISE NOTICE 'El lote % no trajo registros; no se marca nada como borrado', p_batch_id;
        p_borrados := 0;
        RETURN;
    END IF;
    UPDATE climate_analytics.observations o
    SET deleted_at_utc = clock_timestamp(), batch_id = p_batch_id
    WHERE o.deleted_at_utc IS NULL
      AND NOT EXISTS (SELECT 1 FROM climate_analytics.stg_observations st
                      WHERE st.batch_id = p_batch_id AND st.series_id = o.series_id AND st.ts_utc = o.timestamp_utc);
    GET DIAGNOSTICS p_borrados = ROW_COUNT;
END $$;

-- =====================================================================================
-- SP 7 · Promedios diarios por ciudad (día LOCAL de cada serie). Solo cambia la
-- versión de un día si sus valores cambiaron.
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_calcular_agregados_diarios(
    INOUT p_nuevos int, INOUT p_actualizados int)
LANGUAGE plpgsql AS $$
BEGIN
    WITH dias AS (
        SELECT o.series_id, s.timezone_name,
               date_trunc('day', o.timestamp_utc AT TIME ZONE s.timezone_name) AS dia_local,
               min(o.temperature_c) AS tmin, max(o.temperature_c) AS tmax,
               round(avg(o.temperature_c), 6) AS tmed, round(avg(o.humidity_pct), 6) AS hmed,
               sum(o.precipitation_mm) AS lluvia, count(*) AS muestras
        FROM climate_analytics.observations o
        JOIN climate_analytics.series s ON s.series_id = o.series_id
        WHERE o.deleted_at_utc IS NULL
        GROUP BY o.series_id, s.timezone_name, dia_local
    ), rangos AS (
        SELECT *, dia_local AT TIME ZONE timezone_name AS inicio,
               (dia_local + interval '1 day') AT TIME ZONE timezone_name AS fin
        FROM dias
    ), cambios AS (
        INSERT INTO climate_analytics.climate_aggregates AS a
            (series_id, granularity, starts_at_utc, ends_at_utc, version, temperature_min_c,
             temperature_max_c, temperature_mean_c, humidity_mean_pct, precipitation_total_mm,
             valid_samples, expected_samples, coverage_pct, computed_at_utc)
        SELECT series_id, 'DAY', inicio, fin, 1, tmin, tmax, tmed, hmed, lluvia, muestras,
               round(extract(epoch FROM fin - inicio) / 3600)::int,
               least(100, round(100.0 * muestras / round(extract(epoch FROM fin - inicio) / 3600), 2)),
               clock_timestamp()
        FROM rangos
        ON CONFLICT (series_id, granularity, starts_at_utc) DO UPDATE SET
            ends_at_utc = EXCLUDED.ends_at_utc, temperature_min_c = EXCLUDED.temperature_min_c,
            temperature_max_c = EXCLUDED.temperature_max_c, temperature_mean_c = EXCLUDED.temperature_mean_c,
            humidity_mean_pct = EXCLUDED.humidity_mean_pct, precipitation_total_mm = EXCLUDED.precipitation_total_mm,
            valid_samples = EXCLUDED.valid_samples, expected_samples = EXCLUDED.expected_samples,
            coverage_pct = EXCLUDED.coverage_pct, computed_at_utc = EXCLUDED.computed_at_utc,
            version = a.version + 1
        WHERE (a.temperature_min_c, a.temperature_max_c, a.temperature_mean_c, a.humidity_mean_pct,
               a.precipitation_total_mm, a.valid_samples)
          IS DISTINCT FROM (EXCLUDED.temperature_min_c, EXCLUDED.temperature_max_c, EXCLUDED.temperature_mean_c,
               EXCLUDED.humidity_mean_pct, EXCLUDED.precipitation_total_mm, EXCLUDED.valid_samples)
        RETURNING (xmax = 0) AS insertado
    )
    SELECT count(*) FILTER (WHERE insertado), count(*) FILTER (WHERE NOT insertado)
      INTO p_nuevos, p_actualizados
    FROM cambios;
END $$;

-- =====================================================================================
-- SP 8 · Cierra el lote (SUCCEEDED o FAILED) con sus conteos y su punto de control.
-- En un lote exitoso borra del staging las filas ya cargadas; las RECHAZADAS se quedan
-- como cola de registros anómalos.
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_cerrar_lote(
    p_batch_id uuid, p_estado text, p_leidos int, p_insertados int, p_actualizados int,
    p_omitidos int, p_rechazados int, p_punto_control jsonb, p_error text)
LANGUAGE plpgsql AS $$
BEGIN
    IF p_estado NOT IN ('SUCCEEDED', 'FAILED') THEN
        RAISE EXCEPTION 'Estado no válido: % (use SUCCEEDED o FAILED)', p_estado;
    END IF;
    UPDATE climate_analytics.etl_batches SET
        finished_at_utc = clock_timestamp(), status = p_estado, end_checkpoint = p_punto_control,
        read_count = coalesce(p_leidos, 0), inserted_count = coalesce(p_insertados, 0),
        updated_count = coalesce(p_actualizados, 0), skipped_count = coalesce(p_omitidos, 0),
        rejected_count = coalesce(p_rechazados, 0), error_summary = left(p_error, 1000)
    WHERE batch_id = p_batch_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'El lote % no existe', p_batch_id;
    END IF;
    IF p_estado = 'SUCCEEDED' THEN
        DELETE FROM climate_analytics.stg_observations WHERE batch_id = p_batch_id AND es_valido;
    END IF;
END $$;

-- =====================================================================================
-- SP 9 · Evalúa predicciones contra lo que realmente pasó (días completos en
-- climate_aggregates). p_forecast_run_id NULL = todas las predicciones pendientes.
-- Si un día real se recalcula (nueva versión), se vuelve a evaluar.
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_evaluar_predicciones(
    p_forecast_run_id uuid, INOUT p_evaluadas int)
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO climate_analytics.prediction_evaluations
        (prediction_id, actual_version, series_id, granularity, starts_at_utc, ends_at_utc,
         evaluated_at_utc, actual_values, errors)
    SELECT p.prediction_id, a.version, p.series_id, p.granularity, p.target_start_utc, p.target_end_utc,
           now(),   -- misma hora para toda la corrida de evaluación
           jsonb_build_object(
               'temperature_mean_c', a.temperature_mean_c, 'temperature_min_c', a.temperature_min_c,
               'temperature_max_c', a.temperature_max_c, 'humidity_mean_pct', a.humidity_mean_pct,
               'precipitation_total_mm', a.precipitation_total_mm, 'llovio', a.precipitation_total_mm >= 0.5),
           jsonb_build_object(
               'temperature_mean_c', round(abs(p.temperature_mean_c - a.temperature_mean_c), 6),
               'temperature_min_c', round(abs(p.temperature_min_c - a.temperature_min_c), 6),
               'temperature_max_c', round(abs(p.temperature_max_c - a.temperature_max_c), 6),
               'humidity_mean_pct', round(abs(p.humidity_pct - a.humidity_mean_pct), 6),
               'precipitation_total_mm', round(abs(p.rainfall_mm - a.precipitation_total_mm), 6),
               'acierto_lluvia', (p.rain_probability_pct >= 50) = (a.precipitation_total_mm >= 0.5))
    FROM climate_analytics.predictions p
    JOIN climate_analytics.climate_aggregates a
      ON a.series_id = p.series_id AND a.granularity = p.granularity AND a.starts_at_utc = p.target_start_utc
    WHERE (p_forecast_run_id IS NULL OR p.forecast_run_id = p_forecast_run_id)
      AND a.ends_at_utc = p.target_end_utc
      AND a.coverage_pct = 100
      AND NOT EXISTS (SELECT 1 FROM climate_analytics.prediction_evaluations e
                      WHERE e.prediction_id = p.prediction_id AND e.actual_version = a.version);
    GET DIAGNOSTICS p_evaluadas = ROW_COUNT;
END $$;

-- =====================================================================================
-- SP 10 · Métricas de calidad MAE y RMSE por ciudad y variable, a partir de las
-- evaluaciones de una corrida de pronóstico. p_conjunto: TRAIN, VALIDATION, TEST o LIVE.
-- =====================================================================================
CREATE OR REPLACE PROCEDURE climate_analytics.sp_calcular_metricas(
    p_forecast_run_id uuid, p_conjunto text, INOUT p_filas int)
LANGUAGE plpgsql AS $$
BEGIN
    IF p_conjunto NOT IN ('TRAIN', 'VALIDATION', 'TEST', 'LIVE') THEN
        RAISE EXCEPTION 'Conjunto no válido: %', p_conjunto;
    END IF;
    WITH ultima AS (
        SELECT DISTINCT ON (e.prediction_id) e.prediction_id, e.series_id, e.starts_at_utc, e.ends_at_utc,
               e.actual_values, p.training_run_id, p.temperature_mean_c, p.temperature_min_c,
               p.temperature_max_c, p.humidity_pct, p.rainfall_mm
        FROM climate_analytics.prediction_evaluations e
        JOIN climate_analytics.predictions p ON p.prediction_id = e.prediction_id
        WHERE p.forecast_run_id = p_forecast_run_id
        ORDER BY e.prediction_id, e.actual_version DESC
    ), errores AS (
        SELECT u.training_run_id, u.series_id, u.starts_at_utc, u.ends_at_utc, v.variable, v.unidad, v.error
        FROM ultima u
        CROSS JOIN LATERAL (VALUES
            ('temperature_mean_c', '°C', u.temperature_mean_c - (u.actual_values->>'temperature_mean_c')::numeric),
            ('temperature_min_c', '°C', u.temperature_min_c - (u.actual_values->>'temperature_min_c')::numeric),
            ('temperature_max_c', '°C', u.temperature_max_c - (u.actual_values->>'temperature_max_c')::numeric),
            ('humidity_mean_pct', '%', u.humidity_pct - (u.actual_values->>'humidity_mean_pct')::numeric),
            ('precipitation_total_mm', 'mm', u.rainfall_mm - (u.actual_values->>'precipitation_total_mm')::numeric)
        ) AS v(variable, unidad, error)
    )
    INSERT INTO climate_analytics.model_metrics
        (metric_id, training_run_id, series_id, variable_name, metric_name, dataset_split,
         horizon_minutes, granularity, starts_at_utc, ends_at_utc, value, sample_count, unit)
    SELECT gen_random_uuid(), training_run_id, series_id, variable, m.nombre, p_conjunto, 1440, 'DAY',
           min(starts_at_utc), max(ends_at_utc),
           CASE m.nombre WHEN 'MAE' THEN round(avg(abs(error)), 8)
                         ELSE round(sqrt(avg(error * error)), 8) END,
           count(*), unidad
    FROM errores CROSS JOIN (VALUES ('MAE'), ('RMSE')) AS m(nombre)
    GROUP BY training_run_id, series_id, variable, unidad, m.nombre
    ON CONFLICT (training_run_id, series_id, variable_name, metric_name, dataset_split,
                 horizon_minutes, granularity, starts_at_utc, ends_at_utc)
    DO UPDATE SET value = EXCLUDED.value, sample_count = EXCLUDED.sample_count;
    GET DIAGNOSTICS p_filas = ROW_COUNT;
END $$;

-- Verificación: deben aparecer 10 procedimientos (PROCEDURE) y 2 funciones auxiliares.
SELECT routine_type AS tipo, routine_name AS nombre
FROM information_schema.routines
WHERE routine_schema = 'climate_analytics'
ORDER BY routine_type DESC, routine_name;
