"""JWT, MySQL users/sessions/audit + stored procedures (SQLite stand-in), REST API, Idempotency-Key,
public forecast page and the prediction model."""
import re
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app import create_app
from contract import dumps, load_json
from prediccion import demo
from prediccion import modelo as M
from services.analytics import DemoAnalytics, PostgresAnalytics
from services.catalog import classify_condition
from services.endpoints import DemoClient, sample
from services.idempotency import DynamoIdempotency, MemoryIdempotency
from services.jwt_tokens import TokenError, TokenSigner, b64encode, compact
from services.mysql_store import MySQLAccessStore, utcnow

sqlite3.register_adapter(datetime, lambda d: d.strftime("%Y-%m-%d %H:%M:%S.%f"))
SCHEMA = """
CREATE TABLE roles (role_id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL);
INSERT INTO roles VALUES (1,'ADMIN','Administrador'),(2,'DATA_ENTRY','Captura'),(3,'ANALYST','Analista');
CREATE TABLE users (user_id TEXT PRIMARY KEY, role_id INTEGER NOT NULL REFERENCES roles(role_id),
  normalized_email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1, auth_version INTEGER NOT NULL DEFAULT 1 CHECK (auth_version > 0),
  created_at_utc TEXT NOT NULL);
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(user_id),
  issued_auth_version INTEGER NOT NULL, created_at_utc TEXT NOT NULL, last_activity_at_utc TEXT NOT NULL,
  expires_at_utc TEXT NOT NULL, revoked_at_utc TEXT, refresh_token_hash TEXT, CHECK (expires_at_utc > created_at_utc));
CREATE TABLE audit_events (event_id TEXT PRIMARY KEY, actor_id TEXT REFERENCES users(user_id), action TEXT NOT NULL,
  entity_type TEXT NOT NULL, entity_key TEXT NOT NULL, occurred_at_utc TEXT NOT NULL, request_id TEXT NOT NULL,
  transaction_id TEXT NOT NULL, before_data TEXT, after_data TEXT, outcome TEXT NOT NULL, reason TEXT);
CREATE TABLE audit_events_history (event_id TEXT PRIMARY KEY, actor_id TEXT, action TEXT NOT NULL,
  entity_type TEXT NOT NULL, entity_key TEXT NOT NULL, occurred_at_utc TEXT NOT NULL, request_id TEXT NOT NULL,
  transaction_id TEXT NOT NULL, before_data TEXT, after_data TEXT, outcome TEXT NOT NULL, reason TEXT,
  archived_at_utc TEXT NOT NULL);
CREATE TABLE locations (location_id TEXT PRIMARY KEY, name TEXT NOT NULL, city TEXT NOT NULL, state TEXT NOT NULL,
  country TEXT NOT NULL, latitude TEXT NOT NULL, longitude TEXT NOT NULL, altitude_m TEXT NOT NULL,
  timezone_name TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
  created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL);
CREATE TABLE stations (station_id TEXT PRIMARY KEY, location_id TEXT UNIQUE NOT NULL REFERENCES locations(location_id),
  code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, station_type TEXT NOT NULL, expected_interval_minutes INTEGER NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1, created_at_utc TEXT NOT NULL);
CREATE TABLE data_sources (source_id TEXT PRIMARY KEY, name TEXT NOT NULL, source_type TEXT NOT NULL,
  priority INTEGER NOT NULL, configuration TEXT NOT NULL DEFAULT '{}', secret_reference TEXT, is_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE weather_conditions (condition_id TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  description TEXT NOT NULL, rule_version TEXT NOT NULL, rule_definition TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1);
INSERT INTO locations VALUES ('LOC-CDMX','Ciudad de México - Centro','Ciudad de México','CDMX','México','19.4326','-99.1332','2240','America/Mexico_City',1,1,'2026-11-01','2026-11-01');
INSERT INTO stations VALUES ('ST-CDMX-OM','LOC-CDMX','OM-CDMX','Estación virtual CDMX','VIRTUAL',60,1,'2026-11-01');
INSERT INTO data_sources VALUES ('SRC-MANUAL','Captura manual','MANUAL',2,'{}',NULL,1),('SRC-OPENMETEO','Open-Meteo','API',1,'{}',NULL,1);
INSERT INTO weather_conditions VALUES
 ('COND-CLEAR','CLEAR','Despejado','d','r','{}',1),('COND-PARTLY-CLOUDY','PARTLY_CLOUDY','Parcial','d','r','{}',1),
 ('COND-OVERCAST','OVERCAST','Nublado','d','r','{}',1),('COND-FOG','FOG','Niebla','d','r','{}',1),
 ('COND-DRIZZLE','DRIZZLE','Llovizna','d','r','{}',1),('COND-RAIN','RAIN','Lluvia','d','r','{}',1),
 ('COND-SNOW','SNOW','Nieve','d','r','{}',1),('COND-STORM','STORM','Tormenta','d','r','{}',1);
"""
PASSWORD = "correct-password-123"
UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


