-- Consultas de verificación para DBeaver · conexión MySQL climate_transactional
-- Solo leen datos (SELECT). Ejecuta cada una con Ctrl+Enter.

-- 1. Usuarios creados por el administrador y su rol (la contraseña solo existe como hash)
SELECT u.normalized_email, u.display_name, r.code AS rol, u.is_active, u.auth_version, u.created_at_utc
FROM users u JOIN roles r ON r.role_id = u.role_id
ORDER BY u.created_at_utc;

-- 2. Sesiones JWT: activas, revocadas (logout / usuario desactivado) o vencidas por 30 min sin actividad
SELECT s.session_id, u.normalized_email, s.created_at_utc, s.last_activity_at_utc, s.expires_at_utc,
       s.revoked_at_utc,
       CASE WHEN s.revoked_at_utc IS NOT NULL THEN 'REVOCADA'
            WHEN s.expires_at_utc <= UTC_TIMESTAMP(6) THEN 'EXPIRADA'
            WHEN s.last_activity_at_utc < UTC_TIMESTAMP(6) - INTERVAL 30 MINUTE THEN 'INACTIVA > 30 MIN'
            ELSE 'ACTIVA' END AS estado
FROM sessions s JOIN users u ON u.user_id = s.user_id
ORDER BY s.created_at_utc DESC
LIMIT 20;

-- 3. Bitácora de auditoría: últimos 50 eventos
SELECT a.occurred_at_utc, COALESCE(u.normalized_email, '—') AS actor, a.action, a.entity_type,
       a.entity_key, a.outcome, a.reason, a.request_id
FROM audit_events a LEFT JOIN users u ON u.user_id = a.actor_id
ORDER BY a.occurred_at_utc DESC
LIMIT 50;

-- 4. Resumen de auditoría por acción y resultado
SELECT action, outcome, COUNT(*) AS eventos, MAX(occurred_at_utc) AS ultimo
FROM audit_events
GROUP BY action, outcome
ORDER BY action, outcome;

-- 5. Antes y después de un cambio a una lectura (concurrencia optimista: la versión sube 1 en cada cambio)
SELECT occurred_at_utc, action, entity_key,
       JSON_EXTRACT(before_data, '$.version') AS version_antes,
       JSON_EXTRACT(after_data, '$.version')  AS version_despues,
       JSON_EXTRACT(before_data, '$.uv_index') AS uv_antes,
       JSON_EXTRACT(after_data, '$.uv_index')  AS uv_despues
FROM audit_events
WHERE action IN ('READING_CREATE', 'READING_UPDATE', 'READING_DELETE') AND outcome = 'SUCCESS'
ORDER BY occurred_at_utc DESC
LIMIT 20;

-- 6. Intentos de acceso fallidos y bloqueos (5 fallos en 15 minutos bloquean la cuenta)
SELECT occurred_at_utc, entity_key AS correo, outcome, reason
FROM audit_events
WHERE action = 'LOGIN' AND outcome IN ('FAILURE', 'BLOCKED')
ORDER BY occurred_at_utc DESC
LIMIT 20;

-- 7. Catálogo: ubicaciones activas con su estación
SELECT l.location_id, l.name, l.city, l.state, l.latitude, l.longitude, l.timezone_name,
       s.station_id, s.code, s.station_type, s.expected_interval_minutes
FROM locations l JOIN stations s ON s.location_id = l.location_id
WHERE l.is_active = 1
ORDER BY l.location_id;

-- 8. Stored procedures de MySQL (deben salir sp_guardar_admin y sp_rotar_auditoria)
SELECT ROUTINE_NAME AS procedimiento, CREATED AS creado
FROM information_schema.ROUTINES
WHERE ROUTINE_SCHEMA = DATABASE() AND ROUTINE_TYPE = 'PROCEDURE'
ORDER BY ROUTINE_NAME;

-- 9. Historial de la bitácora: lo que movió sp_rotar_auditoria
SELECT archived_at_utc AS archivado, occurred_at_utc AS ocurrio, action, entity_key, outcome
FROM audit_events_history
ORDER BY archived_at_utc DESC, occurred_at_utc DESC
LIMIT 50;
