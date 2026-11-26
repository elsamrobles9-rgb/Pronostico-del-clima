-- =====================================================================================
-- AWS Clima V2 · Migración segura: rol SUPERADMIN en MySQL climate_transactional
--
-- RECOMENDADO: correr el script, que hace estos mismos pasos, verifica que ninguna contraseña
-- cambie y pide el correo sin editar nada:
--     .\.venv\Scripts\python.exe etl\migrar_superadmin.py
-- Este archivo es la versión manual (DBeaver) y la documentación de la migración.
-- Ejecuta bloque por bloque, en orden, y revisa el resultado de cada uno.
--
-- Qué cambia: amplía la regla CHECK de la tabla roles, agrega la fila SUPERADMIN, reinstala
-- sp_guardar_admin (misma firma) y promueve UNA cuenta ADMIN existente a SUPERADMIN.
-- Qué NO cambia: ninguna cuenta se borra, ningún hash de contraseña se toca, ningún dato se elimina.
-- =====================================================================================

-- 0) FOTO ANTES. Guarda los dos resultados: al final deben coincidir las huellas.
SET SESSION group_concat_max_len = 1000000;
SELECT r.code AS rol, COUNT(*) AS cuentas, SUM(u.is_active) AS activas
  FROM users u JOIN roles r ON r.role_id = u.role_id GROUP BY r.code ORDER BY r.code;
SELECT COUNT(*) AS cuentas,
       SHA2(GROUP_CONCAT(u.user_id, ':', u.password_hash ORDER BY u.user_id SEPARATOR '|'), 256) AS huella_hashes
  FROM users u;

-- 1) AMPLIAR LA REGLA CHECK DE roles (solo la regla; los datos no cambian).
--    Revisa el nombre de la regla actual; en el DDL del proyecto es roles_chk_1.
SELECT tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
  FROM information_schema.TABLE_CONSTRAINTS tc
  JOIN information_schema.CHECK_CONSTRAINTS cc
    ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
 WHERE tc.TABLE_SCHEMA = DATABASE() AND tc.TABLE_NAME = 'roles' AND tc.CONSTRAINT_TYPE = 'CHECK';
-- Si la regla ya menciona SUPERADMIN, salta este paso.
ALTER TABLE roles
  DROP CHECK roles_chk_1,
  ADD CONSTRAINT roles_code_chk CHECK (code IN ('SUPERADMIN', 'ADMIN', 'DATA_ENTRY', 'ANALYST'));

-- 2) AGREGAR EL ROL (no hace nada si ya existe).
SET @nuevo_rol := (SELECT COALESCE(MAX(role_id), 0) + 1 FROM roles);
INSERT INTO roles (role_id, code, name)
SELECT @nuevo_rol, 'SUPERADMIN', 'Superadministrador' FROM DUAL
 WHERE NOT EXISTS (SELECT 1 FROM roles WHERE code = 'SUPERADMIN');
SELECT * FROM roles ORDER BY role_id;

-- 3) REINSTALAR LOS PROCEDIMIENTOS: abre sql/mysql_procedimientos.sql y ejecútalo completo
--    (sp_guardar_admin con las reglas de roles; la firma no cambia).

-- 4) TRIGGERS: impiden dejar el sistema sin superadministrador activo, aunque alguien edite la
--    tabla a mano. Si RDS responde "You do not have the SUPER privilege and binary logging is
--    enabled", sáltate este paso: la aplicación y sp_guardar_admin ya lo impiden.
DROP TRIGGER IF EXISTS trg_users_superadmin_upd;
DROP TRIGGER IF EXISTS trg_users_superadmin_del;

DELIMITER //

CREATE TRIGGER trg_users_superadmin_upd BEFORE UPDATE ON users FOR EACH ROW
BEGIN
    DECLARE v_super SMALLINT;
    SET v_super = (SELECT role_id FROM roles WHERE code = 'SUPERADMIN');
    IF v_super IS NOT NULL AND OLD.role_id = v_super AND OLD.is_active = 1
       AND (NEW.role_id <> v_super OR NEW.is_active <> 1) THEN
        IF (SELECT COUNT(*) FROM users
             WHERE role_id = v_super AND is_active = 1 AND user_id <> OLD.user_id) = 0 THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Debe quedar al menos un superadministrador activo';
        END IF;
    END IF;
END //

CREATE TRIGGER trg_users_superadmin_del BEFORE DELETE ON users FOR EACH ROW
BEGIN
    DECLARE v_super SMALLINT;
    SET v_super = (SELECT role_id FROM roles WHERE code = 'SUPERADMIN');
    IF v_super IS NOT NULL AND OLD.role_id = v_super AND OLD.is_active = 1 THEN
        IF (SELECT COUNT(*) FROM users
             WHERE role_id = v_super AND is_active = 1 AND user_id <> OLD.user_id) = 0 THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Debe quedar al menos un superadministrador activo';
        END IF;
    END IF;
END //

DELIMITER ;

-- 5) PROMOVER LA CUENTA ELEGIDA. Cambia el correo: debe ser un ADMIN activo que ya exista.
--    Sube auth_version y revoca sus sesiones: el nuevo rol aplica al volver a iniciar sesión.
SET @correo := LOWER(TRIM('cambia.este.correo@ejemplo.com'));
START TRANSACTION;
UPDATE users u JOIN roles a ON a.role_id = u.role_id AND a.code = 'ADMIN'
   SET u.role_id = (SELECT role_id FROM roles WHERE code = 'SUPERADMIN'),
       u.auth_version = u.auth_version + 1
 WHERE u.normalized_email = @correo AND u.is_active = 1;
-- Debe decir "1 row affected". Si dice 0, el correo no existe, no es ADMIN o está desactivado:
-- ejecuta ROLLBACK; y revisa el correo.
UPDATE sessions s JOIN users u ON u.user_id = s.user_id
   SET s.revoked_at_utc = UTC_TIMESTAMP(6)
 WHERE u.normalized_email = @correo AND s.revoked_at_utc IS NULL;
INSERT INTO audit_events (event_id, actor_id, action, entity_type, entity_key, occurred_at_utc,
                          request_id, transaction_id, before_data, after_data, outcome, reason)
SELECT UUID(), NULL, 'ROLE_CHANGE', 'USER', u.normalized_email, UTC_TIMESTAMP(6), 'migracion-superadmin', UUID(),
       JSON_OBJECT('role', 'ADMIN'), JSON_OBJECT('role', 'SUPERADMIN'), 'SUCCESS', 'migracion manual'
  FROM users u WHERE u.normalized_email = @correo;
COMMIT;

-- 6) FOTO DESPUÉS. Repite el paso 0: la huella_hashes y el número de cuentas deben ser IGUALES.
--    Debe aparecer al menos 1 SUPERADMIN activo.

-- =====================================================================================
-- REVERTIR (solo si hace falta). Regresa a ADMIN a todos los superadministradores y quita
-- los triggers. La fila SUPERADMIN y la regla CHECK ampliada pueden quedarse: no estorban.
-- =====================================================================================
-- DROP TRIGGER IF EXISTS trg_users_superadmin_upd;
-- DROP TRIGGER IF EXISTS trg_users_superadmin_del;
-- UPDATE users
--    SET role_id = (SELECT role_id FROM roles WHERE code = 'ADMIN'), auth_version = auth_version + 1
--  WHERE role_id = (SELECT role_id FROM roles WHERE code = 'SUPERADMIN');