class Cursor:
    """pymysql-like cursor over SQLite: %s parameters and dictionary rows."""
    def __init__(self, cur): self.cur, self.rowcount = cur, -1
    def execute(self, sql, params=()):
        self.cur.execute(sql.replace("%s", "?"), tuple(params))
        self.rowcount = self.cur.rowcount
    def fetchone(self): return self.cur.fetchone()
    def fetchall(self): return self.cur.fetchall()
    def close(self): self.cur.close()


class Connection:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = lambda c, r: {d[0]: v for d, v in zip(c.description, r)}
    def cursor(self): return Cursor(self.db.cursor())
    def commit(self): self.db.commit()
    def rollback(self): self.db.rollback()
    def close(self): self.db.close()


class StoreDouble(MySQLAccessStore):
    """Runs the same Python code; the two MySQL procedures are emulated with the same rules
    (sql/mysql_procedimientos.sql) because SQLite has no stored procedures."""
    calls = None

    def call_procedure(self, cur, name, args, outputs):
        self.calls = (self.calls or []) + [(name, list(args))]
        now = utcnow()
        if name == "sp_guardar_admin":
            email, nombre, hashed, nuevo, actor, evento, transaccion, solicitud = args
            email = email.strip().lower()
            if not re.fullmatch(r"[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,180}[.][a-z]{2,24}", email):
                raise ValueError("Correo no válido")
            if not hashed.startswith(("scrypt:", "pbkdf2:")) or len(hashed) < 40:
                raise ValueError("Se esperaba el hash de la contraseña, no la contraseña")
            if not all(UUID4.fullmatch(x) for x in (nuevo, evento, transaccion)):
                raise ValueError("Los identificadores deben ser UUID versión 4")
            cur.execute("SELECT u.user_id, r.code FROM users u JOIN roles r ON r.role_id = u.role_id"
                        " WHERE u.normalized_email = %s", (email,))
            fila = cur.fetchone()
            revocadas = 0
            if not fila:
                cur.execute("INSERT INTO users VALUES (%s, 1, %s, %s, %s, 1, 1, %s)",
                            (nuevo, email, hashed, nombre or email.split("@")[0], now))
                usuario, accion = nuevo, "CREADO"
            else:
                if fila["code"] != "ADMIN":
                    raise ValueError("Ese correo pertenece a un usuario que no es administrador")
                usuario, accion = fila["user_id"], "CONTRASENA"
                cur.execute("UPDATE users SET password_hash = %s, display_name = COALESCE(%s, display_name),"
                            " auth_version = auth_version + 1 WHERE user_id = %s", (hashed, nombre, usuario))
                cur.execute("UPDATE sessions SET revoked_at_utc = %s WHERE user_id = %s AND revoked_at_utc IS NULL",
                            (now, usuario))
                revocadas = cur.rowcount
            cur.execute("INSERT INTO audit_events VALUES (%s, %s, %s, 'USER', %s, %s, %s, %s, NULL, %s, 'SUCCESS', NULL)",
                        (evento, actor, "ADMIN_CREATE" if accion == "CREADO" else "PASSWORD_CHANGE", email, now,
                         solicitud, transaccion, dumps({"sesiones_revocadas": revocadas})))
            return [usuario, accion]
        if name == "sp_rotar_auditoria":
            dias, actor, evento, transaccion, solicitud = args
            if not 0 <= dias <= 3650:
                raise ValueError("Los días deben estar entre 0 y 3650")
            corte = now - timedelta(days=dias)
            cur.execute("INSERT INTO audit_events_history SELECT *, %s FROM audit_events WHERE occurred_at_utc < %s",
                        (now, corte))
            movidos = cur.rowcount
            cur.execute("DELETE FROM audit_events WHERE occurred_at_utc < %s AND event_id IN"
                        " (SELECT event_id FROM audit_events_history)", (corte,))
            cur.execute("DELETE FROM sessions WHERE revoked_at_utc IS NOT NULL OR expires_at_utc < %s"
                        " OR last_activity_at_utc < %s", (now, now - timedelta(minutes=30)))
            sesiones = cur.rowcount
            cur.execute("INSERT INTO audit_events VALUES (%s, %s, 'AUDIT_ROTATE', 'AUDIT', %s, %s, %s, %s, NULL, %s,"
                        " 'SUCCESS', NULL)", (evento, actor, f"dias={dias}", now, solicitud, transaccion,
                                               dumps({"movidos": movidos, "sesiones": sesiones})))
            return [movidos, sesiones]
        raise AssertionError(name)


