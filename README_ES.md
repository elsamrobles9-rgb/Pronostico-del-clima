# Portal AWS Clima V2 — Ciudad de México

El sistema trabaja **solo con la Ciudad de México** (`LOC-CDMX`, estación `ST-CDMX-OM`), como autorizó el profesor, con unos 14 meses de lecturas por hora de Open-Meteo.

| Quién | Qué ve |
|---|---|
| **Visitante** (sin cuenta, sin iniciar sesión) | `/`: clima actual, pronóstico de 7 días y desglose hora por hora. Hoy empieza en la hora actual; los demás días se ven al tocar su tarjeta |
| **Administrador** (inicia sesión con JWT) | Registros: crear, editar, borrar e importar. Alta de administradores, cambio de contraseña, bitácora con rotación y la API REST |

No hay registro público ni otros roles con acceso.

## Recursos que usa (todos existían; no se crean nuevos)

| Recurso | Para qué |
|---|---|
| Lambdas `ClimateCreate/Read/Update/DeleteReadingV2` + DynamoDB `ClimateReadingsV2` | Lecturas (CRUD) |
| RDS MySQL `climate_transactional` | `users`, `roles`, `sessions` (JWT), `audit_events` y `audit_events_history`, catálogos. SP 12 y 13 |
| RDS PostgreSQL `climate_analytics` | Lecturas y pronóstico para la página pública. SP 1 a 11 |
| DynamoDB `ClimateIdempotency` | Idempotency-Key de la API |

## Arrancar

**Modo demo** (sin AWS ni credenciales, con datos sintéticos de la CDMX; para programar la interfaz):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements_aws.txt
.\.venv\Scripts\python.exe manage_users.py create admin
.\.venv\Scripts\python.exe app.py
```

**Modo AWS** (laboratorio encendido, las dos RDS en `available` y tu IP en el security group):

```powershell
.\.venv\Scripts\python.exe app.py --mode lambda
```

Pide el usuario y la contraseña de MySQL y luego los de PostgreSQL (los de DBeaver). Si no hay administradores, crea el primero con `sp_guardar_admin`. Abre http://127.0.0.1:5000 (página pública) y http://127.0.0.1:5000/login (administración).

Las direcciones de las bases se buscan solas con las credenciales del lab. También se pueden fijar con `CLIMA_MYSQL_HOST` y `CLIMA_PG_HOST`, y los usuarios con `CLIMA_MYSQL_USER` y `CLIMA_PG_USER`. Las contraseñas siempre se escriben a mano y nunca se guardan.

## Los 13 stored procedures

Se instalan con `.\.venv\Scripts\python.exe etl\instalar_sp.py`.

| Base | SP | Lo usa |
|---|---|---|
| PostgreSQL | 1 a 10: lote, catálogo, limpieza y anómalos, condición, upsert, borrados, promedios diarios, cierre, evaluación, MAE/RMSE | ETL (`etl\paso5_etl_postgres.py`) y predicción (`etl\paso6_predecir.py`) |
| PostgreSQL | 11 `sp_upsert_lecturas` | El portal y la API, al crear, editar o importar: PostgreSQL tiene el dato al instante |
| MySQL | 12 `sp_guardar_admin` | Alta de administradores y cambio de contraseña (pantalla Administrators, My password y `manage_users.py --mysql`) |
| MySQL | 13 `sp_rotar_auditoria` | Botón "Rotate now" en Audit log: historiza la bitácora y limpia sesiones vencidas |

## Predicción

`prediccion/modelo.py` combina **lo normal de la temporada con la desviación de los últimos días, que se va apagando**, y reparte cada día en horas con el ciclo típico.

`etl\paso6_predecir.py` lo evalúa (MAE y RMSE por horizonte de 1 a 7 días, contra persistencia y promedio móvil) y guarda el pronóstico. La página pública usa el mismo modelo al momento de la consulta.

Detalles en `etl\README_ES.md`.

## API REST (Postman)

Importa `postman\AWS_Clima_V2_API.postman_collection.json`, escribe tu correo y contraseña en las variables de la colección y ejecuta las peticiones en orden.

- **Públicas:** `/api/health`, `/api/pronostico` y `/api/auth/login`.
- **Todo lo demás** exige `Authorization: Bearer <token>` de un administrador.
- `POST` y `PUT` exigen `Idempotency-Key`; `PUT` y `DELETE` exigen `If-Match` con la versión.

## Seguridad, en corto

- **JWT HS256:** firma, emisor y expiración verificados; se rechaza `alg: none`.
- **Cada petición revisa la sesión en MySQL.** Responde 401 si la sesión fue revocada, si pasaron 30 minutos sin actividad o si cambió `auth_version`, que sube al desactivar al usuario o al cambiar su contraseña.
- **5 intentos fallidos en 15 minutos bloquean la cuenta.** Las contraseñas se guardan con hash scrypt y la base solo recibe el hash.
- **Formularios** con CSRF; concurrencia optimista con la versión del registro; auditoría de cada acción.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Son 49 pruebas sin AWS: JWT, usuarios y SP de MySQL (simulados con las mismas reglas), API, idempotencia, página pública, modelo (coherencia, sin información del futuro, gana a la persistencia a 7 días) y llamada a `sp_upsert_lecturas`.

## Cerrar

Ctrl+C en la terminal del portal y **apaga las dos RDS**:

```powershell
aws rds stop-db-instance --db-instance-identifier climate-mysql --region us-east-1 --query "DBInstance.DBInstanceStatus"
aws rds stop-db-instance --db-instance-identifier climate-postgres --region us-east-1 --query "DBInstance.DBInstanceStatus"
```
