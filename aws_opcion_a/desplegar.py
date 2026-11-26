"""Despliega la opción A en TU cuenta del Learner Lab. Se corre en tu PC, con el venv del proyecto:

    .\\.venv\\Scripts\\python.exe aws_opcion_a\\desplegar.py              (primera vez: crea todo)
    .\\.venv\\Scripts\\python.exe aws_opcion_a\\desplegar.py --solo-codigo (después: solo sube código nuevo)
    .\\.venv\\Scripts\\python.exe aws_opcion_a\\desplegar.py --probar      (solo prueba la API)
    .\\.venv\\Scripts\\python.exe aws_opcion_a\\desplegar.py --logs        (últimos mensajes de las Lambdas)

Qué crea (nombres nuevos, no duplica nada de lo que ya existe):
  - Security group  climate-lambda-db-sg   (en la VPC de las RDS, sin reglas de entrada)
  - Lambda          ClimateDbV2            (dentro de la VPC; la única que habla con MySQL y PostgreSQL)
  - Lambda          ClimateApiV2           (fuera de la VPC; la puerta pública)
  - HTTP API        climate-portal-api-v2  (API Gateway → ClimateApiV2)
Qué cambia de lo que ya existe (solo AGREGA, no borra):
  - Security group de las RDS: entrada 3306 y 5432 SOLO desde climate-lambda-db-sg
  - ClimateDbV2 y las 4 Lambdas de lecturas: permiso para que LabRole (ClimateApiV2) las invoque
Las contraseñas de las bases se teclean aquí (no se muestran) y quedan solo en la configuración de
ClimateDbV2. Nada de esto se guarda en archivos ni en GitHub.
"""
import argparse
import getpass
import io
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import zipfile
from importlib import metadata
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
REGION = "us-east-1"
FN_DB, FN_API = "ClimateDbV2", "ClimateApiV2"
LECTURAS = [f"Climate{n}ReadingV2" for n in ("Create", "Read", "Update", "Delete")]
API_NOMBRE = "climate-portal-api-v2"
SG_NOMBRE = "climate-lambda-db-sg"
RDS_MYSQL, RDS_PG = "climate-mysql", "climate-postgres"
PAQUETES = ["PyMySQL", "pg8000", "Werkzeug"]           # y sus dependencias (se buscan solas)
CARPETAS = ["backend", "services", "prediccion"]
ZONA_SIN_LAMBDA = {"use1-az3"}                          # Lambda no acepta subredes de esa zona
SALIDA = Path(__file__).resolve().parent / "salida.json"


def paso(texto):
    print(f"\n=== {texto}")


def fallar(texto):
    print(f"\nERROR: {texto}")
    sys.exit(1)


# ------------------------------------------------------------------ paquete .zip de las Lambdas
def distribuciones():
    vistas, pendientes = {}, [(p, False) for p in PAQUETES]
    while pendientes:
        nombre, opcional = pendientes.pop()
        clave = nombre.lower().replace("_", "-")
        if clave in vistas:
            continue
        try:
            dist = metadata.distribution(nombre)
        except metadata.PackageNotFoundError:
            if opcional:
                continue
            fallar(f"Falta el paquete {nombre} en el venv. Instálalo con:\n"
                   f"   .\\.venv\\Scripts\\python.exe -m pip install -r requirements_aws.txt")
        vistas[clave] = dist
        for req in dist.requires or []:
            if "extra ==" in req or "extra==" in req:
                continue
            nombre_req = re.split(r"[\s;<>=!~\[(]", req.strip(), maxsplit=1)[0]
            pendientes.append((nombre_req, ";" in req))
    return list(vistas.values())


