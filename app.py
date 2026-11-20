"""Layer 1: local Flask UI. Launch with python app.py; no dotenv support."""
import argparse
import secrets
from pathlib import Path
from services.access import AccessStore
from services.imports import register_imports
from flask import g, Flask, abort, flash, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
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

def create_app(client=None, mode="demo", auth_path=None):
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(32)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", MAX_CONTENT_LENGTH=300000)
    client = client if client is not None else DemoClient()
    app.extensions["climate_client"] = client
    store = AccessStore(auth_path or Path(__file__).parent / 'instance' / 'portal.sqlite3')
    app.extensions['access_store'] = store
    register_imports(app, store, client, secrets.token_urlsafe(24))
    signer = URLSafeTimedSerializer(app.secret_key, salt="record-version")

    @app.context_processor
    def context():
        if "csrf" not in session: session["csrf"] = secrets.token_urlsafe(32)
        return dict(csrf=session["csrf"], mode=mode, fields=FIELDS, current_user=g.get("user"))

    @app.before_request
    def authorize():
        g.user = store.user(session.get('sid', ''))
        if request.endpoint in ('login','static','health'): return
        if not g.user:
            session.clear()
            return redirect(url_for('login'))
        restricted = ('add','edit','delete','import_records','import_preview','import_confirm','import_template')
        if request.endpoint in restricted and g.user['role'] == 'ANALYST': abort(403)
        if request.endpoint == 'delete' and g.user['role'] != 'ADMIN': abort(403)

    @app.get('/health')
    def health(): return {'status':'ok'}

    @app.route('/login', methods=['GET','POST'])
    def login():
        if request.method == 'POST':
            sid=store.login(request.form.get('username',''),request.form.get('password',''))
            if sid:
                store.logout(session.get('sid',''))
                session.clear()
                session['sid']=sid
                session['csrf']=secrets.token_urlsafe(32)
                return redirect(url_for('index'))
            flash('Sign-in failed. Check your details or try again in 15 minutes.', 'error')
        return render_template('login.html')

    @app.post('/logout')
    def logout():
        store.logout(session.get('sid',''))
        session.clear()
        return redirect(url_for('login'))

    @app.before_request
    def protect_form():
        if request.method == "POST":
            expected = session.get("csrf", "")
            actual = request.form.get("csrf", "")
            if not expected or not secrets.compare_digest(expected.encode(), actual.encode()):
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
        return signer.dumps({**{k: record[k] for k in KEYS}, "version": int(record["version"]),
                             "action": action, "csrf": session["csrf"]})

    def version_from_form(keys, action):
        try:
            saved = signer.loads(request.form.get("snapshot", ""), max_age=1800)
            if any(saved[k] != keys[k] for k in KEYS) or saved["action"] != action or saved["csrf"] != session["csrf"]:
                raise ValueError()
            return saved["version"]
        except (BadSignature, KeyError, ValueError):
            raise ValueError("This form has expired or changed. Reopen the record.") from None

    def parse_form(editing=False):
        extra = set(request.form) - REQUIRED - {"csrf", "snapshot"}
        if extra: raise ValueError("Unknown form fields.")
        data = {k: request.form.get(k, "").strip() for k in REQUIRED}
        validated = normalize_record(data, allow_text=True)
        # Let the backend create the UUID and preserve the existing one on update.
        validated.pop("record_id")
        return validated

    @app.get("/")
    def index():
        q = request.args.get("q", "").strip()
        rows, cursor = [], ""
        try: rows, cursor = client.list_records(q, request.args.get("cursor", ""))
        except ApiError as exc: flash(str(exc), "error")
        listOfDicts: list[dict] = rows
        return render_template("index.html", rows=listOfDicts, q=q, cursor=cursor)

    @app.route("/add", methods=["GET", "POST"])
    def add():
        data = request.form.to_dict() if request.method == "POST" else {"source_id": "SRC-MANUAL", "precipitation_period_minutes": 60}
        if request.method == "POST":
            try:
                client.create(parse_form())
                flash("Record added successfully.", "success")
                return redirect(url_for("index"))
            except (ApiError, ValueError) as exc: flash(str(exc), "error")
        return render_template("form.html", data=data, editing=False, snapshot="")

    @app.route("/edit", methods=["GET", "POST"])
    def edit():
        keys = keys_from_url()
        token = request.form.get("snapshot", "")
        if request.method == "POST":
            data = request.form.to_dict()
            try:
                version = version_from_form(keys, "edit")
                parsed = parse_form(True)
                if any(parsed[k] != keys[k] or data[k] != keys[k] for k in KEYS):
                    raise ValueError("Record keys cannot be changed.")
                client.update({**parsed, "expected_version": version})
                flash("Record updated successfully.", "success")
                return redirect(url_for("index"))
            except (ApiError, ValueError) as exc: flash(str(exc), "error")
        else:
            try:
                data = client.get(keys)
                context()
                token = snapshot(data, "edit")
            except ApiError as exc:
                flash(str(exc), "error")
                return redirect(url_for("index"))
        return render_template("form.html", data=data, editing=True, snapshot=token)

    @app.route("/delete", methods=["GET", "POST"])
    def delete():
        keys = keys_from_url()
        token = request.form.get("snapshot", "")
        if request.method == "POST":
            try:
                if set(request.form) != {"csrf", "snapshot"}: raise ValueError("Unknown form fields.")
                client.delete({**keys, "expected_version": version_from_form(keys, "delete")})
                flash("Record deleted successfully.", "success")
                return redirect(url_for("index"))
            except (ApiError, ValueError) as exc: flash(str(exc), "error")
        else:
            try:
                record = client.get(keys)
                context()
                token = snapshot(record, "delete")
            except ApiError as exc:
                flash(str(exc), "error")
                return redirect(url_for("index"))
        return render_template("delete.html", keys=keys, snapshot=token)

    return app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("demo", "lambda"), default="demo")
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    backend = DemoClient() if args.mode == "demo" else LambdaClient(args.region)
    create_app(backend, args.mode).run(host="127.0.0.1", port=5000, debug=False, load_dotenv=False)
