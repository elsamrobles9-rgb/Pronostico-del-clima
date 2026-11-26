"""Users, sessions, audit events and catalogs in RDS MySQL (climate_transactional).

Tables used (they already exist; nothing is created or dropped here):
  roles, users, sessions, audit_events, locations, stations, data_sources, weather_conditions.
The SUPERADMIN role is added once by etl/migrar_superadmin.py (safe migration, keeps every hash).

Account rules (the same as services/access.py; sp_guardar_admin enforces them again in MySQL):
  - only ADMIN and SUPERADMIN sign in;
  - only a SUPERADMIN creates accounts, changes other passwords or enables/disables ADMIN accounts;
  - nobody can disable, demote or change the password of a SUPERADMIN from the panel;
  - there is always at least one active SUPERADMIN; roles change only from the command line.

All SQL uses %s parameters (never string formatting) and UTC datetimes calculated in Python.
"""
import hashlib
import queue
import re
import secrets
import ssl
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from contract import dumps
from services import roles
from services.catalog import build_catalog
from services.jwt_tokens import TokenError, TokenSigner

IDLE_LIMIT = timedelta(minutes=30)
LOCK_WINDOW = timedelta(minutes=15)
MAX_FAILURES = 5
TOUCH_EVERY = timedelta(seconds=60)
ROLES = roles.STAFF + roles.HEREDADOS      # codes in the roles table; only roles.STAFF can sign in
EMAIL = re.compile(r"[a-z0-9._%+-]{1,64}@[a-z0-9.-]{1,180}\.[a-z]{2,24}")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
STAFF_SQL = "('" + "','".join(roles.STAFF) + "')"   # fixed constants, not user input


