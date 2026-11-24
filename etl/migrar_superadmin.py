"""Migración segura del rol SUPERADMIN en MySQL climate_transactional (RDS 1).

Hace lo mismo que sql/migracion_superadmin.sql, comprobando cada paso:
  1. Foto antes: cuántas cuentas hay por rol y la huella SHA-256 del hash de cada contraseña
     (las contraseñas no se leen ni se muestran; solo se comparan huellas de los hashes).
  2. Amplía la regla CHECK de la tabla roles para aceptar SUPERADMIN (si hace falta).
  3. Agrega la fila SUPERADMIN a la tabla roles (si no existe).
  4. Reinstala sql/mysql_procedimientos.sql: sp_guardar_admin con las reglas de roles, misma firma.
  5. Instala los triggers que impiden quedarse sin superadministrador activo (si RDS lo permite).
  6. Promueve a SUPERADMIN la cuenta ADMIN que elijas: sube auth_version y revoca sus sesiones
     (el nuevo rol aplica al volver a iniciar sesión) y lo registra en audit_events.
  7. Foto después: comprueba que no desapareció ninguna cuenta, que ningún hash cambió y que hay
     al menos un superadministrador activo.
Se puede repetir sin daño. No borra cuentas ni datos.

Ejecutar desde la carpeta del proyecto, con el laboratorio y la RDS de MySQL encendidos:
    .\\.venv\\Scripts\\python.exe etl\\migrar_superadmin.py
Opciones:
    --correo ana@correo.com   la cuenta ADMIN que será superadministrador (si no se da, la pregunta)
    --revisar                 solo muestra el estado actual; no cambia nada
    --revertir                regresa a ADMIN a todos los superadministradores y quita los triggers
"""
import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARCHIVO_SP = RAIZ / "sql" / "mysql_procedimientos.sql"
CODIGOS = ("SUPERADMIN", "ADMIN", "DATA_ENTRY", "ANALYST")
NOMBRE_CHECK = "roles_code_chk"
SIN_SUPER = "Debe quedar al menos un superadministrador activo"

_CUERPO_TRIGGER = """
BEGIN
    DECLARE v_super SMALLINT;
    SET v_super = (SELECT role_id FROM roles WHERE code = 'SUPERADMIN');
    IF v_super IS NOT NULL AND OLD.role_id = v_super AND OLD.is_active = 1{extra} THEN
        IF (SELECT COUNT(*) FROM users
             WHERE role_id = v_super AND is_active = 1 AND user_id <> OLD.user_id) = 0 THEN
            SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{mensaje}';
        END IF;
    END IF;
END"""
TRIGGERS = {
    "trg_users_superadmin_upd": "CREATE TRIGGER trg_users_superadmin_upd BEFORE UPDATE ON users FOR EACH ROW"
                                + _CUERPO_TRIGGER.format(extra="\n       AND (NEW.role_id <> v_super OR NEW.is_active <> 1)",
                                                         mensaje=SIN_SUPER),
    "trg_users_superadmin_del": "CREATE TRIGGER trg_users_superadmin_del BEFORE DELETE ON users FOR EACH ROW"
                                + _CUERPO_TRIGGER.format(extra="", mensaje=SIN_SUPER),
}


# ------------------------------------------------------------------ pasos (reciben un cursor tipo pymysql)
def foto(cur):
    """Estado de las cuentas sin exponer contraseñas: huella de cada hash, cuentas por rol y supers activos."""
    cur.execute("SELECT u.user_id, u.normalized_email, u.password_hash, u.is_active, r.code AS rol"
                " FROM users u JOIN roles r ON r.role_id = u.role_id")
    filas = cur.fetchall()
    return {"huellas": {f["user_id"]: hashlib.sha256(f["password_hash"].encode("utf-8")).hexdigest() for f in filas},
            "por_rol": dict(sorted(Counter(f["rol"] for f in filas).items())),
            "super_activos": sorted(f["normalized_email"] for f in filas
                                    if f["rol"] == "SUPERADMIN" and int(f["is_active"]))}


def verificar(antes, despues):
    """Lista de problemas (vacía si la migración conservó todo)."""
    problemas = []
    faltan = set(antes["huellas"]) - set(despues["huellas"])
    if faltan:
        problemas.append(f"Desaparecieron {len(faltan)} cuenta(s).")
    cambiadas = [u for u, h in antes["huellas"].items() if u in despues["huellas"] and despues["huellas"][u] != h]
    if cambiadas:
        problemas.append(f"Cambió el hash de {len(cambiadas)} contraseña(s).")
    if not despues["super_activos"]:
        problemas.append("No hay ningún superadministrador activo.")
    return problemas


def reglas_check_de_roles(cur):
    cur.execute("SELECT tc.CONSTRAINT_NAME AS nombre, cc.CHECK_CLAUSE AS regla"
                " FROM information_schema.TABLE_CONSTRAINTS tc JOIN information_schema.CHECK_CONSTRAINTS cc"
                " ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME"
                " WHERE tc.TABLE_SCHEMA = DATABASE() AND tc.TABLE_NAME = 'roles' AND tc.CONSTRAINT_TYPE = 'CHECK'")
    return [(f["nombre"], f["regla"]) for f in cur.fetchall()]


