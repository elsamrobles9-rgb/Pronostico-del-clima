"""REST API for Postman and other external clients.

Public: GET /api/health, GET /api/pronostico (Mexico City forecast), GET /api/registros (hourly readings
of one day, read only) and POST /api/auth/login.
Everything else needs "Authorization: Bearer <JWT>" of an ADMIN or SUPERADMIN, and each operation checks the
permission matrix of services/roles.py (create/update: ADMIN and SUPERADMIN; delete: SUPERADMIN only).
POST and PUT need an "Idempotency-Key" header. Updates and deletes need the record version (If-Match header or
expected_version) for optimistic concurrency.
"""
import re
import uuid
from functools import wraps

from flask import Blueprint, Response, current_app, g, request

from contract import REQUIRED, dumps, load_json, normalize_record
from services import roles
from services.catalog import check_record, fill_station, public_catalog, resolve_condition
from services.endpoints import ApiError
from services.idempotency import IdempotencyError, check_key, fingerprint
from services.mysql_store import DatabaseUnavailable

bp = Blueprint("api", __name__, url_prefix="/api")
PUBLIC = {"api.health", "api.login", "api.pronostico", "api.registros"}
KEYS = ("location_id", "timestamp_utc")


def ext(name):
    return current_app.extensions[name]


def reply(status, body, headers=None):
    response = Response(dumps(body), status=status, mimetype="application/json")
    response.headers["X-Request-Id"] = g.get("request_id", "")
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def problem(status, code, message, headers=None):
    return reply(status, {"error": code, "message": message}, headers)


def unauthorized(message):
    return problem(401, "unauthorized", message, {"WWW-Authenticate": 'Bearer realm="climate-api"'})


@bp.before_request
def authenticate():
    supplied = request.headers.get("X-Request-Id", "")
    g.request_id = supplied if re.fullmatch(r"[A-Za-z0-9_.:-]{8,100}", supplied) else uuid.uuid4().hex
    g.user = None
    if request.endpoint in PUBLIC:
        return None
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return unauthorized("Send the header Authorization: Bearer <token>. Get a token from POST /api/auth/login.")
    g.token = header[7:].strip()
    try:
        g.user = ext("auth_store").user(g.token)
    except DatabaseUnavailable as exc:
        return problem(503, "database_unavailable", str(exc))
    if not g.user:
        return unauthorized("The token is invalid, expired, revoked or was idle for 30 minutes. Sign in again.")
    return None


@bp.errorhandler(404)
@bp.errorhandler(405)
def http_error(exc):
    return problem(exc.code, "not_found" if exc.code == 404 else "method_not_allowed", exc.description)


def requiere(accion):
    """The server decides with the permission matrix; hiding a button is not enough."""
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not roles.puede(g.user, accion):
                audit("ACCESS_DENIED", "ENDPOINT", request.method + " " + request.path, "DENIED",
                      reason=f"role {g.user.get('role')} cannot {accion}")
                return problem(403, "forbidden", "Your role cannot use this operation.")
            return view(*args, **kwargs)
        return wrapper
    return decorator


def audit(action, entity_type, entity_key, outcome, before=None, after=None, reason=None):
    try:
        ext("auth_store").audit(g.user, action, entity_type, entity_key, outcome, before, after, reason, g.request_id)
    except Exception as exc:   # auditing must not hide the result of an operation that already happened
        current_app.logger.warning("Audit event not saved: %s", type(exc).__name__)


def json_body():
    raw = request.get_data(cache=True, as_text=True)
    if len(raw) > 65536:
        raise ValueError("The JSON body is too large.")
    try:
        data = load_json(raw or "null")
    except ValueError as exc:
        raise ValueError("Send a valid JSON object. " + str(exc)) from None
    if not isinstance(data, dict):
        raise ValueError("Send a JSON object.")
    return data


def path_keys(location_id, timestamp_utc):
    from services.reading_service import keys as validate_keys
    try:
        return validate_keys({"location_id": location_id, "timestamp_utc": timestamp_utc})
    except Exception:
        raise ValueError("Invalid location_id or timestamp_utc in the URL.") from None


def version_from_request(body):
    from_body = body.pop("expected_version", None)
    raw = request.headers.get("If-Match", "").strip().strip('"') or from_body or request.args.get("expected_version")
    if raw is None or raw == "":
        return None
    try:
        version = int(str(raw))
    except ValueError:
        raise ValueError("If-Match / expected_version must be the record version number.") from None
    if version < 1:
        raise ValueError("If-Match / expected_version must be a positive integer.")
    return version


