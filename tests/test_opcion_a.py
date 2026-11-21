"""Opción A: portal (Vercel o PC) -> API Gateway -> ClimateApiV2 -> {4 Lambdas, ClimateIdempotency, ClimateDbV2}.

Aquí toda la cadena corre en memoria: el portal manda HTTP (eventos de API Gateway) a la Lambda
ClimateApiV2, que invoca a ClimateDbV2 con JSON real. MySQL es el doble de SQLite de las otras
pruebas; las 4 Lambdas son DemoClient. Además se repiten TODAS las pruebas de la API y de la web
del modo lambda, pero pasando por la API remota.
"""
import json
import os
import unittest
from unittest import mock

import app as modulo_app
from app import create_app
from backend.lambda_api import Puerta
from backend.lambda_db import ServicioDatos
from services.endpoints import ApiError, DemoClient
from services.idempotency import MemoryIdempotency
from services.remoto import (ApiRemota, RemoteAnalytics, RemoteClient, RemoteIdempotency, RemoteStore,
                             SesionVencida)
from services.rpc_json import codificar, decodificar
from test_aws_features import PASSWORD, ApiTests, RecordingAnalytics, WebTests, mysql_double


def pila_remota(store, lecturas, analytics, idempotencia=None):
    """Arma ClimateDbV2 + ClimateApiV2 + el cliente del portal, conectados con JSON como en AWS."""
    servicio = ServicioDatos(store, analytics)
    tabla = idempotencia or MemoryIdempotency()

    def invocar_db(peticion):                       # boto3 lambda.invoke: solo viaja JSON
        return json.loads(json.dumps(servicio.atender(json.loads(json.dumps(peticion)))))

    puerta = Puerta(invocar_db, lecturas, lambda: tabla)

    def transporte(ruta, cuerpo):                   # API Gateway HTTP API (payload 2.0)
        evento = {"rawPath": ruta, "requestContext": {"http": {"method": "POST" if cuerpo else "GET"}},
                  "body": cuerpo.decode("utf-8") if cuerpo else None, "isBase64Encoded": False}
        r = puerta.atender_http(evento)
        return r["statusCode"], r["body"].encode("utf-8")

    api = ApiRemota("https://prueba.execute-api.us-east-1.amazonaws.com", transporte=transporte)
    partes = dict(client=RemoteClient(api), auth_store=RemoteStore(api), analytics=RemoteAnalytics(api),
                  idempotency=RemoteIdempotency(api))
    return partes, api, servicio


class RemoteApiTests(ApiTests):
    """Las mismas pruebas de la API REST, ahora por la opción A."""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.path = self.temp.name + "/mysql.db"
        self.store = mysql_double(self.path, self)
        for email in ("admin@example.com", "second@example.com"):
            self.store.save_admin(email, None, PASSWORD)
        self.backend = DemoClient(seed=False)
        self.analytics = RecordingAnalytics()
        partes, self.api, _ = pila_remota(self.store, self.backend, self.analytics)
        self.app = create_app(mode="api", auth_path=self.temp.name + "/local.sqlite3", **partes)
        self.app.testing = True
        self.web = self.app.test_client()


class RemoteWebTests(WebTests):
    """Las mismas pruebas de las páginas web, ahora por la opción A."""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.store = mysql_double(self.temp.name + "/mysql.db", self)
        self.store.save_admin("admin@example.com", "Admin", PASSWORD)
        self.backend = DemoClient(seed=False)
        self.analytics = RecordingAnalytics()
        partes, self.api, _ = pila_remota(self.store, self.backend, self.analytics)
        self.app = create_app(mode="api", auth_path=self.temp.name + "/local.sqlite3", **partes)
        self.app.testing = True
        self.web = self.app.test_client()

    def test_peticiones_del_navegador_no_rompen_el_login(self):
        """El navegador pide /favicon.ico y otras rutas mientras muestra el login: el token debe seguir valiendo."""
        self.web.get("/login")
        token = self.csrf()
        self.assertEqual(self.web.get("/favicon.ico").status_code, 204)
        self.assertEqual(self.web.get("/robots.txt").status_code, 404)
        self.assertEqual(self.web.get("/apple-touch-icon.png").status_code, 404)
        self.web.get("/records")                       # página protegida sin sesión: manda al login
        r = self.web.post("/login", data={"csrf": token, "username": "admin@example.com", "password": PASSWORD})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/records"))

    def test_aviso_modo_api(self):
        self.assertIn("AWS API · option A", self.web.get("/login").get_data(as_text=True))


