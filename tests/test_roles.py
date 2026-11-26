"""Roles y permisos del prompt maestro (secciones 5, 6 y 9): visitante, administrador y superadministrador.

La seguridad no depende de ocultar botones, así que cada regla se prueba en las capas por donde
alguien podría intentarla:
  1. páginas web (Flask, modo lambda, MySQL simulado con SQLite);
  2. API REST /api con JWT (peticiones HTTP manuales);
  3. opción A (Vercel): llamadas directas a ClimateApiV2 / ClimateDbV2 con un token válido;
  4. cuentas locales del modo demo (SQLite) y la migración segura de su tabla users.
"""
import io
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from flask import Flask, g
from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from contract import load_json
from services import roles
from services.access import AccessStore
from services.endpoints import ApiError, DemoClient, sample
from services.idempotency import MemoryIdempotency
from services.remoto import RemoteClient, RemoteStore, SesionVencida
from test_aws_features import PASSWORD, RecordingAnalytics, crear_super, mysql_double, reading
from test_opcion_a import pila_remota

ADMIN, OTRO, SUPER = "admin@example.com", "otro@example.com", "super@example.com"
TS = "2026-11-19T18:00:00Z"
CLAVES = f"location_id=LOC-CDMX&timestamp_utc={TS}"
SIN_SESION = ("/admin", "/records", "/records/ir?dia=2026-11-19&hora=12", "/add", f"/edit?{CLAVES}",
              f"/delete?{CLAVES}", "/import", "/import/template/csv", "/admin/users", "/admin/audit",
              "/account/password", "/account/profile")