class DatabaseUnavailable(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_datetime(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def normalize_email(value):
    return (value or "").strip().lower()[:254]


def password_matches(stored, password):
    try:
        return check_password_hash(stored, password)
    except (ValueError, TypeError, AttributeError):   # hash written by another tool: treat as wrong password
        return False


def check_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError("Password must contain 12–128 characters.")


class Pool:
    """A few reusable connections (RDS TLS handshakes are slow)."""

    def __init__(self, factory, size=4):
        self.factory, self.size, self.idle = factory, size, queue.LifoQueue()

    def acquire(self):
        try:
            conn = self.idle.get_nowait()
        except queue.Empty:
            return self.factory()
        try:
            ping = getattr(conn, "ping", None)
            if ping:
                ping(reconnect=True)
            else:   # pg8000: a cheap query proves the connection is still alive
                cursor = conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchall()
                cursor.close()
                conn.rollback()
            return conn
        except Exception:
            self.discard(conn)
            return self.factory()

    def close(self):
        """Close the idle connections (tests and shutdown)."""
        while True:
            try:
                self.discard(self.idle.get_nowait())
            except queue.Empty:
                return

    @staticmethod
    def discard(conn):
        try:
            conn.close()
        except Exception:
            pass

    @contextmanager
    def tx(self):
        try:
            conn = self.acquire()
        except Exception as exc:
            raise DatabaseUnavailable("Cannot connect to MySQL. Check that the RDS instance is on "
                                      "and that your IP is allowed in the security group.") from exc
        healthy = False
        try:
            cursor = conn.cursor()
            try:
                yield cursor
            finally:
                cursor.close()
            conn.commit()
            healthy = True
        except Exception:
            try:
                conn.rollback()
                healthy = True
            except Exception:
                pass
            raise
        finally:
            if healthy and self.idle.qsize() < self.size:
                self.idle.put(conn)
            elif not healthy:
                self.discard(conn)


class MySQLAccessStore:
    kind = "mysql"

    def __init__(self, factory, signer=None):
        self.db = Pool(factory)
        self.signer = signer or TokenSigner()
        self.dummy = generate_password_hash(secrets.token_urlsafe(32))
        self.catalog_cache = (0.0, None)

    # ------------------------------------------------------------------ helpers
    def role_id(self, cur, code):
        cur.execute("SELECT role_id FROM roles WHERE code = %s", (code,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Role not found in the roles table: " + code)
        return row["role_id"]

    def find_user(self, cur, email):
        cur.execute("SELECT u.user_id, u.normalized_email, u.display_name, u.password_hash, u.is_active,"
                    " u.auth_version, r.code AS role FROM users u JOIN roles r ON r.role_id = u.role_id"
                    " WHERE u.normalized_email = %s", (email,))
        return cur.fetchone()

    def insert_audit(self, cur, actor_id, action, entity_type, entity_key, outcome,
                     before=None, after=None, reason=None, request_id=None):
        cur.execute(
            "INSERT INTO audit_events (event_id, actor_id, action, entity_type, entity_key, occurred_at_utc,"
            " request_id, transaction_id, before_data, after_data, outcome, reason)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), actor_id, action[:40], entity_type[:60], str(entity_key)[:255], utcnow(),
             (request_id or uuid.uuid4().hex)[:100], str(uuid.uuid4()),
             None if before is None else dumps(before), None if after is None else dumps(after),
             outcome[:30], None if reason is None else str(reason)[:2000]))

    # ------------------------------------------------------------------ authentication
    def login(self, email, password):
        email = normalize_email(email)
        password = password if isinstance(password, str) else ""
        now = utcnow()
        with self.db.tx() as cur:
            cur.execute("SELECT MAX(occurred_at_utc) AS last_ok FROM audit_events WHERE action = 'LOGIN'"
                        " AND entity_type = 'USER' AND entity_key = %s AND outcome = 'SUCCESS'", (email,))
            last_ok = as_datetime((cur.fetchone() or {}).get("last_ok"))
            since = max(now - LOCK_WINDOW, last_ok) if last_ok else now - LOCK_WINDOW
            cur.execute("SELECT COUNT(*) AS failures FROM audit_events WHERE action = 'LOGIN'"
                        " AND entity_type = 'USER' AND entity_key = %s AND outcome = 'FAILURE'"
                        " AND occurred_at_utc > %s", (email, since))
            failures = int(cur.fetchone()["failures"])
            user = self.find_user(cur, email)
            actor = user["user_id"] if user else None
            if failures >= MAX_FAILURES:
                self.insert_audit(cur, actor, "LOGIN", "USER", email, "BLOCKED",
                                  reason="5 failed attempts in 15 minutes")
                return None
            valid = password_matches(user["password_hash"] if user else self.dummy, password[:129])
            if not user or not valid or not int(user["is_active"]) or user["role"] not in roles.STAFF:
                reason = ("inactive user" if not int(user["is_active"]) else "only administrators can sign in") \
                    if user and valid else "invalid credentials"
                self.insert_audit(cur, actor, "LOGIN", "USER", email, "FAILURE", reason=reason)
                return None
            session_id = str(uuid.uuid4())
            version = int(user["auth_version"])
            token = self.signer.issue({"sub": user["user_id"], "sid": session_id, "ver": version,
                                       "role": user["role"], "email": email})
            jti = self.signer.verify(token)["jti"]
            cur.execute("INSERT INTO sessions (session_id, user_id, issued_auth_version, created_at_utc,"
                        " last_activity_at_utc, expires_at_utc, revoked_at_utc, refresh_token_hash)"
                        " VALUES (%s, %s, %s, %s, %s, %s, NULL, %s)",
                        (session_id, user["user_id"], version, now, now,
                         now + timedelta(seconds=self.signer.ttl), hashlib.sha256(jti.encode()).hexdigest()))
            self.insert_audit(cur, actor, "LOGIN", "USER", email, "SUCCESS", after={"session_id": session_id})
            return token

    def user(self, token):
        try:
            claims = self.signer.verify(token)
        except TokenError:
            return None
        now = utcnow()
        with self.db.tx() as cur:
            cur.execute("SELECT s.session_id, s.issued_auth_version, s.last_activity_at_utc, s.expires_at_utc,"
                        " s.revoked_at_utc, u.user_id, u.normalized_email, u.display_name, u.is_active,"
                        " u.auth_version, r.code AS role FROM sessions s JOIN users u ON u.user_id = s.user_id"
                        " JOIN roles r ON r.role_id = u.role_id WHERE s.session_id = %s AND s.user_id = %s",
                        (claims.get("sid"), claims.get("sub")))
            row = cur.fetchone()
            if not row or row["revoked_at_utc"] is not None or not int(row["is_active"]) \
                    or row["role"] not in roles.STAFF:
                return None
            version = int(row["auth_version"])
            if version != int(row["issued_auth_version"]) or version != claims.get("ver"):
                return None
            last = as_datetime(row["last_activity_at_utc"])
            if now >= as_datetime(row["expires_at_utc"]) or now - last >= IDLE_LIMIT:
                cur.execute("UPDATE sessions SET revoked_at_utc = %s WHERE session_id = %s"
                            " AND revoked_at_utc IS NULL", (now, row["session_id"]))
                return None
            if now - last >= TOUCH_EVERY:
                cur.execute("UPDATE sessions SET last_activity_at_utc = %s WHERE session_id = %s",
                            (now, row["session_id"]))
            return {"user_id": row["user_id"], "username": row["normalized_email"],
                    "display_name": row["display_name"], "role": row["role"], "session_id": row["session_id"]}

    def logout(self, token):
        try:
            claims = self.signer.verify(token, check_expiry=False)
        except TokenError:
            return
        with self.db.tx() as cur:
            cur.execute("UPDATE sessions SET revoked_at_utc = %s WHERE session_id = %s AND user_id = %s"
                        " AND revoked_at_utc IS NULL", (utcnow(), claims.get("sid"), claims.get("sub")))
            if cur.rowcount:
                self.insert_audit(cur, claims.get("sub"), "LOGOUT", "USER", claims.get("email", ""), "SUCCESS",
                                  after={"session_id": claims.get("sid")})

    # ------------------------------------------------------------------ user administration
    def call_procedure(self, cur, name, args, outputs):
        """CALL a MySQL stored procedure and read its OUT parameters."""
        salidas = [f"@{name}_{i}" for i in range(outputs)]
        try:
            cur.execute(f"CALL {name}({', '.join(['%s'] * len(args) + salidas)})", args)
        except Exception as exc:
            codigo = exc.args[0] if getattr(exc, "args", None) else None
            if codigo == 1644:   # SIGNAL SQLSTATE '45000' raised by the procedure
                raise ValueError(str(exc.args[1])) from None
            if codigo == 1305:   # procedure does not exist
                raise ValueError(f"{name} is not installed. Run etl/instalar_sp.py.") from None
            raise
        cur.execute("SELECT " + ", ".join(f"{v} AS o{i}" for i, v in enumerate(salidas)))
        fila = cur.fetchone()
        return [fila[f"o{i}"] for i in range(outputs)]

    @staticmethod
    def check_manager(actor, target):
        """Rules for the web and the API (actor present). The command line (actor None) is trusted:
        it already holds the database password."""
        if actor is None:
            return
        own = target is not None and target["user_id"] == actor.get("user_id")
        if not own and not roles.es_super(actor):
            raise ValueError("Only the super administrator can manage administrator accounts.")
        if target is not None and target["role"] == roles.SUPERADMIN and not own:
            raise ValueError("A super administrator's password can only be changed by that same account.")

    def other_active_supers(self, cur, user_id):
        cur.execute("SELECT COUNT(*) AS n FROM users u JOIN roles r ON r.role_id = u.role_id"
                    " WHERE r.code = %s AND u.is_active = 1 AND u.user_id <> %s", (roles.SUPERADMIN, user_id))
        return int(cur.fetchone()["n"])

    def save_admin(self, email, display_name, password, actor=None, request_id=None, mode=None):
        """Create an administrator or change a password (SP sp_guardar_admin).
        Only the scrypt hash reaches the database. New accounts are always ADMIN.
        mode='create' rejects an existing email; mode='password' requires it; None does either."""
        email = normalize_email(email)
        if not EMAIL.fullmatch(email):
            raise ValueError("Enter a valid email address.")
        check_password(password)
        name = (display_name or "").strip()[:120] or None
        if name and CONTROL.search(name):
            raise ValueError("The display name must contain 1 to 120 characters.")
        with self.db.tx() as cur:
            target = self.find_user(cur, email)
            if mode == "create" and target is not None:
                raise ValueError("An account with that email already exists.")
            if mode == "password" and target is None:
                raise ValueError("That account does not exist.")
            if target is not None and target["role"] not in roles.STAFF:
                raise ValueError("That email belongs to a user who is not an administrator.")
            self.check_manager(actor, target)
            _, accion = self.call_procedure(cur, "sp_guardar_admin", [
                email, name, generate_password_hash(password),
                str(uuid.uuid4()), actor["user_id"] if actor else None, str(uuid.uuid4()), str(uuid.uuid4()),
                (request_id or uuid.uuid4().hex)[:100]], 2)
        return accion

    def add_user(self, email, password, role="ADMIN", display_name=None, actor=None):
        """Command line: create ADMIN or SUPERADMIN (the panel only creates ADMIN through save_admin)."""
        if role not in roles.STAFF:
            raise ValueError("Only administrators have accounts; visitors do not sign in.")
        if actor is not None and role != roles.ADMIN:
            raise ValueError("Accounts created in the panel are always administrators.")
        with self.db.tx() as cur:
            if self.find_user(cur, normalize_email(email)):
                raise ValueError("A user with that email already exists.")
        accion = self.save_admin(email, display_name, password, actor)
        if role == roles.SUPERADMIN:
            self.set_role(email, roles.SUPERADMIN, actor)
        return accion

    def set_password(self, email, password, actor=None):
        with self.db.tx() as cur:
            if not self.find_user(cur, normalize_email(email)):
                raise ValueError("User not found.")
        return self.save_admin(email, None, password, actor)

    def change_own_password(self, user, current, new):
        with self.db.tx() as cur:
            cur.execute("SELECT password_hash FROM users WHERE user_id = %s", (user["user_id"],))
            fila = cur.fetchone()
        if not fila or not password_matches(fila["password_hash"], (current or "")[:129]):
            raise ValueError("Your current password is not correct.")
        return self.save_admin(user["username"], None, new, user)

    def set_role(self, email, role, actor=None):
        """Command line and migration only: the web and the API never change roles.
        Raises auth_version and revokes sessions so the new role applies at the next sign-in."""
        if role not in roles.STAFF:
            raise ValueError("Role must be ADMIN or SUPERADMIN.")
        email = normalize_email(email)
        now = utcnow()
        with self.db.tx() as cur:
            user = self.find_user(cur, email)
            if not user:
                raise ValueError("User not found.")
            if user["role"] == role:
                return False
            if user["role"] not in roles.STAFF:
                raise ValueError("That account is not an administrator.")
            if user["role"] == roles.SUPERADMIN and int(user["is_active"]) and \
                    not self.other_active_supers(cur, user["user_id"]):
                raise ValueError("The system must keep at least one active super administrator.")
            nuevo = self.role_id(cur, role)
            cur.execute("UPDATE users SET role_id = %s, auth_version = auth_version + 1 WHERE user_id = %s",
                        (nuevo, user["user_id"]))
            cur.execute("UPDATE sessions SET revoked_at_utc = %s WHERE user_id = %s AND revoked_at_utc IS NULL",
                        (now, user["user_id"]))
            self.insert_audit(cur, actor["user_id"] if actor else None, "ROLE_CHANGE", "USER", email, "SUCCESS",
                              before={"role": user["role"]}, after={"role": role}, reason="command line")
        return True

    def profile(self, user):
        with self.db.tx() as cur:
            cur.execute("SELECT u.normalized_email AS username, u.display_name, r.code AS role, u.created_at_utc"
                        " FROM users u JOIN roles r ON r.role_id = u.role_id WHERE u.user_id = %s",
                        (user["user_id"],))
            return cur.fetchone()

    def update_own_profile(self, user, display_name):
        name = (display_name or "").strip()
        if not 1 <= len(name) <= 120 or CONTROL.search(name):
            raise ValueError("The display name must contain 1 to 120 characters.")
        with self.db.tx() as cur:
            cur.execute("SELECT display_name FROM users WHERE user_id = %s", (user["user_id"],))
            before = cur.fetchone()
            if not before:
                raise ValueError("User not found.")
            cur.execute("UPDATE users SET display_name = %s WHERE user_id = %s", (name, user["user_id"]))
            self.insert_audit(cur, user["user_id"], "PROFILE_UPDATE", "USER", user.get("username", ""), "SUCCESS",
                              before={"display_name": before["display_name"]}, after={"display_name": name})

    def set_active(self, email, active, actor=None):
        email = normalize_email(email)
        now = utcnow()
        with self.db.tx() as cur:
            user = self.find_user(cur, email)
            if not user:
                raise ValueError("User not found.")
            if actor is not None:
                if not roles.es_super(actor):
                    raise ValueError("Only the super administrator can manage administrator accounts.")
                if user["user_id"] == actor.get("user_id"):
                    raise ValueError("You cannot change your own account status.")
                if user["role"] == roles.SUPERADMIN:
                    raise ValueError("A super administrator account cannot be disabled or enabled from the panel.")
            if user["role"] == roles.SUPERADMIN and not active and int(user["is_active"]) and \
                    not self.other_active_supers(cur, user["user_id"]):
                raise ValueError("The system must keep at least one active super administrator.")
            # Raising auth_version invalidates every token already issued: immediate revocation.
            cur.execute("UPDATE users SET is_active = %s, auth_version = auth_version + 1 WHERE user_id = %s",
                        (1 if active else 0, user["user_id"]))
            cur.execute("UPDATE sessions SET revoked_at_utc = %s WHERE user_id = %s AND revoked_at_utc IS NULL",
                        (now, user["user_id"]))
            revoked = cur.rowcount
            self.insert_audit(cur, actor["user_id"] if actor else None,
                              "USER_ENABLE" if active else "USER_DISABLE", "USER", email, "SUCCESS",
                              before={"is_active": bool(int(user["is_active"]))},
                              after={"is_active": active, "revoked_sessions": revoked})

    def disable(self, email, actor=None):
        self.set_active(email, False, actor)

    def enable(self, email, actor=None):
        self.set_active(email, True, actor)

    def list_users(self):
        with self.db.tx() as cur:
            cur.execute("SELECT u.normalized_email AS username, u.display_name, r.code AS role, u.is_active AS active,"
                        " u.created_at_utc FROM users u JOIN roles r ON r.role_id = u.role_id"
                        f" WHERE r.code IN {STAFF_SQL} ORDER BY u.created_at_utc")
            return [dict(row, active=bool(int(row["active"]))) for row in cur.fetchall()]

    def count_admins(self):
        """Active accounts that can sign in (ADMIN + SUPERADMIN)."""
        with self.db.tx() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users u JOIN roles r ON r.role_id = u.role_id"
                        f" WHERE r.code IN {STAFF_SQL} AND u.is_active = 1")
            return int(cur.fetchone()["n"])

    def count_superadmins(self):
        with self.db.tx() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users u JOIN roles r ON r.role_id = u.role_id"
                        " WHERE r.code = %s AND u.is_active = 1", (roles.SUPERADMIN,))
            return int(cur.fetchone()["n"])

    # ------------------------------------------------------------------ audit
    def audit(self, actor, action, entity_type, entity_key, outcome, before=None, after=None,
              reason=None, request_id=None):
        with self.db.tx() as cur:
            self.insert_audit(cur, actor["user_id"] if actor else None, action, entity_type, entity_key,
                              outcome, before, after, reason, request_id)

    def audit_log(self, limit=100, actor=None):
        """All events, or only the events of `actor` (what an ADMIN may see)."""
        filtro, params = ("WHERE a.actor_id = %s", (actor["user_id"], int(limit))) if actor is not None \
            else ("", (int(limit),))
        with self.db.tx() as cur:
            cur.execute("SELECT a.occurred_at_utc, COALESCE(u.normalized_email, '—') AS actor, a.action,"
                        " a.entity_type, a.entity_key, a.outcome, a.reason, a.request_id"
                        " FROM audit_events a LEFT JOIN users u ON u.user_id = a.actor_id"
                        f" {filtro} ORDER BY a.occurred_at_utc DESC LIMIT %s", params)
            return cur.fetchall()

    def rotate_audit(self, days, actor=None, request_id=None):
        """SP sp_rotar_auditoria: archive old events and remove dead sessions."""
        with self.db.tx() as cur:
            movidos, sesiones = self.call_procedure(cur, "sp_rotar_auditoria", [
                int(days), actor["user_id"] if actor else None, str(uuid.uuid4()), str(uuid.uuid4()),
                (request_id or uuid.uuid4().hex)[:100]], 2)
        return int(movidos or 0), int(sesiones or 0)

    def history_count(self):
        try:
            with self.db.tx() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM audit_events_history")
                return int(cur.fetchone()["n"])
        except Exception:
            return None

    # ------------------------------------------------------------------ catalogs
    def catalog(self, fresh=False):
        loaded, cached = self.catalog_cache
        if cached and not fresh and time.time() - loaded < 60:
            return cached
        with self.db.tx() as cur:
            cur.execute("SELECT l.location_id, l.name, l.city, l.state, l.latitude, l.longitude, l.timezone_name,"
                        " s.station_id, s.name AS station_name, s.station_type FROM locations l"
                        " JOIN stations s ON s.location_id = l.location_id"
                        " WHERE l.is_active = 1 AND s.is_active = 1 ORDER BY l.location_id")
            locations = [{k: (str(v) if k in ("latitude", "longitude") else v) for k, v in row.items()}
                         for row in cur.fetchall()]
            cur.execute("SELECT source_id, name FROM data_sources WHERE is_active = 1 ORDER BY priority, source_id")
            sources = list(cur.fetchall())
            cur.execute("SELECT condition_id, name FROM weather_conditions WHERE is_active = 1 ORDER BY condition_id")
            conditions = list(cur.fetchall())
        catalog = build_catalog(locations, sources, conditions)
        self.catalog_cache = (time.time(), catalog)
        return catalog


# ---------------------------------------------------------------------- RDS connection
def ssl_context():
    """Encrypted connection. Verifies the server if global-bundle.pem is in the project or etl folder."""
    base = Path(__file__).resolve().parent.parent
    for bundle in (base / "global-bundle.pem", base / "etl" / "global-bundle.pem"):
        if bundle.exists():
            return ssl.create_default_context(cafile=str(bundle))
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def rds_endpoint(instance, region):
    import boto3
    data = boto3.client("rds", region_name=region).describe_db_instances(
        DBInstanceIdentifier=instance)["DBInstances"][0]
    if data["DBInstanceStatus"] != "available":
        raise DatabaseUnavailable(f"The RDS instance {instance} is '{data['DBInstanceStatus']}'. Start it and wait.")
    return data["Endpoint"]["Address"], data["Endpoint"]["Port"]


def mysql_factory(host, port, user, password, database="climate_transactional"):
    import pymysql

    def connect():
        return pymysql.connect(host=host, port=int(port), user=user, password=password, database=database,
                               ssl=ssl_context(), connect_timeout=10, read_timeout=20, write_timeout=20,
                               charset="utf8mb4", autocommit=False, cursorclass=pymysql.cursors.DictCursor)
    return connect