def prepare(body):
    catalog = ext("auth_store").catalog()
    body = fill_station(dict(body), catalog)
    record = normalize_record(body)
    record.pop("record_id", None)   # the backend generates the UUID v4
    return check_record(resolve_condition(record), catalog)


def safe_get(keys):
    try:
        return ext("climate_client").get(keys)
    except ApiError:
        return None


def idempotent(handler):
    """Run handler once per Idempotency-Key; replay the stored response on retries."""
    store = ext("idempotency")
    try:
        key = check_key(request.headers.get("Idempotency-Key", "").strip())
    except IdempotencyError as exc:
        return problem(exc.status, exc.code, exc.message)
    scope = f"{g.user['user_id']}#{key}"
    print_ = fingerprint(request.method, request.path, request.get_data(cache=True),
                         request.headers.get("If-Match", ""))
    try:
        saved = store.begin(scope, print_)
    except IdempotencyError as exc:
        return problem(exc.status, exc.code, exc.message)
    except Exception as exc:
        current_app.logger.warning("Idempotency store unavailable: %s", type(exc).__name__)
        return problem(503, "idempotency_unavailable", "Cannot reserve the Idempotency-Key. Check the AWS session.")
    if saved:
        status, body = saved
        return reply(status, body, {"Idempotent-Replayed": "true"})
    try:
        status, body = handler()
    except ApiError as exc:
        if exc.status < 500:
            status, body = exc.status, {"error": "rejected", "message": str(exc)}
        else:   # unknown outcome: free the key so the client can retry safely
            store.abandon(scope)
            return problem(502, "backend_unavailable", str(exc))
    except Exception:
        store.abandon(scope)
        raise
    store.finish(scope, status, body)
    return reply(status, body, {"Idempotent-Replayed": "false"})


# ---------------------------------------------------------------------------- public
@bp.get("/health")
def health():
    return reply(200, {"status": "ok", "mode": current_app.config.get("CLIMATE_MODE", "demo"),
                       "users": ext("auth_store").kind, "idempotency": ext("idempotency").description})


@bp.get("/pronostico")
def pronostico():
    """The same data as the public page, as JSON (no sign-in)."""
    from datetime import date
    try:
        elegido = date.fromisoformat(request.args.get("dia", ""))
    except ValueError:
        elegido = None
    try:
        datos = ext("analytics").pagina(elegido)
    except Exception:
        return problem(503, "forecast_unavailable", "The forecast is not available right now.")
    import json
    datos = json.loads(json.dumps(datos, default=str))   # fechas -> texto ISO
    return reply(200, {**datos, "ubicacion": "LOC-CDMX", "aviso": "Statistical forecast by this system"})


@bp.get("/registros")
def registros():
    """Hourly readings of one Mexico City day, read only and without sign-in (same data as /registros)."""
    from datetime import date
    texto = request.args.get("dia", "").strip()
    try:
        elegido = date.fromisoformat(texto) if texto else None
    except ValueError:
        return problem(400, "invalid_date", "Send dia=YYYY-MM-DD.")
    try:
        datos = ext("analytics").lecturas_dia(elegido)
    except Exception:
        return problem(503, "records_unavailable", "The records are not available right now.")
    import json
    datos = json.loads(json.dumps(datos, default=str))   # fechas -> texto ISO
    return reply(200, {**datos, "ubicacion": "LOC-CDMX"})


@bp.post("/auth/login")
def login():
    try:
        data = json_body()
    except ValueError as exc:
        return problem(400, "invalid_body", str(exc))
    user, password = data.get("email") or data.get("username"), data.get("password")
    if not isinstance(user, str) or not isinstance(password, str):
        return problem(400, "invalid_body", 'Send {"email": "...", "password": "..."}.')
    try:
        store = ext("auth_store")
        token = store.login(user, password)
    except DatabaseUnavailable as exc:
        return problem(503, "database_unavailable", str(exc))
    if not token:
        return unauthorized("Sign-in failed. Check your details or try again in 15 minutes.")
    claims = store.signer.verify(token)
    return reply(200, {"access_token": token, "token_type": "Bearer", "expires_in": store.signer.ttl,
                       "idle_timeout_seconds": 1800, "role": claims.get("role"), "email": claims.get("email")})


# ---------------------------------------------------------------------------- session
@bp.post("/auth/logout")
def logout():
    ext("auth_store").logout(g.token)
    return reply(200, {"message": "Signed out. This token no longer works."})


@bp.get("/me")
def me():
    return reply(200, {k: g.user[k] for k in ("user_id", "username", "display_name", "role")})


@bp.get("/catalog")
def catalog():
    return reply(200, public_catalog(ext("auth_store").catalog()))


