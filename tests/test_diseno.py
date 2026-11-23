"""Nueva administración (estilo C): páginas en español, botones de regreso y avisos claros de fallos."""
import tempfile
import unittest
from pathlib import Path

from app import create_app
from services.avisos import explicar, hora_local
from services.endpoints import ApiError, DemoClient, sample
from services.mysql_store import DatabaseUnavailable
from test_aws_features import PASSWORD, RecordingAnalytics, mysql_double

PLANTILLAS = Path(__file__).resolve().parent.parent / "templates"


class AnalyticsCaida(RecordingAnalytics):
    def pagina(self, *a, **k):
        raise DatabaseUnavailable("Cannot connect to PostgreSQL.")


class DisenoTests(unittest.TestCase):
    def setUp(self, analytics=None):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.store = mysql_double(self.temp.name + "/mysql.db", self)
        self.store.save_admin("admin@example.com", "Admin", PASSWORD)
        self.backend = DemoClient(seed=False)
        self.app = create_app(self.backend, "lambda", auth_path=self.temp.name + "/local.sqlite3",
                              auth_store=self.store, analytics=analytics or RecordingAnalytics())
        self.app.testing = True
        self.web = self.app.test_client()

    def csrf(self):
        with self.web.session_transaction() as s: return s["csrf"]

    def entrar(self):
        self.web.get("/login")
        return self.web.post("/login", data={"csrf": self.csrf(), "username": "admin@example.com", "password": PASSWORD})

    def test_al_entrar_llega_al_inicio(self):
        r = self.entrar()
        self.assertTrue(r.headers["Location"].endswith("/admin"))
        html = self.web.get("/admin").get_data(as_text=True)
        for texto in ("What would you like to do today?", "View and edit readings", "Import readings", "System status",
                      "Recent activity", "Sign-in"):
            self.assertIn(texto, html)

    def test_cada_pagina_tiene_regresar_y_ruta(self):
        self.entrar()
        for ruta in ("/records", "/add", "/import", "/admin/users", "/admin/audit", "/account/password"):
            html = self.web.get(ruta).get_data(as_text=True)
            self.assertIn('class="ad-btn regresar"', html, ruta)
            self.assertIn('aria-label="Breadcrumb"', html, ruta)
            self.assertIn('lang="en"', html, ruta)

    def test_login_fallido_explica_el_bloqueo(self):
        self.web.get("/login")
        html = self.web.post("/login", data={"csrf": self.csrf(), "username": "admin@example.com",
                                             "password": "contrasena-mala-123"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("We could not sign you in", html)
        self.assertIn("15 minutes", html)

    def test_sesion_vencida_avisa_y_regresa_a_la_pagina(self):
        self.entrar()
        with self.web.session_transaction() as s:
            token = s["sid"]
        self.store.logout(token)                                      # p. ej. se cerró desde otro lugar
        r = self.web.get("/admin/audit")
        self.assertTrue(r.headers["Location"].endswith("/login"))
        self.assertIn("Your session ended", self.web.get("/login").get_data(as_text=True))
        r = self.web.post("/login", data={"csrf": self.csrf(), "username": "admin@example.com", "password": PASSWORD})
        self.assertTrue(r.headers["Location"].endswith("/admin/audit"))

    def test_hora_sin_lectura_avisa(self):
        self.entrar()
        html = self.web.get("/records/ir?dia=2026-11-19&hora=10", follow_redirects=True).get_data(as_text=True)
        self.assertIn("There is no reading saved for Nov 19, 2026, 10:00", html)
        html = self.web.get("/records/ir?dia=mal&hora=10", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Choose a valid day and hour", html)

    def test_ir_a_hora_abre_la_lectura(self):
        self.entrar()
        self.backend.create({**sample(), "timestamp_utc": "2026-11-19T16:00:00Z"})
        r = self.web.get("/records/ir?dia=2026-11-19&hora=10")
        self.assertIn("timestamp_utc=2026-11-19T16:00:00Z", r.headers["Location"])
        self.assertIn("Before you edit", self.web.get(r.headers["Location"]).get_data(as_text=True))

    def test_conflicto_de_version_en_espanol(self):
        self.entrar()
        self.backend.create({**sample(), "timestamp_utc": "2026-11-19T16:00:00Z"})
        ruta = "/edit?location_id=LOC-CDMX&timestamp_utc=2026-11-19T16:00:00Z"
        self.web.get(ruta)
        with self.web.session_transaction() as s:
            csrf = s["csrf"]
        import re
        html = self.web.get(ruta).get_data(as_text=True)
        snapshot = re.search(r'name="snapshot" value="([^"]+)"', html).group(1)
        actual = self.backend.get({"location_id": "LOC-CDMX", "timestamp_utc": "2026-11-19T16:00:00Z"})
        self.backend.update({**{k: actual[k] for k in sample()}, "temperature_c": 30, "expected_version": 1})
        datos = {k: str(actual[k]) for k in sample()}
        datos.update(csrf=csrf, snapshot=snapshot, temperature_c="20")
        html = self.web.post(ruta, data=datos).get_data(as_text=True)
        self.assertIn("Someone else saved this reading before you", html)
        self.assertIn("Technical detail", html)

    def test_borrar_pide_confirmacion_y_explica(self):
        self.entrar()
        self.backend.create({**sample(), "timestamp_utc": "2026-11-19T16:00:00Z"})
        html = self.web.get("/delete?location_id=LOC-CDMX&timestamp_utc=2026-11-19T16:00:00Z").get_data(as_text=True)
        for texto in ("Delete this reading?", "This cannot be undone", "Nov 19, 2026, 10:00", "No, go back"):
            self.assertIn(texto, html)

    def test_paginas_de_error_en_espanol(self):
        self.entrar()
        r = self.web.get("/no-existe")
        self.assertEqual(r.status_code, 404)
        self.assertIn("We could not find that page", r.get_data(as_text=True))
        r = self.web.get("/api/no-existe")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("We could not find that page", r.get_data(as_text=True))   # la API no cambia
        r = self.web.post("/admin/users", data={"csrf": "malo"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("the form expired", r.get_data(as_text=True))

    def test_desactivar_pide_confirmacion(self):
        self.entrar()
        self.store.save_admin("otro@example.com", "Otro", PASSWORD)
        html = self.web.get("/admin/users").get_data(as_text=True)
        self.assertIn('data-confirmar="Disable otro@example.com?', html)
        self.assertIn("This is you", html)

    def test_sin_estilos_ni_scripts_en_linea(self):
        """La CSP del portal bloquea style="" y <script> en línea: todo va en static/."""
        for archivo in PLANTILLAS.glob("*.html"):
            texto = archivo.read_text(encoding="utf-8")
            self.assertNotIn('style="', texto, archivo.name)
            self.assertNotIn("onclick", texto.lower(), archivo.name)
            self.assertNotIn("onsubmit", texto.lower(), archivo.name)


class InicioSinBasesTests(DisenoTests):
    def setUp(self):
        super().setUp(analytics=AnalyticsCaida())

    def test_al_entrar_llega_al_inicio(self):
        self.entrar()
        html = self.web.get("/admin").get_data(as_text=True)
        self.assertIn("The system status could not be read", html)


class ExplicarTests(unittest.TestCase):
    def test_mensajes(self):
        casos = {
            "Record version conflict. Reload the record.": "Someone else saved",
            "A user with that email already exists.": "An administrator with that email already exists",
            "Dew point exceeds temperature (zero-tolerance rule pending approval).": "dew point",
            "Only LOC-CDMX (Mexico City) is accepted.": "Mexico City",
            "Cannot connect to MySQL.": "There is no connection",
            "humidity_pct must be between 0 and 100": "“Humidity”",
            "Password must contain 12–128 characters.": "12 to 128",
            "A positive integer expected_version is required.": "That hour already exists",
        }
        for tecnico, esperado in casos.items():
            claro, detalle = explicar(tecnico)
            self.assertIn(esperado, claro, tecnico)
            self.assertEqual(detalle, tecnico)
        self.assertEqual(explicar("Mensaje ya claro."), ("Mensaje ya claro.", None))

    def test_hora_local(self):
        self.assertEqual(hora_local("2026-11-19T06:00:00Z"), "Nov 19, 2026, 00:00")
        self.assertEqual(hora_local("2026-11-18 20:00:00.123"), "Nov 18, 2026, 14:00")
        self.assertEqual(hora_local("no es fecha"), "no es fecha")
        self.assertEqual(hora_local(None), "—")


class InglesTests(unittest.TestCase):
    """The forecast labels arrive in Spanish (also from the ClimateDbV2 Lambda) and are shown in English."""

    def test_etiquetas(self):
        from datetime import date
        from services import ingles
        self.assertEqual(ingles.en("Posible lluvia"), "Chance of rain")
        self.assertEqual(ingles.en("Oestesuroeste"), "West-southwest")
        self.assertEqual(ingles.en("Muy alto"), "Very high")
        self.assertEqual(ingles.en("18 nov 2026, 17:00"), "Nov 18, 2026, 17:00")
        self.assertEqual(ingles.en("Already English"), "Already English")
        hoy = date(2026, 11, 23)
        self.assertEqual(ingles.day_name(hoy, hoy), "Today")
        self.assertEqual(ingles.day_name(date(2026, 11, 24), hoy), "Tomorrow")
        self.assertEqual(ingles.day_name("2026-11-25", hoy), "Wednesday")
        self.assertEqual(ingles.short_date(hoy), "Nov 23")
        self.assertEqual(ingles.long_date(hoy), "Monday, November 23")

    def test_plantillas_sin_espanol(self):
        palabras = ("Inicio", "Lecturas", "Guardar", "Borrar", "Regresar", "Contraseña", "Auditoría", "¿", "Fecha y hora")
        for archivo in PLANTILLAS.glob("*.html"):
            texto = archivo.read_text(encoding="utf-8")
            for palabra in palabras:
                self.assertNotIn(palabra, texto, f"{archivo.name}: {palabra}")


if __name__ == "__main__":
    unittest.main()
