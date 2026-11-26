"""Layer 1: Flask web portal + REST API for Mexico City weather. Launch with python app.py; no dotenv.

Visitors (no sign-in):  /  -> current weather, 7-day forecast and hour-by-hour breakdown.
Administrators:         /records, /add, /edit, /delete, /import, /admin/users, /admin/audit,
                        /account/password and the REST API (/api, Bearer JWT).

Demo mode:  python app.py                 (sample data, local accounts, no AWS)
Offline:    python app.py --datos-reales  (the 10,000 real readings from etl/datos/lecturas.jsonl, no AWS)
Option A:   python app.py --mode api --api-url https://...amazonaws.com  (no AWS credentials; also on Vercel)
AWS mode:   python app.py --mode lambda   (Lambdas + DynamoDB; users/sessions/audit/catalogs in MySQL;
                                           readings and forecast data in PostgreSQL;
                                           Idempotency-Key in DynamoDB ClimateIdempotency)
"""
import argparse
import os
import secrets
import sys
from datetime import date, datetime, timedelta
from getpass import getpass
from pathlib import Path
from services import avisos, ingles
from services.access import AccessStore
from services.avisos import avisar, avisar_error, explicar
from services.analytics import DemoAnalytics
from services.api import bp as api_blueprint
from services.catalog import AUTO_CONDITION, FIXED_LOCATION, FIXED_STATION, check_record, fill_station, resolve_condition
from services.idempotency import MemoryIdempotency
from services.imports import register_imports
from services.jwt_tokens import TokenSigner
from services.mysql_store import DatabaseUnavailable
from flask import g, Flask, abort, flash, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.exceptions import HTTPException
from contract import REQUIRED, normalize_record, normalize_time
from services.endpoints import DemoClient, LambdaClient, ApiError

FIELDS = [
    ("location_id", "Location ID", "text"), ("timestamp_utc", "Timestamp (UTC)", "text"),
    ("station_id", "Station ID", "text"), ("source_id", "Source ID", "text"),
    ("condition_id", "Condition ID", "text"),
    ("temperature_c", "Temperature (°C)", "number"), ("feels_like_c", "Feels like (°C)", "number"),
    ("humidity_pct", "Humidity (%)", "number"), ("pressure_hpa", "Pressure (hPa)", "number"),
    ("wind_speed_kmh", "Wind speed (km/h)", "number"), ("wind_direction", "Wind direction", "text"),
    ("precipitation_mm", "Precipitation (mm)", "number"),
    ("precipitation_period_minutes", "Precipitation period (minutes)", "number"),
    ("cloud_cover_pct", "Cloud cover (%)", "number"), ("visibility_km", "Visibility (km)", "number"),
    ("uv_index", "UV index", "number"), ("dew_point_c", "Dew point (°C)", "number")]
KEYS = ("location_id", "timestamp_utc")
PUBLIC_PAGES = ('public_home', 'login')

ERRORES = {
    400: ('The request is not valid', 'Something in the form or in the address is not right.',
          'Reload the page and try again.'),
    403: ('You do not have permission', 'This section is for administrators only.',
          'Sign in with an administrator account.'),
    404: ('We could not find that page', 'The address does not exist or what you were looking for is gone.',
          'Check the address or go back to the home page.'),
    405: ('Action not allowed', 'This page does not accept that action.', 'Use the buttons on the page.'),
    413: ('The file is too large', 'The limit is 256 KB per file.', 'Split the file into smaller parts.'),
    500: ('Something went wrong', 'An unexpected error happened in the portal.',
          'Try again in a moment. If it happens again, tell the team.'),
    503: ('Service unavailable', avisos.SIN_CONEXION, 'Start the lab and the 2 RDS databases, wait until they say '
          'Available and try again.'),
}


