-- =====================================================================================
-- AWS Clima V2 · Stored procedures de MySQL climate_transactional (RDS 1)
--   SP 12 · sp_guardar_admin    alta de administrador o cambio de contraseña (con roles)
--   SP 13 · sp_rotar_auditoria  rotación e historización de la bitácora y limpieza de sesiones
--
-- Cómo instalar: .\.venv\Scripts\python.exe etl\instalar_sp.py --solo-mysql
--   (etl\migrar_superadmin.py también lo reinstala al agregar el rol SUPERADMIN)
-- Se puede ejecutar más de una vez (IF NOT EXISTS / DROP PROCEDURE IF EXISTS).
-- Los UUID v4 los genera la aplicación (MySQL solo tiene UUID() versión 1).
-- Las contraseñas NUNCA llegan aquí: la aplicación manda solo el hash (scrypt).
--
-- Roles (services/roles.py): las cuentas nuevas siempre son ADMIN. Con actor (panel o API) solo un
-- SUPERADMIN activo crea cuentas o cambia contraseñas ajenas, y la contraseña de un SUPERADMIN solo
-- la cambia esa misma cuenta. Sin actor (línea de comandos con la contraseña de la base) se permite.
-- La firma de sp_guardar_admin no cambió: la Lambda ClimateDbV2 anterior sigue funcionando.
-- =====================================================================================

