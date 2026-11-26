"""Lambda ClimateApiV2 (opción A): la puerta pública, detrás de API Gateway.

    GET  /health  -> {"status": "ok"}
    POST /rpc     -> {"metodo": ..., "token": ..., "args": {...}}

- Lecturas (crear, leer, editar, borrar): primero confirma el JWT con ClimateDbV2 y luego invoca las
  4 Lambdas de siempre (ClimateCreate/Read/Update/DeleteReadingV2), que escriben en DynamoDB.
- Idempotency-Key: confirma el JWT y usa la tabla ClimateIdempotency.
- Todo lo de MySQL y PostgreSQL se pasa tal cual a ClimateDbV2, que revisa el token y los permisos.

No está en la VPC: así llega a DynamoDB y a las otras Lambdas sin endpoints extra. No guarda
contraseñas de las bases: esas solo las tiene ClimateDbV2.
"""
import base64
import json
import os

from services.rpc_json import a_json, de_json, error_a_json

LECTURAS = {"lectura"}
IDEMPOTENCIA = {"idem_begin", "idem_finish", "idem_abandon"}
OPERACIONES = {"read", "create", "update", "delete"}
MAX_CUERPO = 400_000


def http(status, cuerpo):
    return {"statusCode": status, "headers": {"content-type": "application/json; charset=utf-8",
                                              "cache-control": "no-store"},
            "body": json.dumps(cuerpo, ensure_ascii=False)}


class Puerta:
    def __init__(self, invocar_db, lecturas, idempotencia):
        self.invocar_db = invocar_db          # función(dict) -> dict  (ClimateDbV2)
        self.lecturas = lecturas              # LambdaClient: las 4 Lambdas de lecturas
        self.idempotencia = idempotencia      # función() -> DynamoIdempotency

    def atender_http(self, event):
        contexto = event.get("requestContext") or {}
        metodo = (contexto.get("http") or {}).get("method") or event.get("httpMethod") or ""
        ruta = event.get("rawPath") or event.get("path") or "/"
        if metodo == "GET" and ruta.rstrip("/").endswith("/health"):
            return http(200, {"status": "ok", "servicio": "ClimateApiV2"})
        if metodo != "POST" or not ruta.rstrip("/").endswith("/rpc"):
            return http(404, {"error": {"tipo": "ValueError", "mensaje": "Ruta no encontrada. Usa POST /rpc."}})
        cuerpo = event.get("body") or ""
        if len(cuerpo) > MAX_CUERPO:
            return http(413, {"error": {"tipo": "ValueError", "mensaje": "La petición es demasiado grande."}})
        try:
            if event.get("isBase64Encoded"):
                cuerpo = base64.b64decode(cuerpo).decode("utf-8")
            peticion = json.loads(cuerpo)
            if not isinstance(peticion, dict):
                raise ValueError
        except ValueError:
            return http(400, {"error": {"tipo": "ValueError", "mensaje": "El cuerpo debe ser JSON."}})
        return http(200, self.atender(peticion))

    def atender(self, peticion):
        metodo = str(peticion.get("metodo", ""))
        if metodo not in LECTURAS | IDEMPOTENCIA:
            return self.invocar_db(peticion)          # MySQL y PostgreSQL: decide ClimateDbV2
        try:
            respuesta = self.invocar_db({"metodo": "autorizar", "token": peticion.get("token") or ""})
            if "error" in respuesta:
                return respuesta
            admin = de_json(respuesta["ok"])
            args = de_json(peticion.get("args") or {})
            if metodo == "lectura":
                operacion = args.get("operation")
                if operacion not in OPERACIONES or not isinstance(args.get("data"), dict):
                    raise ValueError("Operación de lectura inválida.")
                return {"ok": a_json(self.lecturas.call(operacion, args["data"]))}
            alcance = str(args.get("scope", ""))
            if not alcance.startswith(f"{admin['user_id']}#"):
                raise ValueError("Esa Idempotency-Key pertenece a otro usuario.")
            tabla = self.idempotencia()
            if metodo == "idem_begin":
                return {"ok": a_json(tabla.begin(alcance, str(args["print_"])))}
            if metodo == "idem_finish":
                tabla.finish(alcance, int(args["status"]), args["body"])
            else:
                tabla.abandon(alcance)
            return {"ok": None}
        except Exception as exc:   # noqa: BLE001
            if type(exc).__name__ not in ("ValueError", "ApiError", "IdempotencyError"):
                print("ERROR", type(exc).__name__, str(exc)[:500])
            return {"error": error_a_json(exc)}


# ------------------------------------------------------------------ Lambda
_puerta = None


def puerta():
    global _puerta
    if _puerta is None:
        import boto3
        from botocore.config import Config
        from services.endpoints import LambdaClient
        region = os.environ.get("AWS_REGION", "us-east-1")
        sdk = boto3.client("lambda", region_name=region,
                           config=Config(connect_timeout=5, read_timeout=28, retries={"total_max_attempts": 1}))
        funcion_db = os.environ.get("CLIMA_DB_FUNCTION", "ClimateDbV2")

        def invocar_db(peticion):
            try:
                r = sdk.invoke(FunctionName=funcion_db, InvocationType="RequestResponse",
                               Payload=json.dumps(peticion).encode("utf-8"))
                datos = r["Payload"].read()
            except Exception as exc:   # noqa: BLE001
                print("No se pudo invocar", funcion_db, type(exc).__name__, str(exc)[:300])
                return {"error": {"tipo": "DatabaseUnavailable",
                                  "mensaje": "No se pudo contactar el servicio de datos (ClimateDbV2)."}}
            if r.get("FunctionError"):
                print(funcion_db, "falló:", datos[:800])
                return {"error": {"tipo": "DatabaseUnavailable",
                                  "mensaje": "El servicio de datos (ClimateDbV2) falló. Revisa sus logs en CloudWatch."}}
            return json.loads(datos)

        tabla = {}

        def idempotencia():
            if "t" not in tabla:
                from services.idempotency import DynamoIdempotency
                tabla["t"] = DynamoIdempotency(os.environ.get("CLIMA_IDEMPOTENCY_TABLE", "ClimateIdempotency"), region)
            return tabla["t"]

        _puerta = Puerta(invocar_db, LambdaClient(region, sdk=sdk), idempotencia)
    return _puerta


def handler(event, context):
    return puerta().atender_http(event)