class Base(unittest.TestCase):
    """MySQL simulado con 3 cuentas: dos ADMIN y un SUPERADMIN, y una lectura guardada."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.path = self.temp.name + "/mysql.db"
        self.store = mysql_double(self.path, self)
        self.store.save_admin(ADMIN, "Admin", PASSWORD)
        self.store.save_admin(OTRO, "Otro", PASSWORD)
        crear_super(self.store, SUPER)
        self.backend = DemoClient(seed=False)
        self.backend.create({**sample(), "timestamp_utc": TS})
        self.analytics = RecordingAnalytics()
        self.app = create_app(self.backend, "lambda", auth_path=self.temp.name + "/local.sqlite3",
                              auth_store=self.store, idempotency=MemoryIdempotency(), analytics=self.analytics)
        self.app.testing = True
        self.web = self.app.test_client()

    # ------------------------------------------------------------------ ayudas
    def sql(self, statement, params=()):
        db = sqlite3.connect(self.path)
        try:
            with db:
                return db.execute(statement, params).fetchall()
        finally:
            db.close()

    def csrf(self, cliente=None):
        cliente = cliente or self.web
        cliente.get("/login")
        with cliente.session_transaction() as s:
            return s["csrf"]

    def entrar(self, email, cliente=None, password=PASSWORD):
        cliente = cliente or self.web
        return cliente.post("/login", data={"csrf": self.csrf(cliente), "username": email, "password": password})

    def token(self, email):
        r = self.web.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, email)
        return r.get_json()["access_token"]

    def api(self, method, path, token=None, body=None, key=None, version=None):
        headers = {}
        if token: headers["Authorization"] = "Bearer " + token
        if key: headers["Idempotency-Key"] = key
        if version: headers["If-Match"] = f'"{version}"'
        data = None if body is None else __import__("contract").dumps(body)
        return self.web.open(path, method=method, headers=headers, data=data, content_type="application/json")

    def snapshot(self, path):
        html = self.web.get(path).get_data(as_text=True)
        encontrado = re.search(r'name="snapshot" value="([^"]+)"', html)
        self.assertIsNotNone(encontrado, path)
        return encontrado.group(1)

    def texto(self, respuesta):
        return respuesta.get_data(as_text=True)

    def acciones(self):
        return [a for (a,) in self.sql("SELECT action FROM audit_events")]

    def activo(self, email):
        return self.sql("SELECT is_active FROM users WHERE normalized_email = ?", (email,))[0][0] == 1

    def rol_de(self, email):
        return self.sql("SELECT r.code FROM users u JOIN roles r ON r.role_id = u.role_id"
                        " WHERE u.normalized_email = ?", (email,))[0][0]

    def nueva_lectura(self, hora="2026-11-19T20:00:00Z", **cambios):
        return {**reading(**cambios), "timestamp_utc": hora, "csrf": self.csrf()}


# ---------------------------------------------------------------------- la matriz misma
class MatrizTests(unittest.TestCase):
    ESPERADO = {   # acción: (visitante, administrador, superadministrador) — tabla del prompt maestro
        "ver_registros": (True, True, True), "agregar": (False, True, True), "editar": (False, True, True),
        "importar": (False, True, True), "eliminar": (False, False, True), "gestionar_admins": (False, False, True),
        "ver_bitacora": (False, False, True), "mi_perfil": (False, True, True),
    }

    def test_matriz_igual_a_la_del_prompt(self):
        perfiles = (None, {"role": roles.ADMIN}, {"role": roles.SUPERADMIN})
        for accion, esperado in self.ESPERADO.items():
            self.assertEqual(tuple(roles.puede(p, accion) for p in perfiles), esperado, accion)
        self.assertEqual(set(roles.MATRIZ), set(self.ESPERADO))

    def test_roles_heredados_desconocidos_y_acciones_inexistentes(self):
        for rol in ("DATA_ENTRY", "ANALYST", "ROOT", ""):
            self.assertFalse(any(roles.permisos({"role": rol}).values()), rol)
        self.assertFalse(roles.puede({"role": roles.SUPERADMIN}, "borrar_todo"))
        self.assertFalse(roles.puede({"role": roles.SUPERADMIN}, roles.DESCONOCIDA))
        self.assertEqual(roles.etiqueta({"role": roles.SUPERADMIN}), "Super administrator")
        self.assertIsNone(roles.etiqueta(None))

    def test_toda_ruta_web_declara_su_permiso(self):
        """Una ruta nueva sin declarar se niega por defecto; esta prueba obliga a declararla."""
        app = create_app(DemoClient(), auth_path=tempfile.mkdtemp() + "/x.sqlite3")
        rutas = {regla.endpoint for regla in app.url_map.iter_rules() if not regla.endpoint.startswith("api.")}
        self.assertEqual(rutas - set(roles.RUTAS), set())


# ---------------------------------------------------------------------- VISITANTE
class VisitanteTests(Base):
    def test_ve_la_pagina_principal_y_el_clima_sin_sesion(self):
        html = self.texto(self.web.get("/"))
        for texto in ("7-day forecast", "Hourly", "Admin sign-in"):
            self.assertIn(texto, html)
        self.assertEqual(self.web.get("/api/pronostico").status_code, 200)

    def test_consulta_registros_sin_sesion(self):
        html = self.texto(self.web.get("/registros"))           # por omisión: el último día con datos
        self.assertIn("Hourly records", html)
        self.assertEqual(html.count('<td class="hora">'), 24)
        self.assertIn("Previous day", html)
        html = self.texto(self.web.get("/registros?dia=2026-11-18"))
        self.assertEqual(html.count('<td class="hora">'), 24)
        self.assertIn("Next day", html)
        html = self.texto(self.web.get("/registros?dia=2030-01-01"))
        self.assertIn("There are no readings for this day.", html)
        self.assertIn("dia=2026-11-19", html)                    # enlace de regreso al último día
        self.assertIn("Choose a valid date", self.texto(self.web.get("/registros?dia=ayer")))
        r = self.web.get("/api/registros?dia=2026-11-19")
        self.assertEqual((r.status_code, len(r.get_json()["filas"]), r.get_json()["dia"]), (200, 24, "2026-11-19"))
        self.assertEqual(self.web.get("/api/registros?dia=x").status_code, 400)

    def test_no_entra_a_ninguna_ruta_administrativa_escribiendo_la_url(self):
        for ruta in SIN_SESION:
            r = self.web.get(ruta)
            self.assertEqual(r.status_code, 302, ruta)
            self.assertTrue(r.headers["Location"].startswith("/login"), ruta)

    def test_no_puede_agregar_editar_ni_eliminar_con_peticiones_manuales(self):
        csrf = self.csrf()
        posts = {"/add": {**reading(), "csrf": csrf}, f"/edit?{CLAVES}": {**reading(), "csrf": csrf},
                 f"/delete?{CLAVES}": {"csrf": csrf, "snapshot": "x"},
                 "/admin/users": {"csrf": csrf, "username": "intruso@example.com", "password": PASSWORD,
                                  "confirm": PASSWORD},
                 "/admin/users/status": {"csrf": csrf, "username": ADMIN, "action": "disable"},
                 "/admin/audit/rotate": {"csrf": csrf, "days": "0"}}
        for ruta, datos in posts.items():
            self.assertEqual(self.web.post(ruta, data=datos).status_code, 302, ruta)
        self.assertEqual(len(self.backend.table.rows), 1)
        self.assertTrue(self.activo(ADMIN))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM users WHERE normalized_email = 'intruso@example.com'")[0][0], 0)
        for metodo, ruta in (("POST", "/api/readings"), ("PUT", "/api/readings/LOC-CDMX/" + TS),
                             ("DELETE", "/api/readings/LOC-CDMX/" + TS), ("GET", "/api/readings")):
            self.assertEqual(self.api(metodo, ruta, body={}).status_code, 401, ruta)

    def test_no_existe_registro_publico_de_administradores(self):
        for ruta in ("/register", "/signup", "/admin/register", "/account/new"):
            self.assertEqual(self.web.get(ruta).status_code, 404, ruta)
        r = self.web.post("/api/auth/register", json={"email": "x@example.com", "password": PASSWORD})
        self.assertIn(r.status_code, (400, 404))                     # no existe (sin CSRF ni ruta)
        reglas = " ".join(regla.rule for regla in self.app.url_map.iter_rules())
        for palabra in ("register", "signup", "sign-up"):
            self.assertNotIn(palabra, reglas)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM users")[0][0], 3)
        self.assertIn("Ask", self.texto(self.web.get("/login")))   # el login dice que pidas la cuenta


# ---------------------------------------------------------------------- ADMINISTRADOR
class AdministradorTests(Base):
    def setUp(self):
        super().setUp()
        r = self.entrar(ADMIN)
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/admin"))

    def test_consulta_registros(self):
        for ruta in ("/admin", "/records", "/registros", f"/edit?{CLAVES}", "/import", "/account/profile",
                     "/account/password"):
            self.assertEqual(self.web.get(ruta).status_code, 200, ruta)
        self.assertEqual(self.api("GET", "/api/readings", self.token(ADMIN)).status_code, 200)

    def test_agrega_y_modifica(self):
        r = self.web.post("/add", data=self.nueva_lectura(), follow_redirects=True)
        self.assertIn("Reading saved", self.texto(r))
        self.assertEqual(len(self.backend.table.rows), 2)
        datos = {**reading(uv_index="4"), "timestamp_utc": TS, "csrf": self.csrf(),
                 "snapshot": self.snapshot(f"/edit?{CLAVES}")}
        r = self.web.post(f"/edit?{CLAVES}", data=datos, follow_redirects=True)
        self.assertIn("version 2", self.texto(r))
        token = self.token(ADMIN)
        r = self.api("PUT", "/api/readings/LOC-CDMX/" + TS, token, {"uv_index": 5}, key="key-admin-put", version=2)
        self.assertEqual(r.status_code, 200)

    def test_importa_informacion(self):
        plantilla = self.web.get("/import/template/csv").get_data()
        r = self.web.post("/import", data={"csrf": self.csrf(), "file": (io.BytesIO(plantilla), "lecturas.csv")},
                          content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        r = self.web.post(r.headers["Location"] + "/confirm", data={"csrf": self.csrf()}, follow_redirects=True)
        self.assertIn("Saved", self.texto(r))
        self.assertIn(("LOC-CDMX", "2026-10-10T06:00:00Z"), self.backend.table.rows)

    def test_no_puede_eliminar_ni_por_la_web_ni_con_peticiones_manuales(self):
        self.assertEqual(self.web.get(f"/delete?{CLAVES}").status_code, 403)
        r = self.web.post(f"/delete?{CLAVES}", data={"csrf": self.csrf(), "snapshot": "inventado"})
        self.assertEqual(r.status_code, 403)
        r = self.api("DELETE", "/api/readings/LOC-CDMX/" + TS, self.token(ADMIN), version=1)
        self.assertEqual((r.status_code, r.get_json()["error"]), (403, "forbidden"))
        self.assertEqual(len(self.backend.table.rows), 1)
        self.assertIn("ACCESS_DENIED", self.acciones())

    def test_no_puede_crear_ni_gestionar_administradores(self):
        csrf = self.csrf()
        self.assertEqual(self.web.get("/admin/users").status_code, 403)
        r = self.web.post("/admin/users", data={"csrf": csrf, "username": "nuevo@example.com", "display_name": "N",
                                                "password": PASSWORD, "confirm": PASSWORD, "intent": "create"})
        self.assertEqual(r.status_code, 403)
        r = self.web.post("/admin/users", data={"csrf": csrf, "username": OTRO, "password": "otra-clave-12345",
                                                "confirm": "otra-clave-12345"})
        self.assertEqual(r.status_code, 403)
        r = self.web.post("/admin/users/status", data={"csrf": csrf, "username": OTRO, "action": "disable"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self.activo(OTRO))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM users WHERE normalized_email = 'nuevo@example.com'")[0][0], 0)
        self.assertTrue(self.store.login(OTRO, PASSWORD))         # su contraseña no cambió
        with self.assertRaises(ValueError):                         # tampoco llamando al almacén con su sesión
            self.store.save_admin("nuevo@example.com", None, PASSWORD, actor=self.store.user(self.token(ADMIN)))

    def test_no_accede_a_funciones_del_superadministrador(self):
        self.assertEqual(self.web.get("/admin/audit").status_code, 403)
        r = self.web.post("/admin/audit/rotate", data={"csrf": self.csrf(), "days": "0"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("You do not have permission", self.texto(self.web.get("/admin/users")))
        self.assertNotIn("AUDIT_ROTATE", self.acciones())

    def test_no_ve_otras_cuentas_ni_su_actividad(self):
        otro = self.app.test_client()
        self.entrar(SUPER, otro)
        html = self.texto(self.web.get("/admin"))
        for ajeno in (OTRO, SUPER, "Administrator created", "Role changed"):
            self.assertNotIn(ajeno, html)
        self.assertIn("Sign-in", html)                                # su propio inicio de sesión sí
        html = self.texto(self.web.get("/account/profile"))
        self.assertIn(ADMIN, html)
        self.assertNotIn(OTRO, html)

    def test_edita_su_perfil_pero_no_su_rol(self):
        csrf = self.csrf()
        r = self.web.post("/account/profile", data={"csrf": csrf, "display_name": "Ana Admin"}, follow_redirects=True)
        self.assertIn("Your profile was updated", self.texto(r))
        self.assertIn("Ana Admin", self.texto(self.web.get("/account/profile")))
        for intento in ({"role": "SUPERADMIN"}, {"role_id": "4"}, {"is_active": "1"}):
            r = self.web.post("/account/profile", data={"csrf": csrf, "display_name": "X", **intento},
                              follow_redirects=True)
            self.assertIn("unexpected data", self.texto(r))
        self.assertIn("1 to 120 characters", self.texto(self.web.post(
            "/account/profile", data={"csrf": csrf, "display_name": "  "}, follow_redirects=True)))
        self.assertEqual(self.rol_de(ADMIN), "ADMIN")
        self.assertEqual(self.sql("SELECT display_name FROM users WHERE normalized_email = ?", (ADMIN,))[0][0],
                         "Ana Admin")

    def test_cuenta_desactivada_y_sesion_vencida(self):
        self.store.disable(ADMIN)                                     # lo hace el superadministrador
        r = self.web.get("/records")
        self.assertTrue(r.headers["Location"].startswith("/login"))
        self.assertIn("Your session ended", self.texto(self.web.get("/login")))
        self.assertIn("We could not sign you in", self.texto(self.entrar(ADMIN)))
        self.store.enable(ADMIN)
        self.entrar(ADMIN)
        viejo = (datetime.utcnow() - timedelta(minutes=31)).strftime("%Y-%m-%d %H:%M:%S.%f")
        self.sql("UPDATE sessions SET last_activity_at_utc = ?", (viejo,))
        self.assertTrue(self.web.get("/records").headers["Location"].startswith("/login"))

    def test_formularios_invalidos_no_escriben(self):
        r = self.web.post("/add", data=self.nueva_lectura(humidity_pct="101"), follow_redirects=True)
        self.assertNotIn("Reading saved", self.texto(r))
        self.assertEqual(len(self.backend.table.rows), 1)
        r = self.web.post("/add", data={**self.nueva_lectura(), "role": "SUPERADMIN"}, follow_redirects=True)
        self.assertIn("unexpected data", self.texto(r))
        r = self.web.post("/account/password", data={"csrf": self.csrf(), "current": PASSWORD,
                                                     "password": "nueva-clave-123", "confirm": "otra-clave-123"},
                          follow_redirects=True)
        self.assertIn("do not match", self.texto(r))


# ---------------------------------------------------------------------- SUPERADMINISTRADOR
class SuperadministradorTests(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.entrar(SUPER).status_code, 302)

    def test_consulta_agrega_modifica_y_elimina(self):
        for ruta in ("/admin", "/records", "/admin/users", "/admin/audit", "/account/profile"):
            self.assertEqual(self.web.get(ruta).status_code, 200, ruta)
        self.web.post("/add", data=self.nueva_lectura())
        self.assertEqual(len(self.backend.table.rows), 2)
        datos = {"csrf": self.csrf(), "snapshot": self.snapshot(f"/delete?{CLAVES}")}
        r = self.web.post(f"/delete?{CLAVES}", data=datos, follow_redirects=True)
        self.assertIn("Reading deleted", self.texto(r))
        self.assertEqual(len(self.backend.table.rows), 1)
        r = self.api("DELETE", "/api/readings/LOC-CDMX/2026-11-19T20:00:00Z", self.token(SUPER), version=1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.backend.table.rows), 0)

    def test_crea_administradores_con_validaciones(self):
        def alta(**cambios):
            datos = {"csrf": self.csrf(), "username": "nuevo@example.com", "display_name": "Nuevo",
                     "password": PASSWORD, "confirm": PASSWORD, "intent": "create", **cambios}
            return self.texto(self.web.post("/admin/users", data=datos, follow_redirects=True))
        self.assertIn("Administrator created", alta())
        self.assertEqual(self.rol_de("nuevo@example.com"), "ADMIN")       # siempre ADMIN, nunca SUPERADMIN
        self.assertIn("already exists", alta())                              # duplicado
        self.assertIn("already exists", alta(username="NUEVO@Example.com "))
        self.assertIn("valid email", alta(username="no-es-correo"))
        self.assertIn("12 to 128", alta(username="corta@example.com", password="corta", confirm="corta"))
        self.assertIn("do not match", alta(username="otra@example.com", confirm="distinta-clave-99"))
        self.assertIn("unexpected data", alta(username="rol@example.com", role="SUPERADMIN"))
        self.assertIn("does not exist", alta(username="nadie@example.com", intent="password"))
        self.assertIn("Unknown form action", alta(username="x@example.com", intent="promote"))
        for email in ("corta@example.com", "otra@example.com", "rol@example.com", "nadie@example.com"):
            self.assertEqual(self.sql("SELECT COUNT(*) FROM users WHERE normalized_email = ?", (email,))[0][0], 0)
        self.assertIn("Password changed", alta(intent="password", password="cambiada-123456",
                                               confirm="cambiada-123456"))
        self.assertTrue(self.store.login("nuevo@example.com", "cambiada-123456"))

    def test_consulta_las_cuentas_existentes(self):
        html = self.texto(self.web.get("/admin/users"))
        for email in (ADMIN, OTRO, SUPER):
            self.assertIn(email, html)
        self.assertEqual({u["username"]: u["role"] for u in self.store.list_users()},
                         {ADMIN: "ADMIN", OTRO: "ADMIN", SUPER: "SUPERADMIN"})
        html = self.texto(self.web.get("/admin/audit"))
        self.assertIn("Role changed", html)

    def test_desactiva_y_reactiva_administradores(self):
        sesion_otro = self.store.login(OTRO, PASSWORD)
        r = self.web.post("/admin/users/status", data={"csrf": self.csrf(), "username": OTRO, "action": "disable"},
                          follow_redirects=True)
        self.assertIn("Administrator disabled", self.texto(r))
        self.assertIsNone(self.store.user(sesion_otro))                   # sesión revocada al instante
        self.assertIsNone(self.store.login(OTRO, PASSWORD))
        r = self.web.post("/admin/users/status", data={"csrf": self.csrf(), "username": OTRO, "action": "enable"},
                          follow_redirects=True)
        self.assertIn("Administrator enabled", self.texto(r))
        self.assertTrue(self.store.login(OTRO, PASSWORD))

    def test_no_toca_su_propia_cuenta_ni_la_de_otro_superadministrador(self):
        crear_super(self.store, "super2@example.com")
        r = self.web.post("/admin/users/status", data={"csrf": self.csrf(), "username": SUPER, "action": "disable"},
                          follow_redirects=True)
        self.assertIn("cannot disable your own account", self.texto(r))
        r = self.web.post("/admin/users/status", data={"csrf": self.csrf(), "username": "super2@example.com",
                                                       "action": "disable"}, follow_redirects=True)
        self.assertIn("cannot be disabled", self.texto(r))
        r = self.web.post("/admin/users", data={"csrf": self.csrf(), "username": "super2@example.com",
                                                "password": "robada-clave-123", "confirm": "robada-clave-123"},
                          follow_redirects=True)
        self.assertIn("owns that account", self.texto(r))
        r = self.web.post("/admin/users", data={"csrf": self.csrf(), "username": SUPER, "password": "propia-clave-123",
                                                "confirm": "propia-clave-123"}, follow_redirects=True)
        self.assertIn("My password", self.texto(r))
        self.assertTrue(self.activo("super2@example.com"))
        self.assertTrue(self.store.login("super2@example.com", PASSWORD))

    def test_cambia_su_propia_contrasena(self):
        r = self.web.post("/account/password", data={"csrf": self.csrf(), "current": PASSWORD,
                                                     "password": "super-nueva-123", "confirm": "super-nueva-123"})
        self.assertEqual(r.headers["Location"], "/login")
        self.assertTrue(self.store.login(SUPER, "super-nueva-123"))

    def test_nunca_queda_sin_superadministrador_activo(self):
        with self.assertRaises(ValueError): self.store.disable(SUPER)                 # línea de comandos
        with self.assertRaises(ValueError): self.store.set_role(SUPER, roles.ADMIN)
        crear_super(self.store, "super2@example.com")
        self.assertTrue(self.store.set_role(SUPER, roles.ADMIN))                       # ya hay otro
        with self.assertRaises(ValueError): self.store.set_role("super2@example.com", roles.ADMIN)
        with self.assertRaises(ValueError): self.store.disable("super2@example.com")
        self.assertEqual(self.store.count_superadmins(), 1)
        with self.assertRaises(ValueError): self.store.set_role(ADMIN, "ROOT")


# ---------------------------------------------------------------------- opción A: peticiones directas
class PeticionesDirectasOpcionATests(unittest.TestCase):
    """Lo que alguien podría mandar a API Gateway saltándose el portal, con su propio token."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.store = mysql_double(self.temp.name + "/mysql.db", self)
        self.store.save_admin(ADMIN, "Admin", PASSWORD)
        self.store.save_admin(OTRO, "Otro", PASSWORD)
        crear_super(self.store, SUPER)
        self.backend = DemoClient(seed=False)
        self.backend.create({**sample(), "timestamp_utc": TS})
        _, self.api, _ = pila_remota(self.store, self.backend, RecordingAnalytics())
        self.admin = self.api.llamar("login", email=ADMIN, password=PASSWORD)
        self.super = self.api.llamar("login", email=SUPER, password=PASSWORD)

    def borrar(self, token):
        return self.api.llamar("lectura", token, operation="delete",
                               data={"location_id": "LOC-CDMX", "timestamp_utc": TS, "expected_version": 1})

    def test_admin_no_elimina_aunque_llame_directo(self):
        with self.assertRaises(ApiError) as error:
            self.borrar(self.admin)
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(len(self.backend.table.rows), 1)
        self.borrar(self.super)
        self.assertEqual(len(self.backend.table.rows), 0)

    def test_admin_si_agrega_y_edita_por_la_api(self):
        with Flask(__name__).test_request_context():
            g.token = self.admin
            cliente = RemoteClient(self.api)
            self.assertEqual(cliente.create({**sample(), "timestamp_utc": "2026-11-19T21:00:00Z"})["record"]["version"], 1)
            self.assertEqual(cliente.update({**sample(), "timestamp_utc": TS, "uv_index": 7,
                                             "expected_version": 1})["record"]["version"], 2)

    def test_admin_no_gestiona_cuentas_ni_bitacora(self):
        llamadas = {"list_users": {}, "count_admins": {}, "history_count": {}, "rotate_audit": {"days": 0},
                    "disable": {"email": OTRO}, "enable": {"email": OTRO},
                    "save_admin": {"email": "nuevo@example.com", "display_name": None, "password": PASSWORD}}
        for metodo, args in llamadas.items():
            with self.assertRaises(ValueError, msg=metodo) as error:
                self.api.llamar(metodo, self.admin, **args)
            self.assertIn("Permission denied", str(error.exception), metodo)
        self.assertTrue(self.store.login(OTRO, PASSWORD))
        self.assertEqual(len(self.api.llamar("list_users", self.super)), 3)

    def test_admin_solo_ve_sus_eventos(self):
        propios = self.api.llamar("audit_log", self.admin, limit=100)
        self.assertTrue(propios)
        self.assertEqual({e["actor"] for e in propios}, {ADMIN})
        todos = self.api.llamar("audit_log", self.super, limit=100)
        self.assertGreater(len({e["actor"] for e in todos}), 1)

    def test_nadie_cambia_roles_por_la_api(self):
        for token in (self.admin, self.super):
            with self.assertRaises(ValueError):
                self.api.llamar("set_role", token, email=ADMIN, role="SUPERADMIN")
        with self.assertRaises(ValueError):
            RemoteStore(self.api).set_role(ADMIN, "SUPERADMIN")
        self.assertEqual(self.store.list_users()[0]["role"], "ADMIN")
        self.assertEqual(self.api.llamar("my_profile", self.admin)["role"], "ADMIN")

    def test_perfil_propio_y_registros_publicos(self):
        self.api.llamar("update_own_profile", self.admin, display_name="Admin Nuevo")
        self.assertEqual(self.api.llamar("my_profile", self.admin)["display_name"], "Admin Nuevo")
        with self.assertRaises(SesionVencida):
            self.api.llamar("my_profile", "")
        datos = self.api.llamar("lecturas_dia")                         # público, sin token
        self.assertEqual(len(datos["filas"]), 24)

    def test_token_de_cuenta_desactivada_ya_no_sirve(self):
        self.store.disable(ADMIN)
        with self.assertRaises(SesionVencida):
            self.api.llamar("audit_log", self.admin, limit=5)