# ---------------------------------------------------------------------------- readings
@bp.get("/readings")
def list_readings():
    try:
        rows, cursor = ext("climate_client").list_records(request.args.get("location_id", "").strip(),
                                                         request.args.get("cursor", ""))
    except ApiError as exc:
        return problem(exc.status if exc.status < 500 else 502, "rejected", str(exc))
    return reply(200, {"items": rows, "next_cursor": cursor, "count": len(rows)})


@bp.get("/readings/<location_id>/<timestamp_utc>")
def get_reading(location_id, timestamp_utc):
    try:
        record = ext("climate_client").get(path_keys(location_id, timestamp_utc))
    except ValueError as exc:
        return problem(400, "invalid_request", str(exc))
    except ApiError as exc:
        return problem(exc.status if exc.status < 500 else 502, "rejected", str(exc))
    return reply(200, record, {"ETag": f'"{record.get("version")}"'})


@bp.post("/readings")
@requiere("agregar")
def create_reading():
    def handler():
        try:
            record = prepare(json_body())
        except ValueError as exc:
            audit("READING_CREATE", "READING", "-", "REJECTED", reason=str(exc))
            return 400, {"error": "invalid_record", "message": str(exc)}
        entity = record["location_id"] + "|" + record["timestamp_utc"]
        try:
            result = ext("climate_client").create(record)
        except ApiError as exc:
            if exc.status < 500:
                audit("READING_CREATE", "READING", entity, "REJECTED", after=record, reason=str(exc))
            raise
        audit("READING_CREATE", "READING", entity, "SUCCESS", after=result.get("record"))
        if result.get("record"):
            result = {**result, "postgres": ext("analytics_sync")([result["record"]], "api", quiet=True)}
        return 201, result
    return idempotent(handler)


@bp.put("/readings/<location_id>/<timestamp_utc>")
@requiere("editar")
def update_reading(location_id, timestamp_utc):
    def handler():
        try:
            keys = path_keys(location_id, timestamp_utc)
            body = json_body()
            version = version_from_request(body)
            if version is None:
                return 428, {"error": "version_required",
                             "message": "Send If-Match: \"<version>\" or expected_version (GET the record first)."}
            if any(k in body and body[k] != keys[k] for k in KEYS):
                raise ValueError("location_id and timestamp_utc cannot be changed.")
        except ValueError as exc:
            return 400, {"error": "invalid_record", "message": str(exc)}
        before = safe_get(keys)
        if before is None:
            return 404, {"error": "not_found", "message": "Record not found."}
        try:
            # Fields not sent keep their current value, so a PUT may carry only the changes.
            current = {k: v for k, v in before.items() if k in REQUIRED}
            record = prepare({**current, **body, **keys})
        except ValueError as exc:
            return 400, {"error": "invalid_record", "message": str(exc)}
        entity = keys["location_id"] + "|" + keys["timestamp_utc"]
        try:
            result = ext("climate_client").update({**record, "expected_version": version})
        except ApiError as exc:
            if exc.status < 500:
                audit("READING_UPDATE", "READING", entity, "REJECTED", before=before, after=record, reason=str(exc))
            raise
        audit("READING_UPDATE", "READING", entity, "SUCCESS", before=before, after=result.get("record"))
        if result.get("record"):
            result = {**result, "postgres": ext("analytics_sync")([result["record"]], "api", quiet=True)}
        return 200, result
    return idempotent(handler)


@bp.delete("/readings/<location_id>/<timestamp_utc>")
@requiere("eliminar")
def delete_reading(location_id, timestamp_utc):
    def handler():
        try:
            keys = path_keys(location_id, timestamp_utc)
            version = version_from_request({})
        except ValueError as exc:
            return 400, {"error": "invalid_request", "message": str(exc)}
        if version is None:
            return 428, {"error": "version_required", "message": "Send If-Match: \"<version>\" or ?expected_version=."}
        entity = keys["location_id"] + "|" + keys["timestamp_utc"]
        try:
            result = ext("climate_client").delete({**keys, "expected_version": version})
        except ApiError as exc:
            if exc.status < 500:
                audit("READING_DELETE", "READING", entity, "REJECTED", reason=str(exc))
            raise
        audit("READING_DELETE", "READING", entity, "SUCCESS", before=result.get("record"))
        return 200, result
    if request.headers.get("Idempotency-Key"):
        return idempotent(handler)
    try:
        status, body = handler()
    except ApiError as exc:
        return problem(exc.status if exc.status < 500 else 502, "rejected", str(exc))
    return reply(status, body)