def create_app(client=None, mode="demo", auth_path=None, auth_store=None, idempotency=None, analytics=None,
               secret_key=None):
    app = Flask(__name__)
    app.secret_key = secret_key or secrets.token_hex(32)
    avisos.registrar(app)
    ingles.registrar(app)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=300000,
                      CLIMATE_MODE=mode)
    client = client if client is not None else DemoClient()
    signer = auth_store.signer if auth_store is not None else TokenSigner()
    local = AccessStore(auth_path or Path(__file__).parent / 'instance' / 'portal.sqlite3', signer)
    store = auth_store or local
    analytics = analytics or DemoAnalytics()
    app.extensions.update(climate_client=client, access_store=store, auth_store=store, local_store=local,
                          idempotency=idempotency or MemoryIdempotency(), analytics=analytics)
    app.register_blueprint(api_blueprint)
    register_imports(app, local, client, secrets.token_urlsafe(24), store)
    signer_forms = URLSafeTimedSerializer(app.secret_key, salt="record-version")

    def audit(action, entity_type, entity_key, outcome, before=None, after=None, reason=None):
        try: store.audit(g.get('user'), action, entity_type, entity_key, outcome, before, after, reason)
        except Exception as exc: app.logger.warning('Audit event not saved: %s', type(exc).__name__)

    def catalog():
        if 'catalog' not in g:
            try: g.catalog = store.catalog()
            except DatabaseUnavailable: g.catalog = None
        return g.catalog

    def sync_analytics(records, origin, quiet=False):
        """SP sp_upsert_lecturas: PostgreSQL gets the readings now instead of waiting for the ETL."""
        cat = catalog()
        sources = sorted(cat['source_ids']) if cat else ['SRC-MANUAL', 'SRC-OPENMETEO']
        conditions = sorted(cat['condition_ids']) if cat else []
        try:
            result = analytics.upsert(records, origin, sources, conditions)
        except Exception as exc:
            app.logger.warning('sp_upsert_lecturas failed: %s', exc)
            if not quiet:
                avisar('warning', 'The reading was saved in DynamoDB, but PostgreSQL could not be updated right now. '
                       'The forecast will include it after the next load (step 5).', str(exc)[:200])
            return {'status': 'pending', 'message': 'The next ETL run will copy it.'}
        if result and not quiet:
            texto = (f"PostgreSQL updated by sp_upsert_lecturas: {result['insertados']} new, "
                     f"{result['actualizados']} updated, {result['omitidos']} unchanged, "
                     f"{result['rechazados']} rejected.")
            if result['rechazados']:
                avisar('warning', texto + ' Rejected readings stay in the PostgreSQL anomaly queue '
                       '(stg_observations); the forecast does not use them.')
            else:
                avisar('info', texto)
        return result or {'status': 'demo'}

    app.extensions.update(audit=audit, analytics_sync=sync_analytics)

    @app.context_processor
    def context():
        if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(32)
        reales = len(analytics.horas) if getattr(analytics, "datos_reales", False) else 0
        return dict(csrf=session["csrf"], mode=mode, fields=FIELDS, current_user=g.get("user"),
                    users_store=store.kind, datos_reales=reales, etiquetas=avisos.ETIQUETAS, rumbos=avisos.RUMBOS)

    def pagina_error(codigo, detalle=None):
        titulo, mensaje, que_hacer = ERRORES.get(codigo, ERRORES[500])
        if 'user' not in g:                   # rutas que no existen no pasan por authorize()
            try: g.user = store.user(session.get('sid', '')) if session.get('sid') else None
            except Exception: g.user = None
        if detalle:
            claro, tecnico = explicar(detalle)
            if tecnico:                       # se reconoció: el mensaje claro va arriba
                mensaje, detalle = claro, tecnico
        return render_template('error.html', title=titulo, message=mensaje, que_hacer=que_hacer,
                               detalle=detalle, codigo=codigo), codigo

    @app.errorhandler(DatabaseUnavailable)
    def database_down(exc):
        return pagina_error(503, str(exc))

    @app.errorhandler(HTTPException)
    def error_http(exc):
        if exc.code is None or exc.code < 400 or request.path.startswith('/api/') or request.blueprint == 'api':
            return exc
        descripcion = exc.description if exc.description and not str(exc.description).startswith(
            ('The ', 'You ', 'Method ', 'The requested', 'Bad Request')) else None
        return pagina_error(exc.code if exc.code in ERRORES else 500, descripcion)

    @app.errorhandler(500)
    def error_interno(exc):
        if request.path.startswith('/api/'):
            return {'error': 'Internal server error'}, 500
        return pagina_error(500)

    @app.before_request
    def authorize():
        if request.blueprint == 'api' or request.endpoint in ('static', 'health', 'favicon'): return
        # Rutas que no existen (robots.txt, apple-touch-icon.png...): 404 normal, sin tocar la sesión.
        # Antes se trataban como páginas protegidas: borraban la sesión y el login fallaba con
        # "Invalid form token" porque el navegador pide /favicon.ico mientras muestra el formulario.
        if request.endpoint is None: return
        try:
            g.user = store.user(session.get('sid', ''))
        except DatabaseUnavailable:
            if request.endpoint in PUBLIC_PAGES: g.user = None
            else: raise
        if request.endpoint in PUBLIC_PAGES: return
        if not g.user:
            if session.pop('sid', None):     # se quita la sesión vencida; el token del formulario se conserva
                avisar('warning', 'Your session ended: 30 minutes passed without use, it was closed somewhere else '
                       'or the password changed. Please sign in again.')
            if request.method == 'GET':
                session['despues'] = request.full_path.rstrip('?')
            return redirect(url_for('login'))
        if g.user['role'] != 'ADMIN': abort(403)

    @app.get('/health')
    def health(): return {'status': 'ok'}

    @app.get('/favicon.ico')
    def favicon(): return '', 204

    # ------------------------------------------------------------------ public weather page (visitors)
    @app.get('/')
    def public_home():
        try: chosen = date.fromisoformat(request.args.get('dia', ''))
        except ValueError: chosen = None
        try:
            data = analytics.pagina(chosen)
            problem = None
        except Exception as exc:
            app.logger.warning('Forecast unavailable: %s', exc)
            data, problem = None, 'The forecast is not available right now. Please try again later.'
        return render_template('public.html', data=data, problem=problem)

    # ------------------------------------------------------------------ session
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            try:
                token = store.login(request.form.get('username', ''), request.form.get('password', ''))
            except DatabaseUnavailable as exc:
                avisar('error', 'Your account could not be checked because the user database is not responding. '
                       + avisos.SIN_CONEXION, str(exc))
                return render_template('login.html'), 503
            if token:
                destino = session.get('despues', '')
                store.logout(session.get('sid', ''))
                session.clear()
                session['sid'] = token
                session['csrf'] = secrets.token_urlsafe(32)
                seguro = destino.startswith('/') and not destino.startswith('//') and not destino.startswith('/login')
                return redirect(destino if seguro else url_for('inicio'))
            avisar('error', 'We could not sign you in. Check your email and password. After 5 failed attempts in a '
                   'row the account is locked for 15 minutes.')
        return render_template('login.html')

    @app.post('/logout')
    def logout():
        store.logout(session.get('sid', ''))
        session.clear()
        avisar('success', 'You signed out.')
        return redirect(url_for('login'))

    @app.before_request
    def protect_form():
        if request.method == "POST" and request.blueprint != 'api':
            expected = session.get("csrf", "")
            actual = request.form.get("csrf", "")
            if not expected or not secrets.compare_digest(expected.encode(), actual.encode()):
                app.logger.warning("CSRF rechazado: token en la sesion=%s, token en el formulario=%s, "
                                   "cookies recibidas=%s, content-length=%s", bool(expected), bool(actual),
                                   sorted(request.cookies), request.content_length)
                abort(400, "Invalid form token. Reload the page and try again.")
            if any(len(request.form.getlist(k)) != 1 for k in request.form):
                abort(400, "Duplicate form fields are not allowed.")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    # ------------------------------------------------------------------ records (administrators)
    def keys_from_url():
        if set(request.args) != set(KEYS) or any(len(request.args.getlist(k)) != 1 for k in KEYS):
            abort(400, "Location ID and timestamp are required.")
        keys = {k: request.args[k] for k in KEYS}
        try:
            from services.reading_service import keys as validate_keys
            if validate_keys(keys) != keys: raise ValueError()
        except ValueError: abort(400, "Invalid record keys.")
        return keys

    def snapshot(record, action):
        return signer_forms.dumps({**{k: record[k] for k in KEYS}, "version": int(record["version"]),
                                   "action": action, "csrf": session["csrf"]})

    def version_from_form(keys, action):
        try:
            saved = signer_forms.loads(request.form.get("snapshot", ""), max_age=1800)
            if any(saved[k] != keys[k] for k in KEYS) or saved["action"] != action or saved["csrf"] != session["csrf"]:
                raise ValueError()
            return saved["version"]
        except (BadSignature, KeyError, ValueError):
            raise ValueError("This form has expired or changed. Reopen the record.") from None

    def parse_form(editing=False):
        extra = set(request.form) - REQUIRED - {"csrf", "snapshot"}
        if extra: raise ValueError("Unknown form fields.")
        data = fill_station({k: request.form.get(k, "").strip() for k in REQUIRED}, catalog())
        validated = normalize_record(data, allow_text=True)
        # Let the backend create the UUID and preserve the existing one on update.
        validated.pop("record_id")
        return check_record(resolve_condition(validated), catalog())

    def safe_get(keys):
        try: return client.get(keys)
        except ApiError: return None

    @app.get("/admin")
    def inicio():
        try: pagina = analytics.pagina()
        except Exception as exc:
            app.logger.warning('Estado del pronóstico no disponible: %s', exc)
            pagina = None
        try: admins = sum(1 for u in store.list_users() if u['active'])
        except Exception: admins = None
        try: eventos = store.audit_log(5)
        except Exception: eventos = []
        return render_template("inicio.html", pagina=pagina, admins=admins, eventos=eventos)

    @app.get("/records")
    def index():
        q = request.args.get("q", "").strip()
        rows, cursor = [], ""
        try: rows, cursor = client.list_records(q, request.args.get("cursor", ""))
        except ApiError as exc: avisar_error(exc)
        return render_template("index.html", rows=rows, q=q, cursor=cursor)

    @app.get("/records/ir")
    def ir_a_hora():
        """Abre la lectura de un día y hora de la CDMX (se convierte a UTC)."""
        try:
            local = datetime.strptime(f"{request.args.get('dia', '')} {request.args.get('hora', '')}", "%Y-%m-%d %H")
        except ValueError:
            avisar('error', 'Choose a valid day and hour.')
            return redirect(url_for('index'))
        utc = (local + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:00Z")
        return redirect(url_for('edit', location_id=FIXED_LOCATION, timestamp_utc=utc))

    def form_page(data, editing, token):
        return render_template("form.html", data=data, editing=editing, snapshot=token, catalog=catalog(),
                               auto=AUTO_CONDITION)

    @app.route("/add", methods=["GET", "POST"])
    def add():
        data = request.form.to_dict() if request.method == "POST" else {
            "location_id": FIXED_LOCATION, "station_id": FIXED_STATION, "source_id": "SRC-MANUAL",
            "precipitation_period_minutes": 60, "condition_id": AUTO_CONDITION if catalog() else ""}
        if request.method == "POST":
            record = None
            try:
                record = parse_form()
                result = client.create(record)
                audit("READING_CREATE", "READING", record["location_id"] + "|" + record["timestamp_utc"], "SUCCESS",
                      after=result.get("record"))
                avisar("success", "Reading saved in DynamoDB (version 1).")
                if result.get("record"): sync_analytics([result["record"]], "alta")
                return redirect(url_for("index"))
            except (ApiError, ValueError) as exc:
                if record: audit("READING_CREATE", "READING", record["location_id"] + "|" + record["timestamp_utc"],
                                 "REJECTED", after=record, reason=str(exc))
                avisar_error(exc)
        return form_page(data, False, "")

    @app.route("/edit", methods=["GET", "POST"])
    def edit():
        keys = keys_from_url()
        token = request.form.get("snapshot", "")
        if request.method == "POST":
            data = request.form.to_dict()
            entity = keys["location_id"] + "|" + keys["timestamp_utc"]
            try:
                version = version_from_form(keys, "edit")
                parsed = parse_form(True)
                if any(parsed[k] != keys[k] or data[k] != keys[k] for k in KEYS):
                    raise ValueError("Record keys cannot be changed.")
                before = safe_get(keys)
                try:
                    result = client.update({**parsed, "expected_version": version})
                except ApiError as exc:
                    audit("READING_UPDATE", "READING", entity, "REJECTED", before=before, after=parsed, reason=str(exc))
                    raise
                audit("READING_UPDATE", "READING", entity, "SUCCESS", before=before, after=result.get("record"))
                avisar("success", f"Changes saved. The reading is now at version {(result.get('record') or {}).get('version', version + 1)}.")
                if result.get("record"): sync_analytics([result["record"]], "edicion")
                return redirect(url_for("index"))
            except (ApiError, ValueError) as exc: avisar_error(exc)
        else:
            try:
                data = client.get(keys)
                context()
                token = snapshot(data, "edit")
            except ApiError as exc:
                if getattr(exc, "status", None) == 404:
                    avisar("warning", f"There is no reading saved for {avisos.hora_local(keys['timestamp_utc'])} "
                           "(Mexico City time). You can enter it with “New reading”.")
                else:
                    avisar_error(exc)
                return redirect(url_for("index"))
        return form_page(data, True, token)

    @app.route("/delete", methods=["GET", "POST"])
    def delete():
        keys = keys_from_url()
        token = request.form.get("snapshot", "")
        if request.method == "POST":
            entity = keys["location_id"] + "|" + keys["timestamp_utc"]
            try:
                if set(request.form) != {"csrf", "snapshot"}: raise ValueError("Unknown form fields.")
                try:
                    result = client.delete({**keys, "expected_version": version_from_form(keys, "delete")})
                except ApiError as exc:
                    audit("READING_DELETE", "READING", entity, "REJECTED", reason=str(exc))
                    raise
                audit("READING_DELETE", "READING", entity, "SUCCESS", before=result.get("record"))
                avisar("success", "Reading deleted from DynamoDB. PostgreSQL will mark it as deleted on the next "
                       "full load (step 5); until then the forecast keeps using it.")
                return redirect(url_for("index"))
            except (ApiError, ValueError) as exc: avisar_error(exc)
        else:
            try:
                record = client.get(keys)
                context()
                token = snapshot(record, "delete")
            except ApiError as exc:
                avisar_error(exc)
                return redirect(url_for("index"))
        return render_template("delete.html", keys=keys, snapshot=token)

    # ------------------------------------------------------------------ administrators
    @app.route('/admin/users', methods=['GET', 'POST'])
    def admin_users():
        if request.method == 'POST':
            try:
                if request.form.get('password', '') != request.form.get('confirm', ''):
                    raise ValueError('Passwords do not match.')
                action = store.save_admin(request.form.get('username', '').strip(), request.form.get('display_name', ''),
                                          request.form.get('password', ''), actor=g.user)
                avisar('success', 'Administrator created. Share the password through a private channel.' if action == 'CREADO'
                       else 'Password changed. All of that administrator\'s sessions were closed right away.')
                return redirect(url_for('admin_users'))
            except ValueError as exc: avisar_error(exc)
        try: usuarios = store.list_users()
        except DatabaseUnavailable as exc:
            avisar_error(exc)
            usuarios = []
        return render_template('admin_users.html', users=usuarios)

    @app.post('/admin/users/status')
    def admin_user_status():
        username, action = request.form.get('username', ''), request.form.get('action', '')
        try:
            if username.lower() == g.user['username'].lower(): raise ValueError('You cannot change your own account here.')
            if action == 'disable': store.disable(username, actor=g.user)
            elif action == 'enable': store.enable(username, actor=g.user)
            else: raise ValueError('Unknown action.')
            avisar('success', 'Administrator disabled: all of their sessions were closed right away.' if action == 'disable'
                   else 'Administrator enabled. They will need to sign in again.')
        except ValueError as exc: avisar_error(exc)
        return redirect(url_for('admin_users'))

    @app.route('/account/password', methods=['GET', 'POST'])
    def account_password():
        if request.method == 'POST':
            try:
                if request.form.get('password', '') != request.form.get('confirm', ''):
                    raise ValueError('The new passwords do not match.')
                store.change_own_password(g.user, request.form.get('current', ''), request.form.get('password', ''))
                store.logout(session.get('sid', ''))
                session.clear()
                avisar('success', 'Password changed. Sign in again with the new password.')
                return redirect(url_for('login'))
            except ValueError as exc: avisar_error(exc)
        return render_template('account_password.html')

    @app.get('/admin/audit')
    def admin_audit():
        try: eventos = store.audit_log(150)
        except DatabaseUnavailable as exc:
            avisar_error(exc)
            eventos = []
        return render_template('admin_audit.html', events=eventos, history=store.history_count())

    @app.post('/admin/audit/rotate')
    def admin_audit_rotate():
        try:
            moved, sessions = store.rotate_audit(int(request.form.get('days', '30')), actor=g.user)
            avisar('success', f'Audit log archived by sp_rotar_auditoria: {moved} events moved to the history table and '
                   f'{sessions} expired sessions were cleaned up.')
        except ValueError as exc: avisar_error(exc)
        return redirect(url_for('admin_audit'))

    return app


# ---------------------------------------------------------------------- AWS connections
def endpoint(env_host, env_port, instance, default_port, region):
    from services.mysql_store import rds_endpoint
    if os.environ.get(env_host):
        return os.environ[env_host], int(os.environ.get(env_port, default_port))
    return rds_endpoint(instance, region)


def mysql_store(region):
    """RDS MySQL climate_transactional; the password is typed without echo and never saved."""
    from services.mysql_store import MySQLAccessStore, mysql_factory
    host, port = endpoint('CLIMA_MYSQL_HOST', 'CLIMA_MYSQL_PORT', 'climate-mysql', 3306, region)
    user = os.environ.get('CLIMA_MYSQL_USER') or input('Usuario de MySQL (el de DBeaver): ').strip()
    password = getpass('Contraseña de MySQL (no se muestra): ')
    store = MySQLAccessStore(mysql_factory(host, port, user, password))
    admins = store.count_admins()
    print(f'MySQL conectado: {admins} administrador(es) activo(s).')
    if admins == 0:
        print('\nNo hay administradores en la tabla users. Vamos a crear el primero (sp_guardar_admin).')
        email = input('Correo del administrador: ').strip()
        name = input('Nombre para mostrar: ').strip()
        first = getpass('Contraseña nueva (12 a 128 caracteres, no se muestra): ')
        if first != getpass('Repite la contraseña: '):
            raise SystemExit('Las contraseñas no coinciden. Vuelve a ejecutar app.py.')
        store.save_admin(email, name, first)
        print('Administrador creado en MySQL. Inicia sesión con ese correo.\n')
    return store


def postgres_analytics(region):
    """RDS PostgreSQL climate_analytics: readings for the public page and sp_upsert_lecturas."""
    from services.analytics import PostgresAnalytics, pg_factory
    host, port = endpoint('CLIMA_PG_HOST', 'CLIMA_PG_PORT', 'climate-postgres', 5432, region)
    user = os.environ.get('CLIMA_PG_USER') or input('Usuario de PostgreSQL (el de DBeaver): ').strip()
    password = getpass('Contraseña de PostgreSQL (no se muestra): ')
    analytics = PostgresAnalytics(pg_factory(host, port, user, password))
    horas = analytics.historial()
    print(f'PostgreSQL conectado: {len(horas):,} lecturas por hora de la CDMX.')
    return analytics


def idempotency_store(region):
    from services.idempotency import DynamoIdempotency
    try:
        store = DynamoIdempotency('ClimateIdempotency', region)
        print('Idempotency-Key:', store.description)
        return store
    except Exception as exc:
        print(f'AVISO: no se pudo usar ClimateIdempotency ({type(exc).__name__}: {str(exc)[:160]}).')
        print('       Las claves de idempotencia se guardarán solo en memoria mientras el portal esté abierto.')
        return MemoryIdempotency()


def remote_backend(url):
    """Opción A: todo pasa por API Gateway; sin credenciales de AWS ni contraseñas de las bases."""
    from services.remoto import (ApiRemota, RemoteAnalytics, RemoteClient, RemoteIdempotency,
                                 RemoteStore)
    api = ApiRemota(url)
    return dict(client=RemoteClient(api), auth_store=RemoteStore(api), analytics=RemoteAnalytics(api),
                idempotency=RemoteIdempotency(api)), api


def crear_app_desde_entorno():
    """Vercel: la app se arma con variables de entorno (CLIMA_API_URL y CLIMA_SECRET_KEY)."""
    if not os.environ.get("VERCEL"):
        return None
    url, secreto = os.environ.get("CLIMA_API_URL", ""), os.environ.get("CLIMA_SECRET_KEY", "")
    if not url or len(secreto) < 32:
        fallo = Flask(__name__)

        @fallo.route("/", defaults={"ruta": ""})
        @fallo.route("/<path:ruta>")
        def sin_configurar(ruta):
            return ("CLIMA_API_URL and CLIMA_SECRET_KEY (32+ characters) are not set. Add them in "
                    "Vercel → Settings → Environment Variables, then Redeploy."), 503
        return fallo
    partes, _ = remote_backend(url)
    web = create_app(mode="api", auth_path="/tmp/portal.sqlite3", secret_key=secreto, **partes)
    # Lax: la cookie viaja al navegar dentro del sitio y sigue sin enviarse en POST desde otros sitios.
    web.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax")
    web.wsgi_app = CuerpoCompleto(web.wsgi_app)
    return web


class CuerpoCompleto:
    """Si la plataforma entrega el cuerpo de la petición sin CONTENT_LENGTH, Flask no lee el formulario.
    Este envoltorio lo lee completo y pone la longitud."""

    def __init__(self, siguiente):
        self.siguiente = siguiente

    def __call__(self, environ, start_response):
        if environ.get("REQUEST_METHOD") in ("POST", "PUT", "PATCH", "DELETE") and not environ.get("CONTENT_LENGTH"):
            import io
            cuerpo = environ["wsgi.input"].read() or b""
            environ["wsgi.input"] = io.BytesIO(cuerpo)
            environ["CONTENT_LENGTH"] = str(len(cuerpo))
        return self.siguiente(environ, start_response)


# Vercel busca, en este archivo, una variable "app" de nivel superior que sea una aplicación Flask.
# En Vercel se reemplaza por el portal real (modo API); en tu PC se usa main() y esta queda sin uso.
app = Flask(__name__)
_portal_vercel = crear_app_desde_entorno()
if _portal_vercel is not None:
    app = _portal_vercel


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("demo", "lambda", "api"), default="demo",
                        help="api = opción A: solo la URL de API Gateway, sin credenciales de AWS")
    parser.add_argument("--api-url", default=os.environ.get("CLIMA_API_URL", ""),
                        help="URL de API Gateway para --mode api (o variable CLIMA_API_URL)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--users", choices=("auto", "local", "mysql"), default="auto",
                        help="where accounts live (auto: MySQL in lambda mode, local SQLite in demo)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--datos-reales", action="store_true",
                        help="demo sin internet con las lecturas reales de etl/datos/lecturas.jsonl")
    parser.add_argument("--lecturas", default=str(Path(__file__).parent / "etl" / "datos" / "lecturas.jsonl"),
                        help="archivo de lecturas para --datos-reales")
    args = parser.parse_args()
    if args.datos_reales and args.mode != "demo":
        sys.exit("--datos-reales solo funciona en modo demo: python app.py --datos-reales")
    if args.mode == "api":
        if not args.api_url:
            sys.exit("Falta la URL de la API: python app.py --mode api --api-url https://xxxx.execute-api.us-east-1.amazonaws.com")
        try:
            partes, api = remote_backend(args.api_url)
            estado = api.llamar("estado")
        except Exception as exc:
            sys.exit(f"No se pudo usar la API: {exc}\nRevisa que el laboratorio y las 2 RDS estén encendidos.")
        print(f"API de AWS lista (opción A, sin credenciales): {estado['mysql_admins']} administrador(es) en MySQL, "
              f"{estado['postgres_lecturas']:,} lecturas en PostgreSQL.")
        print(f'Página pública: http://127.0.0.1:{args.port}   Administración: http://127.0.0.1:{args.port}/login')
        create_app(mode="api", **partes).run(host="127.0.0.1", port=args.port, debug=False, load_dotenv=False)
        sys.exit(0)
    use_mysql = args.users == "mysql" or (args.users == "auto" and args.mode == "lambda")
    try:
        users = mysql_store(args.region) if use_mysql else None
        analytics = postgres_analytics(args.region) if args.mode == "lambda" else DemoAnalytics()
    except Exception as exc:
        print(f'No se pudo conectar: {type(exc).__name__}: {str(exc)[:200]}')
        print('Revisa: RDS encendidas, tu IP en el security group, usuario y contraseña, credenciales del lab.')
        sys.exit(1)
    backend = DemoClient() if args.mode == "demo" else LambdaClient(args.region)
    if args.datos_reales:
        from services.analytics import leer_lecturas
        try:
            lecturas = leer_lecturas(args.lecturas)
        except (OSError, ValueError) as exc:
            sys.exit(f"No se pudieron leer las lecturas reales: {exc}\n"
                     "Debe existir etl/datos/lecturas.jsonl (lo genera etl/paso2_transformar.py).")
        print(f"Cargando {len(lecturas):,} lecturas reales en memoria (sin AWS ni internet)...")
        analytics = DemoAnalytics.desde_lecturas(lecturas)
        backend = DemoClient(records=lecturas)
        print(f"Listo: {backend.loaded:,} lecturas de {lecturas[0]['timestamp_utc']} a {lecturas[-1]['timestamp_utc']}"
              + (f" ({backend.rejected} no pasaron la validación)" if backend.rejected else ""))
        print("Los cambios que hagas se guardan solo en memoria y se pierden al cerrar el portal.")
    idem = idempotency_store(args.region) if args.mode == "lambda" else MemoryIdempotency()
    print(f'Página pública: http://127.0.0.1:{args.port}   Administración: http://127.0.0.1:{args.port}/login')
    create_app(backend, args.mode, auth_store=users, idempotency=idem, analytics=analytics).run(
        host="127.0.0.1", port=args.port, debug=False, load_dotenv=False)
