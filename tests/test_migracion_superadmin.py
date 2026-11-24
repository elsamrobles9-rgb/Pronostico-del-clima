"""Migración segura del rol SUPERADMIN (etl/migrar_superadmin.py).

MySQL no está disponible en las pruebas: los pasos con SQL estándar corren sobre el mismo doble de
SQLite que usan las demás pruebas, y los que dependen de information_schema o de RDS se prueban con
un cursor simulado. La primera ejecución real en RDS la verifica el propio script (fotos antes/después).
"""
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "etl"))
import migrar_superadmin as M  # noqa: E402

from test_aws_features import PASSWORD, Connection, mysql_double  # noqa: E402


def cursor_sqlite(path):
    conexion = Connection(path)
    conexion.db.create_function("UTC_TIMESTAMP", 1,
                                lambda _: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"))
    return conexion, conexion.cursor()


class CursorSimulado:
    """Guarda cada sentencia; responde a information_schema con filas preparadas."""
    def __init__(self, reglas=(), falla_en=None):
        self.reglas, self.falla_en, self.sentencias, self.filas = list(reglas), falla_en, [], []

    def execute(self, sql, params=()):
        self.sentencias.append(sql)
        if self.falla_en and self.falla_en in sql:
            raise Exception(1419, "You do not have the SUPER privilege and binary logging is enabled")
        self.filas = [{"nombre": n, "regla": r} for n, r in self.reglas] if "CHECK_CONSTRAINTS" in sql else []

    def fetchall(self):
        return self.filas


class MigracionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.path = self.temp.name + "/mysql.db"
        self.store = mysql_double(self.path, self)
        for email in ("admin@example.com", "otro@example.com"):
            self.store.save_admin(email, None, PASSWORD)
        self.sesion = self.store.login("admin@example.com", PASSWORD)
        db = sqlite3.connect(self.path)
        with db:
            db.execute("DELETE FROM roles WHERE code = 'SUPERADMIN'")        # estado de antes de migrar
        db.close()
        self.conexion, self.cur = cursor_sqlite(self.path)
        self.addCleanup(self.conexion.close)

    def test_agrega_el_rol_y_promueve_sin_tocar_contrasenas(self):
        antes = M.foto(self.cur)
        self.assertEqual((antes["por_rol"], antes["super_activos"]), ({"ADMIN": 2}, []))
        self.assertEqual(M.agregar_rol(self.cur), (4, True))
        self.assertEqual(M.agregar_rol(self.cur), (4, False))               # se puede repetir
        self.assertTrue(M.promover(self.cur, "  Admin@Example.com "))
        self.conexion.commit()
        self.assertFalse(M.promover(self.cur, "admin@example.com"))          # ya era SUPERADMIN
        despues = M.foto(self.cur)
        self.assertEqual(M.verificar(antes, despues), [])
        self.assertEqual(despues["por_rol"], {"ADMIN": 1, "SUPERADMIN": 1})
        self.assertEqual(despues["super_activos"], ["admin@example.com"])
        self.assertIsNone(self.store.user(self.sesion))                      # sesiones revocadas
        nueva = self.store.user(self.store.login("admin@example.com", PASSWORD))   # misma contraseña
        self.assertEqual(nueva["role"], "SUPERADMIN")
        self.cur.execute("SELECT action, after_data FROM audit_events WHERE action = 'ROLE_CHANGE'")
        self.assertIn("SUPERADMIN", self.cur.fetchall()[0]["after_data"])

    def test_no_promueve_cuentas_que_no_deben(self):
        M.agregar_rol(self.cur)
        self.conexion.commit()
        with self.assertRaises(ValueError): M.promover(self.cur, "nadie@example.com")
        self.store.disable("otro@example.com")
        with self.assertRaises(ValueError): M.promover(self.cur, "otro@example.com")
        self.cur.execute("UPDATE users SET role_id = 3 WHERE normalized_email = 'admin@example.com'")   # ANALYST
        with self.assertRaises(ValueError): M.promover(self.cur, "admin@example.com")

    def test_verificar_detecta_cuentas_perdidas_hashes_cambiados_y_falta_de_super(self):
        antes = {"huellas": {"a": "1", "b": "2"}, "por_rol": {}, "super_activos": []}
        despues = {"huellas": {"a": "9"}, "por_rol": {}, "super_activos": []}
        problemas = " ".join(M.verificar(antes, despues))
        for texto in ("Desaparecieron 1", "Cambió el hash de 1", "ningún superadministrador"):
            self.assertIn(texto, problemas)

    def test_revertir_regresa_a_admin_sin_tocar_contrasenas(self):
        M.agregar_rol(self.cur)
        M.promover(self.cur, "admin@example.com")
        antes = M.foto(self.cur)
        self.assertEqual(M.revertir(self.cur), ["admin@example.com"])
        despues = M.foto(self.cur)
        self.assertEqual(despues["por_rol"], {"ADMIN": 2})
        self.assertEqual(antes["huellas"], despues["huellas"])

    def test_ampliar_la_regla_check(self):
        vieja = "(`code` in (_utf8mb4'ADMIN',_utf8mb4'DATA_ENTRY',_utf8mb4'ANALYST'))"
        cursor = CursorSimulado([("roles_chk_1", vieja)])
        self.assertTrue(M.ampliar_check(cursor))
        self.assertEqual(cursor.sentencias[-1], "ALTER TABLE roles DROP CHECK `roles_chk_1`, ADD CONSTRAINT "
                         "roles_code_chk CHECK (code IN ('SUPERADMIN', 'ADMIN', 'DATA_ENTRY', 'ANALYST'))")
        cursor = CursorSimulado([("roles_code_chk", "(`code` in (_utf8mb4'SUPERADMIN',_utf8mb4'ADMIN'))")])
        self.assertFalse(M.ampliar_check(cursor))
        self.assertFalse(any(s.startswith("ALTER") for s in cursor.sentencias))

    def test_si_rds_no_permite_triggers_se_explica_y_sigue(self):
        listo, error = M.instalar_triggers(CursorSimulado(falla_en="CREATE TRIGGER"))
        self.assertFalse(listo)
        self.assertEqual(error.args[0], 1419)
        cursor = CursorSimulado()
        self.assertEqual(M.instalar_triggers(cursor), (True, None))
        self.assertEqual(sum(s.startswith("CREATE TRIGGER") for s in cursor.sentencias), 2)

    def test_el_sql_manual_y_el_script_instalan_los_mismos_triggers(self):
        manual = " ".join((RAIZ / "sql" / "migracion_superadmin.sql").read_text(encoding="utf-8").split())
        for sentencia in M.TRIGGERS.values():
            self.assertIn(" ".join(sentencia.split()), manual)

    def test_sp_guardar_admin_conserva_su_firma_y_tiene_las_reglas(self):
        texto = (RAIZ / "sql" / "mysql_procedimientos.sql").read_text(encoding="utf-8")
        firma = re.search(r"CREATE PROCEDURE sp_guardar_admin\((.*?)\)\nBEGIN", texto, re.S).group(1)
        self.assertEqual((firma.count("IN "), firma.count("OUT ")), (8, 2))
        for regla in ("v_rol_actor <> 'SUPERADMIN'", "NOT IN ('ADMIN', 'SUPERADMIN')",
                      "Solo el propio superadministrador puede cambiar su contraseña"):
            self.assertIn(regla, texto)


if __name__ == "__main__":
    unittest.main()