class SeguridadOpcionATests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.store = mysql_double(self.temp.name + "/mysql.db", self)
        self.store.save_admin("admin@example.com", "Admin", PASSWORD)
        self.store.save_admin("otro@example.com", "Otro", PASSWORD)
        self.backend = DemoClient(seed=False)
        self.analytics = RecordingAnalytics()
        _, self.api, self.servicio = pila_remota(self.store, self.backend, self.analytics)

    def test_json_conserva_fechas_y_decimales(self):
        from datetime import date, datetime, timezone
        from decimal import Decimal
        valor = {"d": date(2026, 11, 20), "t": datetime(2026, 11, 20, 6, tzinfo=timezone.utc),
                 "x": Decimal("21.40"), "l": [1, "a", None]}
        self.assertEqual(decodificar(codificar(valor)), valor)

    def test_sin_token_no_hay_administracion(self):
        for metodo in ("list_users", "audit_log", "save_admin", "rotate_audit", "upsert", "lectura", "idem_begin"):
            with self.assertRaises(SesionVencida, msg=metodo):
                self.api.llamar(metodo, "", email="x@example.com", display_name=None, password=PASSWORD,
                                days=30, records=[{}], origen="x", fuentes=[], condiciones=[],
                                operation="read", data={}, scope="a#b", print_="p")

    def test_token_falso_o_revocado(self):
        with self.assertRaises(SesionVencida):
            self.api.llamar("list_users", "a.b.c")
        token = self.api.llamar("login", email="admin@example.com", password=PASSWORD)
        self.assertEqual(len(self.api.llamar("list_users", token)), 2)
        self.api.llamar("logout", token=token)
        with self.assertRaises(SesionVencida):
            self.api.llamar("list_users", token)

    def test_publico_sin_token(self):
        self.assertTrue(self.api.salud())
        self.assertEqual(len(self.api.llamar("pagina")["dias"]), 7)
        self.assertIsNone(self.api.llamar("login", email="admin@example.com", password="mala-contrasena"))
        estado = self.api.llamar("estado")
        self.assertEqual(estado["mysql_admins"], 2)

    def test_el_actor_sale_del_token(self):
        token = self.api.llamar("login", email="otro@example.com", password=PASSWORD)
        self.api.llamar("audit", token, action="PRUEBA", entity_type="READING", entity_key="k", outcome="SUCCESS")
        ultimo = [e for e in self.store.audit_log() if e["action"] == "PRUEBA"][0]
        self.assertEqual(ultimo["actor"], "otro@example.com")

    def test_idempotencia_de_otro_usuario(self):
        token = self.api.llamar("login", email="admin@example.com", password=PASSWORD)
        with self.assertRaises(ValueError):
            self.api.llamar("idem_begin", token, scope="otro-usuario#clave", print_="p")

    def test_lecturas_pasan_por_las_lambdas(self):
        from services.endpoints import sample
        from flask import Flask, g
        token = self.api.llamar("login", email="admin@example.com", password=PASSWORD)
        cliente = RemoteClient(self.api)
        with Flask(__name__).test_request_context():
            g.token = token
            creada = cliente.create({**sample(), "timestamp_utc": "2026-11-19T06:00:00Z"})
            self.assertEqual(creada["record"]["version"], 1)
            filas, _ = cliente.list_records()
            self.assertEqual(len(filas), 1)
            g.token = "malo"
            with self.assertRaises(ApiError) as error:
                cliente.list_records()
            self.assertEqual(error.exception.status, 401)

    def test_api_caida_da_error_claro(self):
        def sin_red(ruta, cuerpo):
            raise OSError("sin red")
        api = ApiRemota("https://x.execute-api.us-east-1.amazonaws.com", transporte=sin_red)
        from services.mysql_store import DatabaseUnavailable
        with self.assertRaises(DatabaseUnavailable):
            RemoteStore(api).login("a@example.com", "x")

    def test_rutas_de_la_puerta(self):
        puerta = Puerta(lambda p: {"ok": None}, None, None)
        self.assertEqual(puerta.atender_http({"rawPath": "/nada", "requestContext": {"http": {"method": "GET"}}})["statusCode"], 404)
        self.assertEqual(puerta.atender_http({"rawPath": "/rpc", "requestContext": {"http": {"method": "POST"}},
                                              "body": "no es json"})["statusCode"], 400)

    def test_url_debe_ser_https(self):
        with self.assertRaises(ValueError):
            ApiRemota("http://ejemplo.com")