def ampliar_check(cur):
    """Cambia la regla del código de rol en una sola sentencia (DDL atómico en InnoDB)."""
    reglas = reglas_check_de_roles(cur)
    if any("SUPERADMIN" in regla for _, regla in reglas):
        return False
    viejas = [nombre for nombre, regla in reglas if "'ADMIN'" in regla]
    lista = ", ".join(f"'{codigo}'" for codigo in CODIGOS)
    partes = [f"DROP CHECK `{nombre}`" for nombre in viejas] + [f"ADD CONSTRAINT {NOMBRE_CHECK} CHECK (code IN ({lista}))"]
    cur.execute("ALTER TABLE roles " + ", ".join(partes))
    return True


def agregar_rol(cur):
    """(role_id, creado). No hace nada si SUPERADMIN ya existe."""
    cur.execute("SELECT role_id FROM roles WHERE code = 'SUPERADMIN'")
    fila = cur.fetchone()
    if fila:
        return int(fila["role_id"]), False
    cur.execute("SELECT COALESCE(MAX(role_id), 0) + 1 AS nuevo FROM roles")
    nuevo = int(cur.fetchone()["nuevo"])
    cur.execute("INSERT INTO roles (role_id, code, name) VALUES (%s, 'SUPERADMIN', 'Superadministrador')", (nuevo,))
    return nuevo, True


def instalar_procedimientos(cur):
    from instalar_sp import sentencias_mysql     # mismo divisor de sentencias que etl/instalar_sp.py
    for sentencia in sentencias_mysql(ARCHIVO_SP.read_text(encoding="utf-8")):
        cur.execute(sentencia)


def instalar_triggers(cur):
    """(True, None) si quedaron instalados; (False, error) si RDS no lo permite."""
    try:
        for nombre, sentencia in TRIGGERS.items():
            cur.execute(f"DROP TRIGGER IF EXISTS {nombre}")
            cur.execute(sentencia)
        return True, None
    except Exception as error:   # noqa: BLE001 - se explica al usuario; la app y el SP siguen protegiendo
        return False, error


def evento(cur, correo, antes, despues, motivo):
    cur.execute("INSERT INTO audit_events (event_id, actor_id, action, entity_type, entity_key, occurred_at_utc,"
                " request_id, transaction_id, before_data, after_data, outcome, reason)"
                " VALUES (%s, NULL, 'ROLE_CHANGE', 'USER', %s, UTC_TIMESTAMP(6), %s, %s, %s, %s, 'SUCCESS', %s)",
                (str(uuid.uuid4()), correo, "migracion-superadmin", str(uuid.uuid4()),
                 json.dumps({"role": antes}), json.dumps({"role": despues}), motivo))


def cambiar_rol(cur, user_id, codigo):
    cur.execute("UPDATE users SET role_id = (SELECT role_id FROM roles WHERE code = %s),"
                " auth_version = auth_version + 1 WHERE user_id = %s", (codigo, user_id))
    cur.execute("UPDATE sessions SET revoked_at_utc = UTC_TIMESTAMP(6)"
                " WHERE user_id = %s AND revoked_at_utc IS NULL", (user_id,))


def promover(cur, correo):
    """True si la promovió; False si ya era SUPERADMIN. ValueError si no se puede."""
    correo = (correo or "").strip().lower()
    cur.execute("SELECT u.user_id, u.is_active, r.code AS rol FROM users u JOIN roles r ON r.role_id = u.role_id"
                " WHERE u.normalized_email = %s", (correo,))
    fila = cur.fetchone()
    if not fila:
        raise ValueError(f"No existe una cuenta con el correo {correo}.")
    if fila["rol"] == "SUPERADMIN":
        return False
    if fila["rol"] != "ADMIN":
        raise ValueError(f"La cuenta {correo} tiene el rol {fila['rol']}; solo se puede promover una cuenta ADMIN.")
    if not int(fila["is_active"]):
        raise ValueError(f"La cuenta {correo} está desactivada. Actívala primero.")
    cambiar_rol(cur, fila["user_id"], "SUPERADMIN")
    evento(cur, correo, "ADMIN", "SUPERADMIN", "migracion etl/migrar_superadmin.py")
    return True


def revertir(cur):
    """Quita los triggers y regresa a ADMIN a todos los superadministradores. Devuelve sus correos."""
    for nombre in TRIGGERS:
        cur.execute(f"DROP TRIGGER IF EXISTS {nombre}")
    cur.execute("SELECT u.user_id, u.normalized_email FROM users u JOIN roles r ON r.role_id = u.role_id"
                " WHERE r.code = 'SUPERADMIN'")
    supers = cur.fetchall()
    for fila in supers:
        cambiar_rol(cur, fila["user_id"], "ADMIN")
        evento(cur, fila["normalized_email"], "SUPERADMIN", "ADMIN", "reversion etl/migrar_superadmin.py")
    return [fila["normalized_email"] for fila in supers]