# ---------------------------------------------------------------------- modo demo (SQLite)
class CuentasLocalesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.path = self.temp.name + "/portal.sqlite3"

    def test_reglas_iguales_a_mysql(self):
        store = AccessStore(self.path)
        store.add_user("super", PASSWORD, roles.SUPERADMIN)
        store.add_user("admin", PASSWORD, roles.ADMIN)
        store.add_user("otro", PASSWORD, roles.ADMIN)
        admin, sup = store.user(store.login("admin", PASSWORD)), store.user(store.login("super", PASSWORD))
        self.assertEqual((admin["role"], sup["role"]), ("ADMIN", "SUPERADMIN"))
        with self.assertRaises(ValueError): store.save_admin("nuevo", None, PASSWORD, actor=admin)
        with self.assertRaises(ValueError): store.disable("otro", actor=admin)
        with self.assertRaises(ValueError): store.save_admin("otro", None, "cambio-clave-123", actor=admin)
        self.assertEqual(store.save_admin("nuevo", "Nuevo", PASSWORD, actor=sup, mode="create"), "CREADO")
        with self.assertRaises(ValueError): store.save_admin("nuevo", None, PASSWORD, actor=sup, mode="create")
        with self.assertRaises(ValueError): store.add_user("x" * 5, PASSWORD, roles.SUPERADMIN, actor=sup)
        store.disable("otro", actor=sup)
        self.assertIsNone(store.login("otro", PASSWORD))
        with self.assertRaises(ValueError): store.disable("super", actor=sup)          # su propia cuenta
        with self.assertRaises(ValueError): store.disable("super")                     # el último superadmin
        with self.assertRaises(ValueError): store.set_role("super", roles.ADMIN)
        store.update_own_profile(admin, "Ana")
        self.assertEqual(store.profile(admin)["display_name"], "Ana")
        self.assertEqual({e["actor"] for e in store.audit_log(50, actor=admin)}, {"admin"})
        store.add_user("lector", PASSWORD, "ANALYST")                                  # rol heredado: no entra
        self.assertIsNone(store.login("lector", PASSWORD))

    def test_migracion_segura_de_una_base_local_anterior(self):
        """Una base creada por la versión anterior (sin display_name ni created) se actualiza sin perder
        cuentas ni hashes."""
        viejo_hash = generate_password_hash(PASSWORD)
        db = sqlite3.connect(self.path)
        with db:
            db.execute("CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL,"
                       " active INTEGER NOT NULL DEFAULT 1)")
            db.execute("INSERT INTO users VALUES ('admin', ?, 'ADMIN', 1)", (viejo_hash,))
        db.close()
        store = AccessStore(self.path)
        db = sqlite3.connect(self.path)
        fila = db.execute("SELECT password_hash, role, display_name FROM users WHERE username='admin'").fetchone()
        db.close()
        self.assertEqual(fila[:2], (viejo_hash, "ADMIN"))
        self.assertTrue(check_password_hash(fila[0], PASSWORD))
        self.assertTrue(store.user(store.login("admin", PASSWORD)))
        self.assertEqual(store.profile({"username": "admin"})["display_name"], "admin")


if __name__ == "__main__":
    unittest.main()