class VercelTests(unittest.TestCase):
    def test_fuera_de_vercel_no_crea_nada(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VERCEL", None)
            self.assertIsNone(modulo_app.crear_app_desde_entorno())

    def test_app_py_tiene_app_flask_de_nivel_superior(self):
        """Lo que revisa Vercel al construir: app.py define app = Flask(...) arriba del todo."""
        import ast
        from flask import Flask
        from pathlib import Path
        arbol = ast.parse(Path(modulo_app.__file__).read_text(encoding="utf-8"))
        nombres = [t.id for n in arbol.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)]
        self.assertIn("app", nombres)
        self.assertIn("app = Flask(__name__)", Path(modulo_app.__file__).read_text(encoding="utf-8"))
        self.assertIsInstance(modulo_app.app, Flask)

    def test_vercel_sin_configurar_avisa(self):
        with mock.patch.dict(os.environ, {"VERCEL": "1", "CLIMA_API_URL": "", "CLIMA_SECRET_KEY": ""}):
            web = modulo_app.crear_app_desde_entorno().test_client()
            r = web.get("/cualquier/ruta")
            self.assertEqual(r.status_code, 503)
            self.assertIn("CLIMA_API_URL", r.get_data(as_text=True))

    def test_vercel_configurado_usa_la_api(self):
        with mock.patch.dict(os.environ, {"VERCEL": "1", "CLIMA_API_URL": "https://x.execute-api.us-east-1.amazonaws.com",
                                          "CLIMA_SECRET_KEY": "s" * 40}):
            web = modulo_app.crear_app_desde_entorno()
            self.assertEqual(web.config["CLIMATE_MODE"], "api")
            self.assertEqual(web.secret_key, "s" * 40)
            self.assertTrue(web.config["SESSION_COOKIE_SECURE"])
            self.assertEqual(web.config["SESSION_COOKIE_SAMESITE"], "Lax")
            self.assertEqual(web.extensions["auth_store"].kind, "mysql (API)")

    def test_vercel_lee_formularios_sin_content_length(self):
        """Simula una plataforma que entrega el cuerpo sin CONTENT_LENGTH: el login debe leer el csrf."""
        import io
        import tempfile
        from test_aws_features import RecordingAnalytics, mysql_double
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(temp.cleanup)
        store = mysql_double(temp.name + "/m.db", self)
        store.save_admin("admin@example.com", "Admin", PASSWORD)
        partes, _, _ = pila_remota(store, DemoClient(seed=False), RecordingAnalytics())
        with mock.patch.dict(os.environ, {"VERCEL": "1", "CLIMA_API_URL": "https://x.execute-api.us-east-1.amazonaws.com",
                                          "CLIMA_SECRET_KEY": "s" * 40}), \
                mock.patch.object(modulo_app, "remote_backend", return_value=(partes, None)):
            web = modulo_app.crear_app_desde_entorno()
        cliente = web.test_client()
        html = cliente.get("/login", base_url="https://portal.example").get_data(as_text=True)
        self.assertIn("Email", html)
        with cliente.session_transaction(base_url="https://portal.example") as s:
            csrf = s["csrf"]
        cuerpo = f"csrf={csrf}&username=admin%40example.com&password={PASSWORD}".encode()
        r = cliente.post("/login", base_url="https://portal.example", input_stream=io.BytesIO(cuerpo),
                         content_type="application/x-www-form-urlencoded",
                         environ_overrides={"CONTENT_LENGTH": ""})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/records"))


del ApiTests, WebTests   # que no se ejecuten dos veces desde este archivo

if __name__ == "__main__":
    unittest.main()
