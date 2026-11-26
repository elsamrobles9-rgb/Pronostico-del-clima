"""Lambda ClimateDbV2 (opción A): la ÚNICA pieza que habla con MySQL y PostgreSQL.

Vive dentro de la red (VPC) de las RDS, así que las bases no necesitan estar abiertas a internet.
No la llama nadie de fuera: solo la Lambda ClimateApiV2, con lambda:InvokeFunction.

Recibe {"metodo": ..., "token": ..., "args": {...}} y responde {"ok": ...} o {"error": {...}}.
Los métodos de administración exigen un JWT de ADMIN con sesión vigente en MySQL; el actor que se
guarda en la auditoría sale SIEMPRE del token, nunca de lo que mande el portal.
"""
import os
import uuid

from services.rpc_json import a_json, de_json, error_a_json


class NoAutorizado(Exception):
    pass


PUBLICOS = {"estado", "login", "user", "logout", "catalog", "pagina"}


class ServicioDatos:
    def __init__(self, store, analytics):
        self.store, self.analytics = store, analytics

    # ------------------------------------------------------------------ entrada
    def atender(self, peticion):
        try:
            peticion = de_json(peticion or {})
            metodo = str(peticion.get("metodo", ""))
            args = peticion.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("Petición inválida.")
            funcion = getattr(self, "m_" + metodo, None)
            if funcion is None:
                raise ValueError("Método desconocido.")
            admin = None
            if metodo not in PUBLICOS:
                admin = self.store.user(str(peticion.get("token") or ""))
                if not admin:
                    raise NoAutorizado("Tu sesión ya no es válida. Vuelve a iniciar sesión.")
            return {"ok": a_json(funcion(admin, **args))}
        except TypeError as exc:
            print("ERROR TypeError", str(exc)[:300])
            return {"error": {"tipo": "ValueError", "mensaje": "Parámetros inválidos."}}
        except Exception as exc:   # noqa: BLE001 - se responde un error seguro, sin detalles internos
            causa = exc.__cause__
            if causa is not None:   # p. ej. contraseña incorrecta o red: queda en CloudWatch
                print("CAUSA", type(exc).__name__, "<-", type(causa).__name__, str(causa)[:400])
            elif type(exc).__name__ not in ("ValueError", "NoAutorizado", "ApiError"):
                print("ERROR", type(exc).__name__, str(exc)[:500])
            error = error_a_json(exc)
            if causa is not None and type(exc).__name__ == "DatabaseUnavailable":
                error["mensaje"] = ("No se pudo conectar a MySQL o PostgreSQL desde ClimateDbV2 "
                                    f"({type(causa).__name__}). Revisa usuario, contraseña y el security group.")
            return {"error": error}

    # ------------------------------------------------------------------ públicos
    def m_estado(self, admin):
        """Prueba de conexión: cuántos administradores hay en MySQL y cuántas lecturas en PostgreSQL."""
        db = getattr(self.analytics, "db", None)
        if db is None:
            lecturas = len(self.analytics.historial())
        else:
            with db.tx() as cur:
                cur.execute("SELECT COUNT(*) FROM climate_analytics.observations WHERE deleted_at_utc IS NULL")
                lecturas = int(cur.fetchone()[0])
        return {"mysql_admins": self.store.count_admins(), "postgres_lecturas": lecturas}

    def m_login(self, admin, email, password):
        return self.store.login(email, password)

    def m_user(self, admin, token):
        return self.store.user(token)

    def m_logout(self, admin, token):
        return self.store.logout(token)

    def m_catalog(self, admin, fresh=False):
        return self.store.catalog(fresh=bool(fresh))

    def m_pagina(self, admin, dia=None):
        return self.analytics.pagina(dia)

    # ------------------------------------------------------------------ administración (token ADMIN)
    def m_save_admin(self, admin, email, display_name, password, request_id=None):
        return self.store.save_admin(email, display_name, password, actor=admin, request_id=request_id)

    def m_change_own_password(self, admin, current, new):
        return self.store.change_own_password(admin, current, new)

    def m_disable(self, admin, email):
        if email == admin["username"]:
            raise ValueError("You cannot change your own account status.")
        return self.store.disable(email, actor=admin)

    def m_enable(self, admin, email):
        return self.store.enable(email, actor=admin)

    def m_list_users(self, admin):
        return self.store.list_users()

    def m_count_admins(self, admin):
        return self.store.count_admins()

    def m_audit(self, admin, action, entity_type, entity_key, outcome, before=None, after=None, reason=None,
                request_id=None):
        return self.store.audit(admin, action, entity_type, entity_key, outcome, before, after, reason, request_id)

    def m_audit_log(self, admin, limit=100):
        return self.store.audit_log(min(int(limit), 500))

    def m_rotate_audit(self, admin, days, request_id=None):
        return self.store.rotate_audit(days, admin, request_id or uuid.uuid4().hex)

    def m_history_count(self, admin):
        return self.store.history_count()

    def m_upsert(self, admin, records, origen, fuentes, condiciones):
        if not isinstance(records, list) or not 1 <= len(records) <= 500:
            raise ValueError("Envía de 1 a 500 lecturas.")
        return self.analytics.upsert(records, origen, fuentes, condiciones)

    def m_autorizar(self, admin):
        """Para ClimateApiV2: confirma el token antes de tocar DynamoDB."""
        return admin


# ------------------------------------------------------------------ Lambda
_servicio = None


def servicio():
    """Se crea una vez por contenedor y reutiliza las conexiones entre invocaciones."""
    global _servicio
    if _servicio is None:
        from services.analytics import PostgresAnalytics, pg_factory
        from services.jwt_tokens import TokenSigner
        from services.mysql_store import MySQLAccessStore, mysql_factory
        env = os.environ
        if not env.get("CLIMA_JWT_SECRET"):
            raise RuntimeError("Falta CLIMA_JWT_SECRET en la configuración de la Lambda.")
        store = MySQLAccessStore(mysql_factory(env["CLIMA_MYSQL_HOST"], env.get("CLIMA_MYSQL_PORT", "3306"),
                                               env["CLIMA_MYSQL_USER"], env["CLIMA_MYSQL_PASSWORD"],
                                               env.get("CLIMA_MYSQL_DB", "climate_transactional")),
                                 TokenSigner())
        analytics = PostgresAnalytics(pg_factory(env["CLIMA_PG_HOST"], env.get("CLIMA_PG_PORT", "5432"),
                                                 env["CLIMA_PG_USER"], env["CLIMA_PG_PASSWORD"],
                                                 env.get("CLIMA_PG_DB", "climate_analytics")))
        _servicio = ServicioDatos(store, analytics)
    return _servicio


def handler(event, context):
    try:
        datos = servicio()
    except Exception as exc:   # noqa: BLE001 - el error de arranque regresa al que llamó (y queda en CloudWatch)
        import traceback
        traceback.print_exc()
        return {"error": {"tipo": "DatabaseUnavailable",
                          "mensaje": f"ClimateDbV2 no pudo iniciar: {type(exc).__name__}: {str(exc)[:300]}"}}
    return datos.atender(event)