def estado(cur):
    print("Reglas CHECK de roles:")
    for nombre, regla in reglas_check_de_roles(cur):
        print(f"  {nombre}: {regla}")
    cur.execute("SELECT TRIGGER_NAME AS nombre FROM information_schema.TRIGGERS"
                " WHERE TRIGGER_SCHEMA = DATABASE() AND EVENT_OBJECT_TABLE = 'users' ORDER BY 1")
    print("Triggers en users: " + (", ".join(f["nombre"] for f in cur.fetchall()) or "(ninguno)"))
    cur.execute("SELECT ROUTINE_NAME AS nombre FROM information_schema.ROUTINES"
                " WHERE ROUTINE_SCHEMA = DATABASE() AND ROUTINE_TYPE = 'PROCEDURE' ORDER BY 1")
    print("Procedimientos: " + (", ".join(f["nombre"] for f in cur.fetchall()) or "(ninguno)"))


def mostrar(titulo, datos):
    roles = ", ".join(f"{rol} {n}" for rol, n in datos["por_rol"].items()) or "sin cuentas"
    supers = ", ".join(datos["super_activos"]) or "ninguno"
    print(f"{titulo}: {len(datos['huellas'])} cuenta(s) · {roles} · superadministradores activos: {supers}")


# ------------------------------------------------------------------ programa
def main():
    parser = argparse.ArgumentParser(description="Agrega el rol SUPERADMIN a MySQL sin perder cuentas ni contraseñas")
    parser.add_argument("--correo", default="", help="cuenta ADMIN que será superadministrador")
    parser.add_argument("--revisar", action="store_true", help="solo muestra el estado; no cambia nada")
    parser.add_argument("--revertir", action="store_true", help="regresa los superadministradores a ADMIN")
    args = parser.parse_args()

    import paso5_etl_postgres as p5
    conexion = p5.conectar_mysql()
    try:
        with conexion.cursor() as cur:
            antes = foto(cur)
            mostrar("Antes", antes)
            if args.revisar:
                estado(cur)
                return
            if args.revertir:
                if input("Escribe REVERTIR para regresar a ADMIN a los superadministradores: ").strip() != "REVERTIR":
                    print("No se cambió nada.")
                    return
                correos = revertir(cur)
                conexion.commit()
                print(f"Regresaron a ADMIN: {', '.join(correos) or '(ninguno)'}. Triggers quitados.")
            else:
                print("\n1/5 Regla CHECK de roles: " + ("ampliada." if ampliar_check(cur) else "ya aceptaba SUPERADMIN."))
                conexion.commit()
                role_id, creado = agregar_rol(cur)
                conexion.commit()
                print(f"2/5 Rol SUPERADMIN (role_id {role_id}): " + ("agregado." if creado else "ya existía."))
                instalar_procedimientos(cur)
                conexion.commit()
                print("3/5 sp_guardar_admin y sp_rotar_auditoria reinstalados.")
                listo, error = instalar_triggers(cur)
                conexion.commit()
                if listo:
                    print("4/5 Triggers de protección instalados.")
                else:
                    codigo = error.args[0] if getattr(error, "args", None) else ""
                    print(f"4/5 AVISO: no se pudieron crear los triggers ({type(error).__name__} {codigo}).")
                    if codigo == 1419:
                        print("     RDS tiene el registro binario activo y pide log_bin_trust_function_creators=1.")
                    print("     No es grave: el portal, la API, las Lambdas y sp_guardar_admin ya impiden quedarse"
                          " sin superadministrador. Sigue con el siguiente paso.")
                correo = args.correo
                if not correo and antes["super_activos"]:
                    print(f"5/5 Ya hay superadministrador activo ({', '.join(antes['super_activos'])}); no se promueve a nadie.")
                else:
                    correo = correo or input("5/5 Correo de la cuenta ADMIN que será superadministrador: ")
                    try:
                        promovida = promover(cur, correo)
                        conexion.commit()
                    except ValueError as exc:
                        conexion.rollback()
                        print(f"No se promovió a nadie: {exc}")
                        sys.exit(1)
                    print(f"5/5 {correo.strip().lower()} " + ("ahora es SUPERADMIN. Debe volver a iniciar sesión."
                                                             if promovida else "ya era SUPERADMIN."))
            despues = foto(cur)
            mostrar("Después", despues)
            problemas = verificar(antes, despues)
            if args.revertir:
                problemas = [p for p in problemas if "superadministrador" not in p]
            if problemas:
                print("REVISAR: " + " ".join(problemas))
                sys.exit(1)
            print("Comprobado: no se perdió ninguna cuenta y ningún hash de contraseña cambió.")
    finally:
        conexion.close()


if __name__ == "__main__":
    main()