-- Historial de la bitácora: mismas columnas que audit_events + la fecha en que se archivó.
-- Sin llave foránea a users para conservar el historial aunque un usuario se elimine.
CREATE TABLE IF NOT EXISTS audit_events_history (
  event_id char(36) COLLATE utf8mb4_bin NOT NULL,
  actor_id char(36) COLLATE utf8mb4_bin DEFAULT NULL,
  action varchar(40) COLLATE utf8mb4_bin NOT NULL,
  entity_type varchar(60) COLLATE utf8mb4_bin NOT NULL,
  entity_key varchar(255) COLLATE utf8mb4_bin NOT NULL,
  occurred_at_utc datetime(6) NOT NULL,
  request_id varchar(100) COLLATE utf8mb4_bin NOT NULL,
  transaction_id char(36) COLLATE utf8mb4_bin NOT NULL,
  before_data json DEFAULT NULL,
  after_data json DEFAULT NULL,
  outcome varchar(30) COLLATE utf8mb4_bin NOT NULL,
  reason text COLLATE utf8mb4_bin,
  archived_at_utc datetime(6) NOT NULL,
  PRIMARY KEY (event_id),
  KEY idx_history_time (occurred_at_utc),
  KEY idx_history_actor (actor_id, occurred_at_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

DROP PROCEDURE IF EXISTS sp_guardar_admin;
DROP PROCEDURE IF EXISTS sp_rotar_auditoria;

DELIMITER //

-- -------------------------------------------------------------------------------------
-- SP 12 · Alta de administrador o cambio de contraseña, en una sola transacción.
--   - Si el correo no existe: crea el usuario con rol ADMIN (p_accion = 'CREADO').
--   - Si existe y es ADMIN o SUPERADMIN: cambia el hash, sube auth_version y revoca todas sus
--     sesiones, así cualquier token anterior deja de servir al instante (p_accion = 'CONTRASENA').
--   - Con p_actor_id: solo un SUPERADMIN activo gestiona cuentas ajenas, y la contraseña de un
--     SUPERADMIN solo la cambia esa misma cuenta.
--   - Escribe el evento en audit_events (ADMIN_CREATE o PASSWORD_CHANGE).
-- -------------------------------------------------------------------------------------
CREATE PROCEDURE sp_guardar_admin(
    IN p_email VARCHAR(254),
    IN p_display_name VARCHAR(120),
    IN p_password_hash VARCHAR(255),
    IN p_new_user_id CHAR(36),
    IN p_actor_id CHAR(36),
    IN p_event_id CHAR(36),
    IN p_transaction_id CHAR(36),
    IN p_request_id VARCHAR(100),
    OUT p_user_id CHAR(36),
    OUT p_accion VARCHAR(20))
BEGIN
    DECLARE v_email VARCHAR(254);
    DECLARE v_nombre VARCHAR(120);
    DECLARE v_rol SMALLINT;
    DECLARE v_usuario CHAR(36);
    DECLARE v_rol_actual VARCHAR(30);
    DECLARE v_rol_actor VARCHAR(30);
    DECLARE v_ahora DATETIME(6);
    DECLARE v_revocadas INT DEFAULT 0;
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;

    SET v_email = LOWER(TRIM(p_email));
    IF v_email IS NULL OR v_email NOT REGEXP '^[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,180}[.][a-z]{2,24}$' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Correo no válido';
    END IF;
    IF p_password_hash IS NULL OR CHAR_LENGTH(p_password_hash) < 40
       OR (p_password_hash NOT LIKE 'scrypt:%' AND p_password_hash NOT LIKE 'pbkdf2:%') THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Se esperaba el hash de la contraseña, no la contraseña';
    END IF;
    IF COALESCE(p_new_user_id, '') NOT REGEXP '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR COALESCE(p_event_id, '') NOT REGEXP '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR COALESCE(p_transaction_id, '') NOT REGEXP '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Los identificadores deben ser UUID versión 4';
    END IF;

    SET v_ahora = UTC_TIMESTAMP(6);
    SET v_nombre = NULLIF(TRIM(p_display_name), '');

    START TRANSACTION;
    SET v_rol = (SELECT role_id FROM roles WHERE code = 'ADMIN');
    IF v_rol IS NULL THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'No existe el rol ADMIN en la tabla roles';
    END IF;
    SET v_usuario = (SELECT user_id FROM users WHERE normalized_email = v_email);

    -- Desde el panel o la API (con actor): crear cuentas o cambiar contraseñas ajenas es solo del
    -- SUPERADMIN activo. Cambiar la propia contraseña lo puede hacer cualquier cuenta.
    IF p_actor_id IS NOT NULL AND (v_usuario IS NULL OR v_usuario <> p_actor_id) THEN
        SET v_rol_actor = (SELECT r.code FROM users u JOIN roles r ON r.role_id = u.role_id
                            WHERE u.user_id = p_actor_id AND u.is_active = 1);
        IF v_rol_actor IS NULL OR v_rol_actor <> 'SUPERADMIN' THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Solo el superadministrador puede crear cuentas o cambiar contraseñas de otros';
        END IF;
    END IF;

    IF v_usuario IS NULL THEN
        INSERT INTO users (user_id, role_id, normalized_email, password_hash, display_name,
                           is_active, auth_version, created_at_utc)
        VALUES (p_new_user_id, v_rol, v_email, p_password_hash,
                COALESCE(v_nombre, SUBSTRING_INDEX(v_email, '@', 1)), 1, 1, v_ahora);
        SET p_user_id = p_new_user_id;
        SET p_accion = 'CREADO';
    ELSE
        SET v_rol_actual = (SELECT r.code FROM users u JOIN roles r ON r.role_id = u.role_id
                            WHERE u.user_id = v_usuario);
        IF v_rol_actual NOT IN ('ADMIN', 'SUPERADMIN') THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Ese correo pertenece a un usuario que no es administrador';
        END IF;
        IF v_rol_actual = 'SUPERADMIN' AND p_actor_id IS NOT NULL AND p_actor_id <> v_usuario THEN
            SIGNAL SQLSTATE '45000'
                SET MESSAGE_TEXT = 'Solo el propio superadministrador puede cambiar su contraseña';
        END IF;
        UPDATE users
           SET password_hash = p_password_hash,
               display_name = COALESCE(v_nombre, display_name),
               auth_version = auth_version + 1
         WHERE user_id = v_usuario;
        UPDATE sessions SET revoked_at_utc = v_ahora
         WHERE user_id = v_usuario AND revoked_at_utc IS NULL;
        SET v_revocadas = ROW_COUNT();
        SET p_user_id = v_usuario;
        SET p_accion = 'CONTRASENA';
    END IF;

    INSERT INTO audit_events (event_id, actor_id, action, entity_type, entity_key, occurred_at_utc,
                              request_id, transaction_id, before_data, after_data, outcome, reason)
    VALUES (p_event_id, p_actor_id,
            IF(p_accion = 'CREADO', 'ADMIN_CREATE', 'PASSWORD_CHANGE'), 'USER', v_email, v_ahora,
            COALESCE(NULLIF(p_request_id, ''), p_event_id), p_transaction_id, NULL,
            JSON_OBJECT('email', v_email, 'rol', COALESCE(v_rol_actual, 'ADMIN'), 'sesiones_revocadas', v_revocadas,
                        'procedimiento', 'sp_guardar_admin'),
            'SUCCESS', NULL);
    COMMIT;
END //

-- -------------------------------------------------------------------------------------
-- SP 13 · Rotación e historización de la bitácora, en una sola transacción.
--   - Copia a audit_events_history los eventos con más de p_dias días y los quita de
--     audit_events (solo los que quedaron copiados).
--   - Elimina las sesiones que ya no sirven: revocadas, vencidas o con más de 30 minutos
--     sin actividad.
--   - La rotación misma queda registrada como evento AUDIT_ROTATE.
-- -------------------------------------------------------------------------------------
CREATE PROCEDURE sp_rotar_auditoria(
    IN p_dias INT,
    IN p_actor_id CHAR(36),
    IN p_event_id CHAR(36),
    IN p_transaction_id CHAR(36),
    IN p_request_id VARCHAR(100),
    OUT p_movidos INT,
    OUT p_sesiones INT)
BEGIN
    DECLARE v_ahora DATETIME(6);
    DECLARE v_corte DATETIME(6);
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        RESIGNAL;
    END;

    IF p_dias IS NULL OR p_dias < 0 OR p_dias > 3650 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Los días deben estar entre 0 y 3650';
    END IF;
    IF COALESCE(p_event_id, '') NOT REGEXP '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR COALESCE(p_transaction_id, '') NOT REGEXP '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Los identificadores deben ser UUID versión 4';
    END IF;

    SET v_ahora = UTC_TIMESTAMP(6);
    SET v_corte = v_ahora - INTERVAL p_dias DAY;

    START TRANSACTION;
    INSERT INTO audit_events_history (event_id, actor_id, action, entity_type, entity_key,
                                      occurred_at_utc, request_id, transaction_id, before_data,
                                      after_data, outcome, reason, archived_at_utc)
    SELECT event_id, actor_id, action, entity_type, entity_key, occurred_at_utc, request_id,
           transaction_id, before_data, after_data, outcome, reason, v_ahora
      FROM audit_events
     WHERE occurred_at_utc < v_corte;
    SET p_movidos = ROW_COUNT();

    DELETE a FROM audit_events a
      JOIN audit_events_history h ON h.event_id = a.event_id
     WHERE a.occurred_at_utc < v_corte;

    DELETE FROM sessions
     WHERE revoked_at_utc IS NOT NULL
        OR expires_at_utc < v_ahora
        OR last_activity_at_utc < v_ahora - INTERVAL 30 MINUTE;
    SET p_sesiones = ROW_COUNT();

    INSERT INTO audit_events (event_id, actor_id, action, entity_type, entity_key, occurred_at_utc,
                              request_id, transaction_id, before_data, after_data, outcome, reason)
    VALUES (p_event_id, p_actor_id, 'AUDIT_ROTATE', 'AUDIT', CONCAT('dias=', p_dias), v_ahora,
            COALESCE(NULLIF(p_request_id, ''), p_event_id), p_transaction_id, NULL,
            JSON_OBJECT('movidos_al_historico', p_movidos, 'sesiones_eliminadas', p_sesiones,
                        'corte_utc', DATE_FORMAT(v_corte, '%Y-%m-%d %H:%i:%s'),
                        'procedimiento', 'sp_rotar_auditoria'),
            'SUCCESS', NULL);
    COMMIT;
END //

DELIMITER ;
