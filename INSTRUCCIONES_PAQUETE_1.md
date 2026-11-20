# Paquete 1 — Solo CDMX y 3 stored procedures nuevos

Qué hace este paquete:

1. Instala los SP nuevos:
   - `sp_upsert_lecturas` en PostgreSQL.
   - `sp_guardar_admin` y `sp_rotar_auditoria` en MySQL, más la tabla `audit_events_history`.
   - En total quedan 13 SP.
2. Respalda y borra las otras ciudades (Guadalajara, Monterrey, Mérida y Tijuana) y los registros de prueba con otra ubicación.
3. Descarga y carga unos 14 meses de CDMX: las 10,000 horas válidas más recientes, del 20 de septiembre de 2025 al 18 de noviembre de 2026.

Todo se hace en la terminal de VS Code, en la carpeta del proyecto (la que tiene `app.py`), en tu rama `cdmx-admin-visitante`.

## 0. Antes de empezar

1. Revisa que estés en la rama correcta:
   ```powershell
   git branch
   ```
   Debe tener `*` junto a `cdmx-admin-visitante`.
2. Inicia el Learner Lab y copia las credenciales nuevas de *AWS Details* a `~/.aws/credentials`.
3. Enciende las dos RDS y espera a que estén listas (unos 5 a 10 minutos):
   ```powershell
   aws rds start-db-instance --db-instance-identifier climate-mysql --region us-east-1 --query "DBInstance.DBInstanceStatus"
   aws rds start-db-instance --db-instance-identifier climate-postgres --region us-east-1 --query "DBInstance.DBInstanceStatus"
   aws rds wait db-instance-available --db-instance-identifier climate-mysql --region us-east-1
   aws rds wait db-instance-available --db-instance-identifier climate-postgres --region us-east-1
   ```
   Los dos `wait` se quedan esperando y terminan sin mensaje cuando la base está lista.
4. Revisa tu IP en https://checkip.amazonaws.com. Si cambió, agrégala al security group (puertos 3306 y 5432) como las otras veces.

## 1. Descomprimir

Copia `paquete1_cdmx.zip` en la carpeta del proyecto y ejecuta:

```powershell
Expand-Archive -Path .\paquete1_cdmx.zip -DestinationPath . -Force
```

## 2. Instalar los stored procedures (unos 2 minutos)

```powershell
.\.venv\Scripts\python.exe etl\instalar_sp.py
```

Te pide el usuario y la contraseña de PostgreSQL y luego los de MySQL. Lo correcto se ve así:

```
Instalado. 11 procedimientos y 2 funciones: ... sp_upsert_lecturas ...
Instalado. Procedimientos: sp_guardar_admin, sp_rotar_auditoria
Tabla audit_events_history: lista
```

## 3. Respaldar y borrar las otras ciudades (unos 3 minutos)

```powershell
.\.venv\Scripts\python.exe etl\purgar_otras_ciudades.py
```

1. Primero **solo cuenta** y te enseña cuánto hay por ciudad y por tabla.
2. Guarda el respaldo en `etl\respaldos\<fecha>\`. Esa carpeta no se sube a GitHub.
3. Luego te pide escribir `BORRAR`. Si escribes otra cosa, no borra nada.

Debe terminar con `Lecturas que no son LOC-CDMX en DynamoDB: 0`.

## 4. Descargar y cargar los 14 meses de CDMX (unos 10 a 15 minutos)

Ejecuta uno por uno:

```powershell
.\.venv\Scripts\python.exe etl\paso1_descargar.py
.\.venv\Scripts\python.exe etl\paso2_transformar.py
.\.venv\Scripts\python.exe etl\paso4_cargar_dynamodb.py --si
.\.venv\Scripts\python.exe etl\paso5_etl_postgres.py --completo --si
```

Qué debes ver:

- **Paso 2:** `Registros listos para cargar: 10,000`.
- **Paso 4:** unos 8,000 `creado` y unos 2,000 `ya_existia`, que son las horas de julio y agosto que ya tenías. Si aparecen algunos `rechazado`, mándamelos.
- **Paso 5:** el lote termina en `SUCCEEDED`, con unas 10,000 lecturas válidas.

Si el paso 5 pide la contraseña de MySQL, es para leer el catálogo; usa la misma de siempre.

## 5. Comprobar en DBeaver (PostgreSQL)

```sql
SELECT s.location_id, count(*) AS horas,
       min(o.timestamp_utc) AS desde, max(o.timestamp_utc) AS hasta
FROM climate_analytics.observations o
JOIN climate_analytics.series s USING (series_id)
WHERE o.deleted_at_utc IS NULL
GROUP BY s.location_id;
```

Debe salir **una sola fila**, `LOC-CDMX`, con unas 10,000 horas.

## 6. Mándame

Lo que salió en los pasos 2 a 5 (puede ser una captura de cada uno).

- Si vas a seguir con el paquete 2 enseguida, deja las RDS encendidas.
- Si no, apágalas:

```powershell
aws rds stop-db-instance --db-instance-identifier climate-mysql --region us-east-1 --query "DBInstance.DBInstanceStatus"
aws rds stop-db-instance --db-instance-identifier climate-postgres --region us-east-1 --query "DBInstance.DBInstanceStatus"
```

Todavía no corras `paso6_predecir.py`: el modelo nuevo viene en el paquete 2.