def armar_zip():
    memoria = io.BytesIO()
    usados = []
    with zipfile.ZipFile(memoria, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(RAIZ / "contract.py", "contract.py")
        for carpeta in CARPETAS:
            for archivo in sorted((RAIZ / carpeta).rglob("*.py")):
                if "__pycache__" not in archivo.parts:
                    z.write(archivo, archivo.relative_to(RAIZ).as_posix())
        for dist in distribuciones():
            usados.append(f"{dist.metadata['Name']} {dist.version}")
            for ruta in dist.files or []:
                texto = str(ruta).replace("\\", "/")
                # La carpeta .dist-info SÍ va: librerías como scramp leen su versión de ahí al importarse.
                if (texto.startswith("..") or "__pycache__" in texto
                        or texto.endswith((".pyc", ".pyd", ".so", ".dll", ".exe"))):
                    continue
                origen = Path(dist.locate_file(ruta))
                if origen.is_file():
                    z.write(origen, texto)
    datos = memoria.getvalue()
    print(f"Paquete listo: {len(datos) / 1e6:.1f} MB con {', '.join(usados)}")
    return datos


# ------------------------------------------------------------------ AWS
def aws():
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        fallar("Falta boto3. Usa el venv: .\\.venv\\Scripts\\python.exe aws_opcion_a\\desplegar.py")
    sesion = boto3.Session(region_name=REGION)
    config = Config(retries={"max_attempts": 6, "mode": "standard"}, connect_timeout=10, read_timeout=60)
    try:
        cuenta = sesion.client("sts", config=config).get_caller_identity()["Account"]
    except Exception as exc:
        fallar(f"No se pudo entrar a AWS ({type(exc).__name__}). ¿Pegaste las credenciales nuevas del lab en "
               f".aws\\credentials? Si dice ExpiredToken, cópialas otra vez de AWS Details.")
    return sesion, config, cuenta


def info_rds(rds, nombre):
    try:
        d = rds.describe_db_instances(DBInstanceIdentifier=nombre)["DBInstances"][0]
    except Exception as exc:
        fallar(f"No encontré la RDS {nombre}: {exc}")
    if d["DBInstanceStatus"] != "available":
        fallar(f"La RDS {nombre} está '{d['DBInstanceStatus']}'. Enciéndela (Actions → Start) y espera a 'Available'.")
    return {"host": d["Endpoint"]["Address"], "puerto": d["Endpoint"]["Port"], "vpc": d["DBSubnetGroup"]["VpcId"],
            "subredes": [s["SubnetIdentifier"] for s in d["DBSubnetGroup"]["Subnets"]],
            "sgs": [g["VpcSecurityGroupId"] for g in d["VpcSecurityGroups"]]}


def existe_funcion(lam, nombre):
    try:
        return lam.get_function_configuration(FunctionName=nombre)
    except lam.exceptions.ResourceNotFoundException:
        return None


def esperar(lam, nombre, creada):
    lam.get_waiter("function_active_v2" if creada else "function_updated_v2").wait(
        FunctionName=nombre, WaiterConfig={"Delay": 5, "MaxAttempts": 120})


def subir_funcion(lam, nombre, rol, handler, codigo, ambiente, timeout, memoria, vpc=None, descripcion=""):
    actual = existe_funcion(lam, nombre)
    if actual is None:
        print(f"Creando la Lambda {nombre}..." + (" (dentro de la VPC: tarda 1 a 3 minutos)" if vpc else ""))
        opciones = dict(FunctionName=nombre, Runtime=ARGS.runtime, Role=rol, Handler=handler,
                        Code={"ZipFile": codigo}, Timeout=timeout, MemorySize=memoria, Description=descripcion,
                        Environment={"Variables": ambiente}, Publish=False)
        if vpc:
            opciones["VpcConfig"] = vpc
        for intento in range(6):
            try:
                lam.create_function(**opciones)
                break
            except lam.exceptions.InvalidParameterValueException as exc:
                if "role" in str(exc).lower() and intento < 5:
                    time.sleep(10)
                    continue
                fallar(f"AWS no aceptó la Lambda {nombre}: {exc}\n"
                       "Si menciona CreateNetworkInterface, LabRole no puede usar la VPC: avísame y lo resolvemos.")
        esperar(lam, nombre, True)
    else:
        print(f"Actualizando la Lambda {nombre} (ya existía; no se duplica)...")
        lam.update_function_code(FunctionName=nombre, ZipFile=codigo)
        esperar(lam, nombre, False)
        if ambiente is not None:
            opciones = dict(FunctionName=nombre, Runtime=ARGS.runtime, Handler=handler, Timeout=timeout,
                            MemorySize=memoria, Environment={"Variables": ambiente})
            if vpc:
                opciones["VpcConfig"] = vpc
            lam.update_function_configuration(**opciones)
            esperar(lam, nombre, False)
    return lam.get_function_configuration(FunctionName=nombre)["FunctionArn"]


def permitir(lam, funcion, sid, **permiso):
    try:
        lam.add_permission(FunctionName=funcion, StatementId=sid, Action="lambda:InvokeFunction", **permiso)
        print(f"  Permiso agregado en {funcion}")
    except lam.exceptions.ResourceConflictException:
        print(f"  {funcion} ya tenía el permiso")
    except Exception as exc:
        print(f"  AVISO: no se pudo agregar el permiso en {funcion}: {type(exc).__name__}: {str(exc)[:200]}")


# ------------------------------------------------------------------ prueba
def probar(url):
    paso("Probando la API")
    ultimo = None
    for intento in range(8):
        try:
            with urllib.request.urlopen(url + "/health", timeout=30) as r:
                salud = json.loads(r.read())
            pedido = urllib.request.Request(url + "/rpc", data=json.dumps({"metodo": "estado"}).encode(),
                                            headers={"content-type": "application/json"}, method="POST")
            with urllib.request.urlopen(pedido, timeout=30) as r:
                estado = json.loads(r.read())
            if "ok" in estado:
                print(f"OK  /health: {salud}")
                print(f"OK  ClimateDbV2 llegó a las bases: {estado['ok']['mysql_admins']} administrador(es) en MySQL, "
                      f"{estado['ok']['postgres_lecturas']:,} lecturas en PostgreSQL")
                return True
            ultimo = estado.get("error")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            ultimo = f"{type(exc).__name__}: {exc}"
        print(f"  intento {intento + 1}: todavía no ({ultimo}); espero 15 s...")
        time.sleep(15)
    print(f"\nLa API no respondió bien: {ultimo}")
    print("Revisa los logs: consola → CloudWatch → Log groups → /aws/lambda/ClimateApiV2 y /aws/lambda/ClimateDbV2")
    return False


# ------------------------------------------------------------------ principal
def main():
    sesion, config, cuenta = aws()
    lam = sesion.client("lambda", config=config)
    ec2 = sesion.client("ec2", config=config)
    rds = sesion.client("rds", config=config)
    api = sesion.client("apigatewayv2", config=config)

    if ARGS.logs:
        logs = sesion.client("logs", config=config)
        for funcion in (FN_API, FN_DB):
            grupo = f"/aws/lambda/{funcion}"
            paso(f"Últimos mensajes de {funcion}")
            try:
                flujo = logs.describe_log_streams(logGroupName=grupo, orderBy="LastEventTime", descending=True,
                                                  limit=1)["logStreams"][0]["logStreamName"]
                for evento in logs.get_log_events(logGroupName=grupo, logStreamName=flujo, limit=40,
                                                  startFromHead=False)["events"]:
                    print(evento["message"].rstrip())
            except Exception as exc:
                print(f"  (sin mensajes: {type(exc).__name__})")
        return

    if ARGS.probar:
        url = json.loads(SALIDA.read_text(encoding="utf-8"))["api_url"] if SALIDA.exists() else input("URL de la API: ")
        sys.exit(0 if probar(url.rstrip("/")) else 1)

    rol = f"arn:aws:iam::{cuenta}:role/LabRole"
    paso("Revisando las RDS")
    mysql, pg = info_rds(rds, RDS_MYSQL), info_rds(rds, RDS_PG)
    if mysql["vpc"] != pg["vpc"]:
        fallar("Las 2 RDS están en VPC distintas; este script espera que estén en la misma.")
    vpc = mysql["vpc"]
    subredes = sorted(set(mysql["subredes"]) | set(pg["subredes"]))
    info = ec2.describe_subnets(SubnetIds=subredes)["Subnets"]
    subredes = [s["SubnetId"] for s in info if s.get("AvailabilityZoneId") not in ZONA_SIN_LAMBDA][:6]
    if not subredes:
        fallar("No hay subredes válidas para Lambda en la VPC de las RDS.")
    print(f"MySQL:      {mysql['host']}:{mysql['puerto']}")
    print(f"PostgreSQL: {pg['host']}:{pg['puerto']}")
    print(f"VPC {vpc} · {len(subredes)} subredes · security groups de las RDS: {', '.join(sorted(set(mysql['sgs'] + pg['sgs'])))}")

    ya_db = existe_funcion(lam, FN_DB)
    if ARGS.solo_codigo:
        if not ya_db or not existe_funcion(lam, FN_API):
            fallar("Todavía no existen las Lambdas. Corre el script sin --solo-codigo la primera vez.")
        codigo = armar_zip()
        subir_funcion(lam, FN_DB, rol, "backend.lambda_db.handler", codigo, None, 30, 512)
        subir_funcion(lam, FN_API, rol, "backend.lambda_api.handler", codigo, None, 29, 256)
        if SALIDA.exists():
            probar(json.loads(SALIDA.read_text(encoding="utf-8"))["api_url"])
        return

    paso("Esto es lo que se va a hacer")
    print(f"  CREAR (o actualizar si ya existe): security group {SG_NOMBRE}, Lambdas {FN_DB} y {FN_API}, "
          f"HTTP API {API_NOMBRE}")
    print("  AGREGAR: en el security group de las RDS, entrada 3306/5432 solo desde " + SG_NOMBRE)
    print(f"  AGREGAR: permiso de invocación (LabRole) en {FN_DB} y en {', '.join(LECTURAS)}")
    print("  No se borra ni se modifica nada más. Las RDS, DynamoDB y las 4 Lambdas siguen igual.")
    if input("\nEscribe SI para continuar: ").strip().upper() != "SI":
        sys.exit("Cancelado. No se hizo ningún cambio.")

    paso("Datos de las bases (las contraseñas no se muestran ni se guardan en archivos)")
    usuario_mysql = input("Usuario de MySQL [climateadmin]: ").strip() or "climateadmin"
    clave_mysql = getpass.getpass("Contraseña de MySQL: ")
    usuario_pg = input("Usuario de PostgreSQL [climateadmin]: ").strip() or "climateadmin"
    clave_pg = getpass.getpass("Contraseña de PostgreSQL: ")
    if not clave_mysql or not clave_pg:
        fallar("Las contraseñas no pueden ir vacías.")
    secreto_jwt = ((ya_db or {}).get("Environment", {}).get("Variables", {}).get("CLIMA_JWT_SECRET")
                   or secrets.token_urlsafe(48))

    paso("Red: security group de ClimateDbV2")
    grupos = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SG_NOMBRE]},
                                                   {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
    if grupos:
        sg = grupos[0]["GroupId"]
        print(f"{SG_NOMBRE} ya existía: {sg}")
    else:
        sg = ec2.create_security_group(GroupName=SG_NOMBRE, VpcId=vpc,
                                       Description="Lambda ClimateDbV2 (opcion A): sale hacia MySQL y PostgreSQL")["GroupId"]
        print(f"Creado {SG_NOMBRE}: {sg}")
    for grupo_rds, puerto in sorted({(g, mysql["puerto"]) for g in mysql["sgs"]} | {(g, pg["puerto"]) for g in pg["sgs"]}):
        try:
            ec2.authorize_security_group_ingress(GroupId=grupo_rds, IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": int(puerto), "ToPort": int(puerto),
                "UserIdGroupPairs": [{"GroupId": sg, "Description": "Lambda ClimateDbV2 (opcion A)"}]}])
            print(f"  Regla agregada en {grupo_rds}: puerto {puerto} desde {SG_NOMBRE}")
        except Exception as exc:
            if "Duplicate" in str(exc):
                print(f"  {grupo_rds} ya tenía la regla del puerto {puerto}")
            else:
                fallar(f"No se pudo agregar la regla en {grupo_rds}: {exc}")

    paso("Paquete de código")
    codigo = armar_zip()

    paso(f"Lambda {FN_DB} (dentro de la VPC)")
    ambiente_db = {"CLIMA_MYSQL_HOST": mysql["host"], "CLIMA_MYSQL_PORT": str(mysql["puerto"]),
                   "CLIMA_MYSQL_USER": usuario_mysql, "CLIMA_MYSQL_PASSWORD": clave_mysql,
                   "CLIMA_MYSQL_DB": "climate_transactional",
                   "CLIMA_PG_HOST": pg["host"], "CLIMA_PG_PORT": str(pg["puerto"]), "CLIMA_PG_USER": usuario_pg,
                   "CLIMA_PG_PASSWORD": clave_pg, "CLIMA_PG_DB": "climate_analytics",
                   "CLIMA_JWT_SECRET": secreto_jwt}
    vpc_config = None if ARGS.sin_vpc else {"SubnetIds": subredes, "SecurityGroupIds": [sg]}
    arn_db = subir_funcion(lam, FN_DB, rol, "backend.lambda_db.handler", codigo, ambiente_db, 30, 512, vpc_config,
                           "Opcion A: unica Lambda que habla con MySQL y PostgreSQL")

    paso(f"Lambda {FN_API} (puerta pública)")
    ambiente_api = {"CLIMA_DB_FUNCTION": FN_DB, "CLIMA_IDEMPOTENCY_TABLE": "ClimateIdempotency"}
    arn_api = subir_funcion(lam, FN_API, rol, "backend.lambda_api.handler", codigo, ambiente_api, 29, 256, None,
                            "Opcion A: puerta publica detras de API Gateway")

    paso("Permisos para que ClimateApiV2 invoque a las otras Lambdas")
    for funcion in [FN_DB] + LECTURAS:
        permitir(lam, funcion, "climate-api-v2-invoke", Principal=rol)

    paso(f"API Gateway {API_NOMBRE}")
    existentes = [a for a in api.get_apis(MaxResults="100").get("Items", []) if a["Name"] == API_NOMBRE]
    if existentes:
        datos_api = existentes[0]
        print(f"La API ya existía: {datos_api['ApiId']}")
    else:
        datos_api = api.create_api(Name=API_NOMBRE, ProtocolType="HTTP", Target=arn_api,
                                   Description="Opcion A: portal → ClimateApiV2")
        print(f"API creada: {datos_api['ApiId']}")
    permitir(lam, FN_API, "apigateway-climate-portal-api-v2", Principal="apigateway.amazonaws.com",
             SourceArn=f"arn:aws:execute-api:{REGION}:{cuenta}:{datos_api['ApiId']}/*")
    url = datos_api["ApiEndpoint"].rstrip("/")
    SALIDA.write_text(json.dumps({"api_url": url, "api_id": datos_api["ApiId"], "lambda_db": arn_db,
                                  "lambda_api": arn_api, "security_group": sg}, indent=2), encoding="utf-8")

    bien = probar(url)
    paso("LISTO" if bien else "Terminó, pero la prueba falló (mira el mensaje de arriba)")
    print(f"URL de la API (CLIMA_API_URL): {url}")
    print("\nEn tu PC:")
    print(f"   .\\.venv\\Scripts\\python.exe app.py --mode api --api-url {url}")
    print("\nEn Vercel → Settings → Environment Variables:")
    print(f"   CLIMA_API_URL    = {url}")
    print(f"   CLIMA_SECRET_KEY = {secrets.token_urlsafe(48)}")
    print("(La URL también quedó en aws_opcion_a\\salida.json; ese archivo no tiene contraseñas.)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Despliega la opción A (API Gateway + Lambdas) en el Learner Lab.")
    parser.add_argument("--solo-codigo", action="store_true", help="solo sube código nuevo a las 2 Lambdas")
    parser.add_argument("--probar", action="store_true", help="solo prueba la API ya desplegada")
    parser.add_argument("--logs", action="store_true", help="muestra los últimos mensajes de las 2 Lambdas")
    parser.add_argument("--sin-vpc", action="store_true", help="plan B si LabRole no puede usar la VPC")
    parser.add_argument("--runtime", default="python3.13")
    ARGS = parser.parse_args()
    main()
