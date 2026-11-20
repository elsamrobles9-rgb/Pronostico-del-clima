# ETL de AWS Clima V2: 10,000 registros reales en DynamoDB y PostgreSQL

Este módulo carga **10,000 registros climáticos reales** (5 ciudades × 2,000 horas, de julio a septiembre de 2026) y los hace pasar por los tres motores de la arquitectura:

```
Open-Meteo (datos reales)
   │ paso 1: descargar           (tu PC, internet; sin AWS)
   ▼
datos/crudos/*.json
   │ paso 2: transformar y validar con contract.py   (tu PC; sin AWS)
   ▼
datos/lecturas.jsonl (10,000)  +  paso3_catalogos_mysql.sql
   │ paso 3: catálogos ───────────────► RDS MySQL  climate_transactional
   │ paso 4: carga por lotes ─────────► DynamoDB   ClimateReadingsV2
   ▼
   │ paso 5: ETL (lee DynamoDB + catálogo MySQL) ─► stg_observations ─► 10 STORED PROCEDURES
   ▼                                                 series · observations · etl_batches · climate_aggregates
   │ paso 6: predicción (Python + SP 9 y 10) ──────► predictions · prediction_evaluations · model_metrics
consultas_verificacion.sql · consultas_prediccion.sql (DBeaver)
```

**Reglas que respeta:**
- Todo se valida con el **mismo `contract.py`** del portal y de las Lambdas.
- La carga a DynamoDB usa el **mismo `service.py`** de las Lambdas: genera `version`, `content_hash`, `transaction_id` y las fechas, igual que si cada registro entrara por el portal.
- Ningún script guarda contraseñas ni claves. Las credenciales de AWS se leen de `~/.aws/credentials` y las contraseñas de las bases se piden al ejecutar el script, sin mostrarse.
- Todos los pasos se pueden **repetir sin duplicar datos**.

## 0. Preparación (una sola vez)

