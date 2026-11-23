"""Modo API (opción A): el portal habla con AWS solo por HTTPS, a través de API Gateway.

Sirve igual en Vercel que en tu PC. No necesita credenciales de AWS ni contraseñas de las bases:
solo la URL de la API (CLIMA_API_URL). Cada clase imita a la que usa el modo lambda, así que
app.py, la API REST y las plantillas no cambian:

    RemoteStore        -> MySQLAccessStore (usuarios, sesiones, auditoría, catálogos)
    RemoteAnalytics    -> PostgresAnalytics (página pública y sp_upsert_lecturas)
    RemoteClient       -> LambdaClient (las 4 Lambdas de lecturas)
    RemoteIdempotency  -> DynamoIdempotency (tabla ClimateIdempotency)
"""
import base64
import json
import urllib.error
import urllib.request

from services.endpoints import ApiError, Client
from services.idempotency import IdempotencyError
from services.mysql_store import DatabaseUnavailable
from services.rpc_json import a_json, de_json


class SesionVencida(ValueError):
    pass


class ApiRemota:
    def __init__(self, url, timeout=29, transporte=None):
        url = (url or "").strip().rstrip("/")
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            raise ValueError("CLIMA_API_URL debe ser la URL https de API Gateway (termina en .amazonaws.com).")
        self.url, self.timeout = url, timeout
        self.transporte = transporte or self.enviar

    def enviar(self, ruta, cuerpo):
        peticion = urllib.request.Request(self.url + ruta, data=cuerpo, method="POST" if cuerpo else "GET",
                                          headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(peticion, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def llamar(self, metodo, sesion="", /, **args):
        """sesion = JWT de quien pide (solo posicional, para que "token" pueda ir dentro de args)."""
        cuerpo = json.dumps({"metodo": metodo, "token": sesion or "", "args": a_json(args)}).encode("utf-8")
        try:
            status, datos = self.transporte("/rpc", cuerpo)
            respuesta = json.loads(datos)
        except (OSError, ValueError):
            raise DatabaseUnavailable("Could not reach the AWS API (API Gateway). "
                                      "Check that the lab is running.") from None
        if not isinstance(respuesta, dict):
            raise DatabaseUnavailable("Invalid response from the AWS API.")
        if "error" in respuesta:
            error = respuesta["error"] or {}
            tipo, mensaje = error.get("tipo"), error.get("mensaje") or "API error."
            if tipo == "ValueError":
                raise ValueError(mensaje)
            if tipo == "NoAutorizado":
                raise SesionVencida(mensaje)
            if tipo == "ApiError":
                raise ApiError(mensaje, int(error.get("status") or 502))
            if tipo == "IdempotencyError":
                raise IdempotencyError(int(error.get("status") or 409), error.get("codigo") or "idempotency", mensaje)
            raise DatabaseUnavailable(mensaje)
        if status >= 500:
            raise DatabaseUnavailable("The AWS API answered with an error.")
        return de_json(respuesta.get("ok"))

    def salud(self):
        status, datos = self.transporte("/health", None)
        return status == 200 and json.loads(datos).get("status") == "ok"


def token_actual():
    """El JWT de quien hace la petición: Authorization (API) o la sesión del navegador (web)."""
    from flask import g, has_request_context, session
    if not has_request_context():
        return ""
    return g.get("token") or session.get("sid", "")


class RemoteSigner:
    """El secreto del JWT vive solo en ClimateDbV2. Aquí solo se leen los datos del token
    (para la respuesta de /api/auth/login); quien decide si un token vale es siempre ClimateDbV2."""
    ttl = 8 * 3600

    def issue(self, claims):
        raise RuntimeError("En modo API los tokens los emite ClimateDbV2.")

    def verify(self, token, check_expiry=True):
        try:
            parte = token.split(".")[1]
            return json.loads(base64.urlsafe_b64decode(parte + "=" * (-len(parte) % 4)))
        except (IndexError, ValueError):
            from services.jwt_tokens import TokenError
            raise TokenError("Invalid token.") from None


class RemoteStore:
    kind = "mysql (API)"

    def __init__(self, api):
        self.api = api
        self.signer = RemoteSigner()

    # públicos
    def login(self, email, password):
        return self.api.llamar("login", email=email, password=password)

    def user(self, token):
        if not token:
            return None
        return self.api.llamar("user", token=token)

    def logout(self, token):
        if token:
            self.api.llamar("logout", token=token)

    def catalog(self, fresh=False):
        return self.api.llamar("catalog", fresh=fresh)

    # administración: el actor lo decide ClimateDbV2 a partir del token, no lo que se mande aquí
    def save_admin(self, email, display_name, password, actor=None, request_id=None):
        return self.api.llamar("save_admin", token_actual(), email=email, display_name=display_name,
                               password=password, request_id=request_id)

    def change_own_password(self, user, current, new):
        return self.api.llamar("change_own_password", token_actual(), current=current, new=new)

    def disable(self, email, actor=None):
        return self.api.llamar("disable", token_actual(), email=email)

    def enable(self, email, actor=None):
        return self.api.llamar("enable", token_actual(), email=email)

    def list_users(self):
        return self.api.llamar("list_users", token_actual())

    def count_admins(self):
        return self.api.llamar("count_admins", token_actual())

    def audit(self, actor, action, entity_type, entity_key, outcome, before=None, after=None, reason=None,
              request_id=None):
        token = token_actual()
        if not token:
            return None        # sin sesión no hay actor (los intentos de login los audita ClimateDbV2)
        return self.api.llamar("audit", token, action=action, entity_type=entity_type, entity_key=entity_key,
                               outcome=outcome, before=before, after=after, reason=reason, request_id=request_id)

    def audit_log(self, limit=100):
        return self.api.llamar("audit_log", token_actual(), limit=limit)

    def rotate_audit(self, days, actor=None, request_id=None):
        return tuple(self.api.llamar("rotate_audit", token_actual(), days=days, request_id=request_id))

    def history_count(self):
        try:
            return self.api.llamar("history_count", token_actual())
        except Exception:
            return None


class RemoteAnalytics:
    kind = "postgres (API)"
    datos_reales = False

    def __init__(self, api):
        self.api = api

    def pagina(self, dia_elegido=None, ahora_local=None):
        return self.api.llamar("pagina", dia=dia_elegido)

    def upsert(self, records, origen, fuentes, condiciones):
        return self.api.llamar("upsert", token_actual(), records=list(records), origen=origen,
                               fuentes=list(fuentes), condiciones=list(condiciones))

    def invalidar(self):
        pass


class RemoteClient(Client):
    """Las 4 Lambdas de lecturas, a través de ClimateApiV2."""

    def __init__(self, api):
        self.api = api

    def call(self, operation, data):
        try:
            return self.api.llamar("lectura", token_actual(), operation=operation, data=data)
        except SesionVencida as exc:
            raise ApiError(str(exc), 401) from None
        except ValueError as exc:
            raise ApiError(str(exc), 400) from None
        except DatabaseUnavailable as exc:
            raise ApiError(str(exc), 503) from None


class RemoteIdempotency:
    description = "DynamoDB ClimateIdempotency (por la API de AWS)"

    def __init__(self, api):
        self.api = api

    def begin(self, scope, print_):
        guardado = self.api.llamar("idem_begin", token_actual(), scope=scope, print_=print_)
        return tuple(guardado) if guardado else None

    def finish(self, scope, status, body):
        self.api.llamar("idem_finish", token_actual(), scope=scope, status=status, body=body)

    def abandon(self, scope):
        self.api.llamar("idem_abandon", token_actual(), scope=scope)