def mysql_double(path, test=None):
    db = sqlite3.connect(path)
    with db:
        db.executescript(SCHEMA)
    db.close()
    store = StoreDouble(lambda: Connection(path))
    if test is not None:
        test.addCleanup(store.db.close)   # runs before the temporary folder is removed
    return store


def reading(**changes):
    row = {**sample(), "condition_id": "AUTO", "timestamp_utc": "2026-11-19T18:00:00Z"}
    row.update(changes)
    return row


class RecordingAnalytics(DemoAnalytics):
    """Demo history + records what would be sent to sp_upsert_lecturas (as PostgreSQL would answer)."""
    def __init__(self):
        super().__init__(demo.historia(dias=120, hasta=datetime(2026, 11, 20, 12, tzinfo=timezone.utc)))
        self.llamadas = []

    def upsert(self, records, origen, fuentes, condiciones):
        self.llamadas.append((origen, [r["timestamp_utc"] for r in records], list(fuentes), list(condiciones)))
        return dict(batch_id="b", insertados=len(records), actualizados=0, omitidos=0, rechazados=0)


class JwtTests(unittest.TestCase):
    def test_roundtrip_tamper_algorithm_and_expiry(self):
        signer = TokenSigner(b"k" * 32)
        token = signer.issue({"sub": "u1", "sid": "s1"})
        self.assertEqual(signer.verify(token)["sub"], "u1")
        head, body, sig = token.split(".")
        forged = b64encode(compact({**signer.verify(token), "role": "ADMIN"}))
        for bad in (head + "." + forged + "." + sig, b64encode(compact({"alg": "none", "typ": "JWT"})) + "." + body + ".",
                    token[:-4] + ("AAAA" if token[-4:-1] != "AAA" else "BBBB"), "not.a.token", ""):
            with self.assertRaises(TokenError): signer.verify(bad)
        with self.assertRaises(TokenError): TokenSigner(b"x" * 32).verify(token)
        expired = TokenSigner(b"k" * 32, ttl=-1).issue({"sub": "u1"})
        with self.assertRaises(TokenError): signer.verify(expired)
        self.assertEqual(signer.verify(expired, check_expiry=False)["sub"], "u1")

    def test_condition_rule_matches_postgres_function(self):
        cases = [((1, 10, 10, 50, -1), "COND-SNOW"), ((0.5, 10, 10, 50, 20), "COND-RAIN"),
                 ((0.1, 10, 10, 50, 20), "COND-DRIZZLE"), ((0, 10, 0.5, 96, 20), "COND-FOG"),
                 ((0, 90, 10, 50, 20), "COND-OVERCAST"), ((0, 30, 10, 50, 20), "COND-PARTLY-CLOUDY"),
                 ((0, 10, 10, 50, 20), "COND-CLEAR")]
        for args, expected in cases:
            self.assertEqual(classify_condition(*args), expected)


class MySQLStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.path = self.temp.name + "/mysql.db"
        self.store = mysql_double(self.path, self)
        self.assertEqual(self.store.save_admin("Admin@Example.com", "Admin", PASSWORD), "CREADO")

    def sql(self, statement, params=()):
        db = sqlite3.connect(self.path)
        try:
            with db:
                return db.execute(statement, params).fetchall()
        finally:
            db.close()

    def test_login_creates_session_and_audit(self):
        token = self.store.login("admin@example.com ", PASSWORD)
        user = self.store.user(token)
        self.assertEqual((user["username"], user["role"]), ("admin@example.com", "ADMIN"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM sessions WHERE revoked_at_utc IS NULL")[0][0], 1)
        self.assertEqual(self.sql("SELECT action, outcome FROM audit_events ORDER BY occurred_at_utc"),
                         [("ADMIN_CREATE", "SUCCESS"), ("LOGIN", "SUCCESS")])
        self.assertNotIn(PASSWORD, str(self.sql("SELECT * FROM users")))
        name, args = self.store.calls[0]
        self.assertEqual(name, "sp_guardar_admin")
        self.assertTrue(args[2].startswith("scrypt:") and PASSWORD not in args)   # only the hash reaches MySQL

    def test_only_administrators_sign_in(self):
        self.sql("INSERT INTO users VALUES ('7f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f', 2, 'clerk@example.com', ?, 'Clerk',"
                 " 1, 1, '2026-11-01')", (self.sql("SELECT password_hash FROM users")[0][0],))
        self.assertIsNone(self.store.login("clerk@example.com", PASSWORD))
        self.assertIn("only administrators", self.sql("SELECT reason FROM audit_events WHERE entity_key='clerk@example.com'")[0][0])
        with self.assertRaises(ValueError): self.store.save_admin("clerk@example.com", None, PASSWORD)

    def test_lockout_after_five_failures(self):
        for _ in range(5): self.assertIsNone(self.store.login("admin@example.com", "wrong-password"))
        self.assertIsNone(self.store.login("admin@example.com", PASSWORD))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM audit_events WHERE outcome='BLOCKED'")[0][0], 1)
        old = (datetime.utcnow() - timedelta(minutes=16)).strftime("%Y-%m-%d %H:%M:%S.%f")
        self.sql("UPDATE audit_events SET occurred_at_utc=? WHERE outcome='FAILURE'", (old,))
        self.assertTrue(self.store.login("admin@example.com", PASSWORD))

    def test_password_change_revokes_every_token(self):
        token = self.store.login("admin@example.com", PASSWORD)
        old = (datetime.utcnow() - timedelta(minutes=31)).strftime("%Y-%m-%d %H:%M:%S.%f")
        self.sql("UPDATE sessions SET last_activity_at_utc=?", (old,))
        self.assertIsNone(self.store.user(token))                         # 30 minutes idle
        token = self.store.login("admin@example.com", PASSWORD)
        admin = self.store.user(token)
        with self.assertRaises(ValueError): self.store.change_own_password(admin, "wrong-password", "new-password-456")
        self.assertEqual(self.store.change_own_password(admin, PASSWORD, "new-password-456"), "CONTRASENA")
        self.assertIsNone(self.store.user(token))                         # auth_version changed
        self.assertIsNone(self.store.login("admin@example.com", PASSWORD))
        self.assertTrue(self.store.user(self.store.login("admin@example.com", "new-password-456")))
        self.assertEqual(self.sql("SELECT auth_version FROM users")[0][0], 2)

    def test_disable_and_logout(self):
        self.store.save_admin("second@example.com", "Second", PASSWORD)
        token = self.store.login("second@example.com", PASSWORD)
        self.store.disable("second@example.com")
        self.assertIsNone(self.store.user(token))
        self.assertIsNone(self.store.login("second@example.com", PASSWORD))
        self.store.enable("second@example.com")
        token = self.store.login("second@example.com", PASSWORD)
        self.store.logout(token)
        self.assertIsNone(self.store.user(token))
        self.assertEqual([u["username"] for u in self.store.list_users()], ["admin@example.com", "second@example.com"])

    def test_validation(self):
        for email, password in (("bad", PASSWORD), ("a@b.co", "short")):
            with self.assertRaises(ValueError): self.store.save_admin(email, None, password)
        with self.assertRaises(ValueError): self.store.add_user("admin@example.com", PASSWORD)
        with self.assertRaises(ValueError): self.store.add_user("x@example.com", PASSWORD, "ANALYST")

    def test_rotate_audit(self):
        token = self.store.login("admin@example.com", PASSWORD)
        self.store.logout(token)                                           # revoked session to clean
        viejo = (datetime.utcnow() - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S.%f")
        self.sql("UPDATE audit_events SET occurred_at_utc=? WHERE action IN ('ADMIN_CREATE', 'LOGIN')", (viejo,))
        movidos, sesiones = self.store.rotate_audit(30)
        self.assertEqual((movidos, sesiones), (2, 1))
        self.assertEqual(self.store.history_count(), 2)
        acciones = [a for (a,) in self.sql("SELECT action FROM audit_events")]
        self.assertEqual(sorted(acciones), ["AUDIT_ROTATE", "LOGOUT"])
        with self.assertRaises(ValueError): self.store.rotate_audit(-1)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.path = self.temp.name + "/mysql.db"
        self.store = mysql_double(self.path, self)
        for email in ("admin@example.com", "second@example.com"):
            self.store.save_admin(email, None, PASSWORD)
        self.backend = DemoClient(seed=False)
        self.analytics = RecordingAnalytics()
        self.app = create_app(self.backend, "lambda", auth_path=self.temp.name + "/local.sqlite3",
                              auth_store=self.store, idempotency=MemoryIdempotency(), analytics=self.analytics)
        self.app.testing = True
        self.web = self.app.test_client()

    def token(self, email="admin@example.com"):
        response = self.web.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        return response.get_json()["access_token"]

    def call(self, method, path, token=None, body=None, key=None, version=None):
        headers = {}
        if token: headers["Authorization"] = "Bearer " + token
        if key: headers["Idempotency-Key"] = key
        if version: headers["If-Match"] = f'"{version}"'
        data = dumps(body) if body is not None else None
        response = self.web.open(path, method=method, headers=headers, data=data, content_type="application/json")
        return response, load_json(response.get_data(as_text=True))

    def test_public_and_protected_routes(self):
        self.assertEqual(self.web.get("/api/health").status_code, 200)
        response, body = self.call("GET", "/api/pronostico")
        self.assertEqual((response.status_code, len(body["dias"]), body["ubicacion"]), (200, 7, "LOC-CDMX"))
        response, body = self.call("GET", "/api/readings")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response.headers["WWW-Authenticate"])
        self.assertEqual(self.call("GET", "/api/readings", "x.y.z")[0].status_code, 401)
        self.assertEqual(self.web.post("/api/auth/login", json={"email": "admin@example.com", "password": "nope"}).status_code, 401)
        self.assertEqual(self.call("GET", "/api/readings", self.token())[0].status_code, 200)

    def test_create_with_idempotency_key_replay_and_postgres_sync(self):
        token = self.token()
        missing, _ = self.call("POST", "/api/readings", token, reading())
        self.assertEqual(missing.status_code, 400)
        first, body = self.call("POST", "/api/readings", token, reading(), key="key-00000001")
        self.assertEqual((first.status_code, first.headers["Idempotent-Replayed"]), (201, "false"))
        self.assertEqual(body["record"]["condition_id"], "COND-CLEAR")   # AUTO calculated (15.5 % clouds)
        self.assertEqual(body["postgres"]["insertados"], 1)                        # sp_upsert_lecturas
        again, body2 = self.call("POST", "/api/readings", token, reading(), key="key-00000001")
        self.assertEqual((again.status_code, again.headers["Idempotent-Replayed"]), (201, "true"))
        self.assertEqual(body2["record"]["record_id"], body["record"]["record_id"])
        self.assertEqual((len(self.backend.table.rows), len(self.analytics.llamadas)), (1, 1))
        changed, problem = self.call("POST", "/api/readings", token, reading(uv_index=9), key="key-00000001")
        self.assertEqual((changed.status_code, problem["error"]), (422, "idempotency_key_reused"))
        duplicate, _ = self.call("POST", "/api/readings", token, reading(), key="key-00000002")
        self.assertEqual(duplicate.status_code, 409)

    def test_only_mexico_city_and_catalog(self):
        token = self.token()
        for n, change in enumerate(({"location_id": "LOC-GDL"}, {"station_id": "ST-OTHER"}, {"source_id": "SRC-FAKE"},
                                    {"condition_id": "COND-01"}, {"humidity_pct": 101})):
            response, body = self.call("POST", "/api/readings", token, reading(**change), key=f"key-reject-{n:03d}")
            self.assertEqual(response.status_code, 400, change)
        response, body = self.call("POST", "/api/readings", token, reading(station_id="", location_id=""), key="key-defaults")
        self.assertEqual((response.status_code, body["record"]["location_id"], body["record"]["station_id"]),
                         (201, "LOC-CDMX", "ST-CDMX-OM"))

    def test_update_requires_version_and_detects_conflicts(self):
        token = self.token()
        self.call("POST", "/api/readings", token, reading(), key="key-create-01")
        path = "/api/readings/LOC-CDMX/2026-11-19T18:00:00Z"
        record, body = self.call("GET", path, token)
        self.assertEqual(record.headers["ETag"], '"1"')
        self.assertEqual(self.call("PUT", path, token, {"uv_index": 5}, key="key-update-00")[0].status_code, 428)
        ok, body = self.call("PUT", path, token, {"uv_index": 5}, key="key-update-01", version=1)
        self.assertEqual((ok.status_code, body["record"]["version"]), (200, 2))
        stale, body = self.call("PUT", path, token, {"uv_index": 6}, key="key-update-02", version=1)
        self.assertEqual(stale.status_code, 409)
        replay, body = self.call("PUT", path, token, {"uv_index": 5}, key="key-update-01", version=1)
        self.assertEqual((replay.status_code, replay.headers["Idempotent-Replayed"]), (200, "true"))
        self.assertEqual([o for o, *_ in self.analytics.llamadas], ["api", "api"])

    def test_delete_logout_and_revocation(self):
        admin, second = self.token(), self.token("second@example.com")
        self.call("POST", "/api/readings", admin, reading(), key="key-create-02")
        path = "/api/readings/LOC-CDMX/2026-11-19T18:00:00Z"
        self.assertEqual(self.call("DELETE", path, admin)[0].status_code, 428)
        self.assertEqual(self.call("DELETE", path, admin, version=1)[0].status_code, 200)
        self.assertEqual(len(self.backend.table.rows), 0)
        self.call("POST", "/api/auth/logout", admin)
        self.assertEqual(self.call("GET", "/api/me", admin)[0].status_code, 401)
        self.store.disable("second@example.com")
        self.assertEqual(self.call("GET", "/api/me", second)[0].status_code, 401)
        db = sqlite3.connect(self.path)
        actions = {r[0] for r in db.execute("SELECT action FROM audit_events")}
        db.close()
        self.assertTrue({"LOGIN", "READING_CREATE", "READING_DELETE", "LOGOUT", "USER_DISABLE", "ADMIN_CREATE"} <= actions)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True); self.addCleanup(self.temp.cleanup)
        self.store = mysql_double(self.temp.name + "/mysql.db", self)
        self.store.save_admin("admin@example.com", "Admin", PASSWORD)
        self.backend = DemoClient(seed=False)
        self.analytics = RecordingAnalytics()
        self.app = create_app(self.backend, "lambda", auth_path=self.temp.name + "/local.sqlite3",
                              auth_store=self.store, analytics=self.analytics)
        self.app.testing = True
        self.web = self.app.test_client()

    def csrf(self):
        with self.web.session_transaction() as s: return s["csrf"]

    def login(self, email="admin@example.com", password=PASSWORD):
        self.web.get("/login")
        return self.web.post("/login", data={"csrf": self.csrf(), "username": email, "password": password})

    def test_public_page_without_sign_in(self):
        html = self.web.get("/").get_data(as_text=True)
        for text in ("7-day forecast", "Hourly", "Today", "Tomorrow", "Feels like", "statistical forecast",
                     "Admin sign-in"):
            self.assertIn(text, html)
        self.assertEqual(len(re.findall(r'class="dia( elegido)?"', html)), 7)
        self.assertEqual(self.web.get("/?dia=2026-11-23").status_code, 200)
        self.assertEqual(self.web.get("/records").status_code, 302)

    def test_form_fixed_city_auto_condition_and_sync(self):
        self.assertEqual(self.login().status_code, 302)
        html = self.web.get("/add").get_data(as_text=True)
        self.assertIn('value="LOC-CDMX" readonly', html)
        self.assertNotIn("Locations", html)
        form = {**reading(precipitation_mm="2"), "csrf": self.csrf()}
        response = self.web.post("/add", data=form, follow_redirects=True)
        self.assertIn("sp_upsert_lecturas: 1 new", response.get_data(as_text=True))
        saved = next(iter(self.backend.table.rows.values()))
        self.assertEqual((saved["station_id"], saved["condition_id"]), ("ST-CDMX-OM", "COND-RAIN"))
        html = self.web.post("/add", data={**form, "location_id": "LOC-GDL"}).get_data(as_text=True)
        self.assertIn("Only LOC-CDMX", html)
        self.assertEqual(self.analytics.llamadas[0][0], "alta")

    def test_administrators_password_and_audit_rotation(self):
        self.login()
        for page in ("/admin/users", "/admin/audit", "/account/password", "/records", "/import"):
            self.assertEqual(self.web.get(page).status_code, 200)
        self.assertEqual(self.web.get("/admin/locations").status_code, 404)
        response = self.web.post("/admin/users", data={"csrf": self.csrf(), "username": "new@example.com",
                                                       "display_name": "New", "password": PASSWORD, "confirm": PASSWORD},
                                 follow_redirects=True)
        self.assertIn("Administrator created", response.get_data(as_text=True))
        response = self.web.post("/admin/users", data={"csrf": self.csrf(), "username": "new@example.com",
                                                       "display_name": "", "password": "other-password-789",
                                                       "confirm": "other-password-789"}, follow_redirects=True)
        self.assertIn("Password changed", response.get_data(as_text=True))
        response = self.web.post("/admin/audit/rotate", data={"csrf": self.csrf(), "days": "0"}, follow_redirects=True)
        self.assertIn("sp_rotar_auditoria", response.get_data(as_text=True))
        self.assertIn("Audit log archived", self.web.get("/admin/audit").get_data(as_text=True))
        response = self.web.post("/account/password", data={"csrf": self.csrf(), "current": PASSWORD,
                                                            "password": "brand-new-pass-1", "confirm": "brand-new-pass-1"})
        self.assertEqual(response.headers["Location"], "/login")
        self.assertEqual(self.web.get("/records").status_code, 302)        # session closed
        self.assertEqual(self.login(password="brand-new-pass-1").status_code, 302)


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.horas = demo.historia(hasta=datetime(2026, 11, 20, 12, tzinfo=timezone.utc))
        cls.modelo = M.Modelo(cls.horas)

    def test_forecast_is_coherent(self):
        dias = self.modelo.pronostico()
        self.assertEqual([d["horizonte"] for d in dias], list(range(1, 8)))
        for d in dias:
            self.assertLessEqual(d["temperature_min_c"], d["temperature_mean_c"])
            self.assertLessEqual(d["temperature_mean_c"], d["temperature_max_c"])
            self.assertEqual(len(d["horas"]), 24)
            for h in d["horas"]:
                self.assertTrue(d["temperature_min_c"] - 0.01 <= h["temperature_c"] <= d["temperature_max_c"] + 0.01)
                self.assertLessEqual(h["rain_probability_pct"], d["rain_probability_pct"] + 0.01)
            self.assertAlmostEqual(sum(h["precipitation_mm"] for h in d["horas"]), d["precipitation_total_mm"], delta=0.05)

    def test_beats_persistence_at_one_week(self):
        ultimo = self.modelo.ultimo_dia()
        pares = M.evaluar(self.modelo, [ultimo - timedelta(days=i) for i in range(8, 60)])
        nuevo = M.errores(pares["CLIMATOLOGY_ANOMALY"]["temperature_mean_c"][7])[0]
        persistencia = M.errores(pares["PERSISTENCE"]["temperature_mean_c"][7])[0]
        self.assertLess(nuevo, persistencia)

    def test_no_future_information(self):
        emision = self.modelo.ultimo_dia() - timedelta(days=20)
        antes = self.modelo.pronostico_dia(emision, emision + timedelta(days=3))
        recortado = M.Modelo([h for h in self.horas if M.a_local(h["ts_utc"]).date() <= emision], phi=self.modelo.phi)
        self.assertEqual(antes, recortado.pronostico_dia(emision, emision + timedelta(days=3)))

    def test_public_page_starts_at_current_hour(self):
        analytics = DemoAnalytics(self.horas)
        ahora = datetime(2026, 11, 20, 14, 30)
        datos = analytics.pagina(ahora_local=ahora)
        self.assertEqual(datos["dias"][0]["nombre"], "Hoy")
        self.assertEqual(datos["horas"][0]["hora"], "14:00")
        otro = analytics.pagina(date(2026, 11, 22), ahora_local=ahora)
        self.assertEqual((len(otro["horas"]), otro["titulo_dia"]), (24, "domingo 22 de noviembre"))


class FakePg:
    """Records SQL sent by PostgresAnalytics (pg8000 cursors return tuples)."""
    def __init__(self, rows): self.rows, self.sent = rows, []
    def cursor(self):
        outer = self
        class C:
            def execute(self, sql, params=()): outer.sent.append((sql, params))
            def fetchall(self): return outer.rows
            def fetchone(self): return ("b-1", 1, 0, 0, 0)
            def close(self): pass
        return C()
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


class PostgresAnalyticsTests(unittest.TestCase):
    def test_reads_history_and_calls_the_procedure(self):
        horas = demo.historia(dias=60, hasta=datetime(2026, 11, 20, 12, tzinfo=timezone.utc))
        conexion = FakePg([(h["ts_utc"], Decimal(str(h["temperature_c"])), Decimal(str(h["humidity_pct"])),
                            Decimal(str(h["precipitation_mm"]))) for h in horas])
        analytics = PostgresAnalytics(lambda: conexion)
        self.assertEqual(len(analytics.historial()), 60 * 24)
        resultado = analytics.upsert([{"location_id": "LOC-CDMX", "uv_index": Decimal("3.2")}], "alta",
                                     ["SRC-MANUAL"], ["COND-CLEAR"])
        self.assertEqual(resultado["insertados"], 1)
        sql, params = conexion.sent[-1]
        self.assertIn("CALL climate_analytics.sp_upsert_lecturas", sql)
        self.assertEqual((params[1], params[2], params[3]), ("alta", ["SRC-MANUAL"], ["COND-CLEAR"]))
        self.assertEqual(load_json(params[0])[0]["uv_index"], Decimal("3.2"))


class FakeTable:
    """Just enough of a boto3 DynamoDB Table to test conditional writes."""
    class Error(Exception):
        response = {"Error": {"Code": "ConditionalCheckFailedException"}}

    def __init__(self, keys, types):
        self.items, self.keys, self.types = {}, keys, types
        client = type("Client", (), {})()
        client.describe_table = lambda TableName: {"Table": {
            "KeySchema": [{"AttributeName": n, "KeyType": t} for t, n in keys.items()],
            "AttributeDefinitions": [{"AttributeName": n, "AttributeType": types[n]} for n in keys.values()]}}
        client.describe_time_to_live = lambda TableName: {"TimeToLiveDescription": {"TimeToLiveStatus": "ENABLED", "AttributeName": "ttl"}}
        self.meta = type("Meta", (), {"client": client})()

    def k(self, item): return tuple(item[n] for n in self.keys.values())

    def put_item(self, Item, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        old = self.items.get(self.k(Item))
        if old and not old.get("ttl", 0) < ExpressionAttributeValues[":now"]:
            raise self.Error()
        self.items[self.k(Item)] = dict(Item)

    def get_item(self, Key, ConsistentRead):
        item = self.items.get(self.k(Key))
        return {"Item": dict(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        item = self.items[self.k(Key)]
        item.update(status=ExpressionAttributeValues[":s"], response_status=Decimal(ExpressionAttributeValues[":rs"]),
                    response_body=ExpressionAttributeValues[":rb"])

    def delete_item(self, Key): self.items.pop(self.k(Key), None)


class DynamoIdempotencyTests(unittest.TestCase):
    def test_discovers_keys_and_replays(self):
        for keys, types in (({"HASH": "idempotency_key"}, {"idempotency_key": "S"}),
                            ({"HASH": "pk", "RANGE": "sk"}, {"pk": "S", "sk": "S"})):
            table = FakeTable(keys, types)
            resource = type("Resource", (), {"Table": lambda self, name: table})()
            store = DynamoIdempotency("ClimateIdempotency", resource=resource)
            self.assertIn("TTL ttl", store.description)
            self.assertIsNone(store.begin("u#key-1", "print-a"))
            with self.assertRaises(Exception) as busy: store.begin("u#key-1", "print-a")
            self.assertEqual(busy.exception.status, 409)
            store.finish("u#key-1", 201, {"record": {"uv_index": Decimal("3.2")}})
            self.assertEqual(store.begin("u#key-1", "print-a"), (201, {"record": {"uv_index": Decimal("3.2")}}))
            with self.assertRaises(Exception) as reused: store.begin("u#key-1", "print-b")
            self.assertEqual(reused.exception.status, 422)
            store.abandon("u#key-1")
            self.assertIsNone(store.begin("u#key-1", "print-b"))


if __name__ == "__main__":
    unittest.main()