1. Descomprime este ZIP **dentro de** `climate_portal_v3`. La carpeta `etl` debe quedar junto a `app.py`.
2. En la terminal de VS Code, ya ubicada en `climate_portal_v3`, instala las dependencias:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r etl\requirements_etl.txt
   ```
3. **Opcional, pero recomendado:** descarga el certificado oficial de RDS. Con él, la conexión a las bases se verifica por completo:
   ```powershell
   Invoke-WebRequest https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem -OutFile etl\global-bundle.pem
   ```
   Sin este archivo la conexión sigue cifrada, pero no se verifica el certificado del servidor. El script te avisa cuando falta.

Si quieres otras ciudades u otras fechas, cámbialas en `etl\config.py` **antes** del paso 1.

## Paso 1 · Descargar los datos reales (sin AWS)

```powershell
.\.venv\Scripts\python.exe etl\paso1_descargar.py
```

**Qué esperar:** una línea por ciudad con `2208 horas` y el modelo usado. Tarda alrededor de un minuto. Si el modelo predeterminado no trae visibilidad o índice UV, el script prueba automáticamente con el modelo GFS.

## Paso 2 · Transformar y validar (sin AWS)

```powershell
.\.venv\Scripts\python.exe etl\paso2_transformar.py
```

**Qué esperar:**
- `Registros listos para cargar: 10,000`.
- Algunas filas rechazadas, con su motivo. Por ejemplo: un dato faltante, o un punto de rocío mayor que la temperatura, que tu contrato no permite. Quedan en `etl\datos\rechazadas.jsonl`.

## Paso 3 · Catálogos en MySQL (DBeaver)

*Necesita la RDS **climate-mysql** encendida.*

1. Abre `etl\datos\paso3_catalogos_mysql.sql` en **VS Code** y copia todo su contenido (Ctrl+A, Ctrl+C). Copiarlo desde VS Code evita problemas con los acentos.
2. En DBeaver, abre un editor SQL en la conexión **MySQL**, pega el contenido y ejecuta todo con **Alt+X**.
3. La última consulta debe mostrar **2** fuentes, **8** condiciones, **5** ubicaciones y **5** estaciones.

## Paso 4 · Cargar en DynamoDB

*Necesita credenciales vigentes del Learner Lab en `~/.aws/credentials`.*

Primero haz una **prueba con 20 registros**:
```powershell
.\.venv\Scripts\python.exe etl\paso4_cargar_dynamodb.py --limite 20
```
El script muestra tu rol y la tabla, y te pide confirmar con `s`. Si dice `creado 20`, carga el resto:
```powershell
.\.venv\Scripts\python.exe etl\paso4_cargar_dynamodb.py
```

**Qué esperar:**
- Un avance cada 500 registros. Al final: `creado 9,980` y `ya_existia 20` (los 20 de la prueba).
- Tarda pocos minutos y cuesta **menos de 2 centavos de dólar** (DynamoDB en modo bajo demanda).
- El detalle de cada registro queda en `etl\datos\resultado_carga.csv`.

**Opcional:** `--via lambda` invoca la Lambda `ClimateCreateReadingV2` por cada registro, en lugar de escribir directo. Así se demuestra el camino por Lambda a escala. Es más lento y también cuesta centavos.

## Instalar los stored procedures (una sola vez)

*Necesita la RDS **climate-postgres** encendida.*

En DBeaver abre `etl\sql\procedimientos_postgres.sql` sobre la conexión **PostgreSQL** y ejecútalo completo con **Alt+X**. Crea la tabla de staging `stg_observations` y estos procedimientos en el esquema `climate_analytics`:

| # | Procedimiento | Qué hace |
|---|---|---|
| 1 | `sp_iniciar_lote` | Abre el lote del ETL (RUNNING) |
| 2 | `sp_sincronizar_series` | Copia ubicaciones y estaciones del catálogo de MySQL a `series` |
| 3 | `sp_validar_staging` | **Limpieza**: faltantes, formatos, rangos, rocío ≤ temperatura, minuto exacto, decimales, duplicados; anota el motivo |
| 4 | `sp_clasificar_condiciones` | **Calcula la condición** con reglas (lluvia, llovizna, niebla, nublado...) si la fuente no la trae |
| 5 | `sp_cargar_observaciones` | Inserta o actualiza `observations` (gana el dato más reciente) |
| 6 | `sp_marcar_borrados` | Borrado lógico de lo que ya no existe en DynamoDB |
| 7 | `sp_calcular_agregados_diarios` | Promedios diarios por día local |
| 8 | `sp_cerrar_lote` | Cierra el lote con conteos; las filas rechazadas quedan como **cola de anómalos** |
| 9 | `sp_evaluar_predicciones` | Compara predicciones con lo que realmente pasó |
| 10 | `sp_calcular_metricas` | Calcula **MAE y RMSE** por ciudad y variable |

La última consulta del script debe mostrar 10 `PROCEDURE` y 2 `FUNCTION`. Se puede ejecutar más de una vez. Requiere PostgreSQL 13 o superior (`SELECT version();`).

## Paso 5 · ETL a PostgreSQL

*Necesita **las dos RDS encendidas**, credenciales de AWS vigentes y los stored procedures instalados.*

Python solo extrae los datos de DynamoDB y los deja en `stg_observations`; **la transformación y la limpieza las hacen los stored procedures** (1 a 9) en una sola transacción.

```powershell
.\.venv\Scripts\python.exe etl\paso5_etl_postgres.py
```

1. Te pide el **usuario y la contraseña de MySQL** y luego los de **PostgreSQL**: son los mismos que usas en DBeaver. No se muestran ni se guardan.
2. Muestra cuántos registros leyó de DynamoDB y te pide confirmar con `s`.

**Qué esperar la primera vez:** `válidos` y `rechazados` (los calcula `sp_validar_staging`), `insertados 10,000` (más el registro de prueba que ya estaba en la tabla, si su ubicación está en el catálogo) y alrededor de **420 agregados diarios**. Los registros cuya ubicación, fuente o condición no existe en el catálogo de MySQL aparecen como **rechazados**, con su motivo en un CSV. Por ejemplo, el registro de muestra `LOC-001` del portal.

**Las siguientes veces:** el ETL es *incremental*. Solo procesa lo que cambió en DynamoDB desde el último lote exitoso; por ejemplo, un registro que editaste en el portal. Opciones:
- `--completo` lee toda la tabla y marca como borradas (`deleted_at_utc`) las observaciones que ya no existen en DynamoDB.
- `--catalogo-local` usa la copia local del catálogo (paso 2) si no puedes conectarte a MySQL.

Las filas rechazadas se quedan en `stg_observations` con su motivo (cola de registros anómalos) y además se guardan en un CSV.

Si algo falla a mitad del proceso, no queda nada a medias: el lote se revierte y queda registrado como `FAILED` en `etl_batches`.

## Paso 6 · Predicción (PostgreSQL)

*Necesita la RDS **climate-postgres** encendida y el paso 5 ya ejecutado.*

```powershell
.\.venv\Scripts\python.exe etl\paso6_predecir.py
```

Te pide el usuario y la contraseña de PostgreSQL, y luego hace lo siguiente:
1. Divide los días completos de `climate_aggregates` en **entrenamiento (70 %)**, **validación (15 %)** y **prueba (15 %)**.
2. Compara 3 métodos para predecir el día siguiente: **persistencia** ("mañana igual que hoy"), **promedio de 7 días** y **regresión lineal** (día anterior + promedio de 7 días).
3. Muestra el error de cada uno (**MAE** y **RMSE**) y elige el que menos se equivoca en validación.
4. Muestra el pronóstico de los próximos 7 días por ciudad y pide confirmar con `s`.
5. Guarda en `prediction_models`, `training_runs`, `model_metrics` (MAE y RMSE) y `predictions` (días de prueba y 7 días futuros).
6. Llama a `sp_evaluar_predicciones` y `sp_calcular_metricas` para evaluar los días de prueba contra lo real.

Cada vez que corres el paso 5 con datos nuevos, `sp_evaluar_predicciones` también evalúa los pronósticos futuros cuyo día real ya llegó: así se audita el error contra las mediciones posteriores.

Revisa los resultados con `etl\consultas_prediccion.sql` en DBeaver. La consulta 5 muestra la temperatura real contra la predicha, lista para graficar.

**Aclaración para la entrega:** es un pronóstico **estadístico** con 3 meses de datos. Funciona razonablemente para la temperatura del día siguiente, pero es débil para la lluvia. Los servicios meteorológicos usan modelos físicos de la atmósfera.

## Verificar (DBeaver)

Abre `etl\consultas_verificacion.sql` en la conexión **PostgreSQL** y ejecuta cada consulta con **Ctrl+Enter**:
1. Conteos por tabla.
2. Historial de lotes del ETL.
3. Resumen por ciudad.
4. **Trazabilidad**: el mismo registro con su `record_id`, `version` y `content_hash`. Compáralo con el ítem en la consola de DynamoDB.
5. Los días más lluviosos.
6. Temperatura media diaria, lista para graficar.
7. Los 10 stored procedures instalados.
8. La cola de registros anómalos y sus motivos.
9. Condición de la fuente contra la condición calculada por la regla.
10. Porcentaje de coincidencia entre ambas.

## Problemas comunes

| Mensaje | Qué hacer |
|---|---|
| `ExpiredToken` / `InvalidClientTokenId` | Copia credenciales nuevas de *AWS Details* a `~/.aws/credentials`. Si la carga se cortó, vuelve a ejecutarla: lo ya cargado se cuenta como `ya_existia`. |
| `La instancia ... está 'stopped'` | Enciende la RDS y espera a que diga `available`. |
| Se queda esperando y marca *timeout* al conectar a MySQL o PostgreSQL | Tu IP cambió. Revísala en https://checkip.amazonaws.com y agrégala al security group. |
| `database "climate_analytics" does not exist` | Cambia `POSTGRES_DB` en `etl\config.py` por el nombre de base que aparece en tu conexión de DBeaver. |
| `Faltan los procedimientos almacenados` | Instala `etl\sql\procedimientos_postgres.sql` en DBeaver (Alt+X). |
| `function gen_random_uuid() does not exist` | Tu PostgreSQL es anterior a la 13; avísame para adaptar el script. |
| Menos de 10,000 registros en el paso 2 | Amplía `FECHA_FIN` en `config.py` y repite los pasos 1 y 2. |

## Créditos de los datos

Los datos meteorológicos provienen de **Open-Meteo.com** ([Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api)), con licencia **CC BY 4.0**. Cita esta fuente en tu entrega.

## Pendiente (siguiente versión)

- Guardar las filas rechazadas en `ClimateRejectedRecords` y registrar cada carga en `ClimateIdempotency`. Para eso hace falta confirmar las llaves de esas tablas.
- Que las Lambdas escriban eventos en `ClimateOutbox`. Hoy el ETL usa `transaction_id` y `updated_at_utc` de cada registro para detectar cambios.
