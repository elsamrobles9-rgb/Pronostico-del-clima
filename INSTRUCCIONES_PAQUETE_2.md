# Paquete 2 — Portal: visitante y administrador, predicción de 7 días por hora

Antes: termina el **paquete 1** (los SP instalados y los 14 meses de CDMX cargados).

Todo va en la terminal de VS Code, en la carpeta del proyecto y en tu rama `cdmx-admin-visitante`. Necesitas el lab encendido, las dos RDS en `available` y tu IP en el security group.

## 1. Descomprimir e instalar (2 minutos)

Copia `paquete2_portal.zip` en la carpeta del proyecto y ejecuta:

```powershell
Expand-Archive -Path .\paquete2_portal.zip -DestinationPath . -Force
.\.venv\Scripts\python.exe -m pip install -r requirements_aws.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Las pruebas deben terminar en `OK` (49 pruebas). El paquete trae todo el código; no borra tu `.venv`, `instance/` ni `etl/datos`.

## 2. Predicción nueva (3 minutos)

```powershell
.\.venv\Scripts\python.exe etl\paso6_predecir.py
```

Pide la contraseña de PostgreSQL y muestra:

- Una tabla de **MAE por horizonte** (día 1 a día 7) para los 3 métodos, en validación y prueba.
- El error **por hora**.
- El pronóstico de 7 días.

Confirma con `s`. Debe terminar con `Guardado ... (7 días + 168 horas)`. **Toma captura de las tablas.**

## 3. Arrancar el portal en modo AWS

```powershell
.\.venv\Scripts\python.exe app.py --mode lambda
```

1. Te pide el usuario y la contraseña de **MySQL**.
2. La primera vez no hay administradores, así que te pide correo, nombre y una contraseña nueva para el **primer administrador**. Se crea con `sp_guardar_admin`.
3. Luego te pide el usuario y la contraseña de **PostgreSQL**.
4. Debe salir algo como `PostgreSQL conectado: 10,000 lecturas por hora de la CDMX.` y una línea `Idempotency-Key: ...`. **Mándame esa línea.**

## 4. Probar en el navegador (toma capturas)

| Dónde | Qué hacer | Qué demuestra |
|---|---|---|
| http://127.0.0.1:5000 | Ábrelo **sin iniciar sesión**. Toca otro día y baja a "Por hora" | Visitante: clima actual, 7 días y hora por hora |
| **Acceso administrador** | Entra con tu correo | Login con JWT validado en MySQL |
| **Records → Add record** | Ubicación y estación ya vienen fijas (CDMX). Deja la condición en *Automatic* y guarda | CRUD por Lambda y el mensaje verde de `sp_upsert_lecturas` |
| **Administrators** | Crea un segundo administrador. Luego vuelve a guardar el mismo correo con otra contraseña | `sp_guardar_admin`: alta y cambio de contraseña |
| **Audit log** | Escribe `0` días y pulsa **Rotate now** | `sp_rotar_auditoria`: los eventos pasan al historial |
| **My password** | Cambia tu contraseña | Te saca y entras con la nueva (revocación inmediata) |

## 5. Postman

Vuelve a importar `postman\AWS_Clima_V2_API.postman_collection.json` (reemplaza la anterior). Escribe tu correo y contraseña en **Variables** y corre las peticiones de la `00` a la `14`. Todas deben salir en verde.

## 6. DBeaver

- **MySQL:** `sql\consultas_mysql_seguridad.sql`. La consulta 8 lista los 2 SP y la 9 muestra el historial de la bitácora.
- **PostgreSQL:** `etl\consultas_prediccion.sql`. La consulta 1 compara los métodos por horizonte, la 3 muestra los 7 días y la 4 las horas.

## 7. Subir a GitHub

```powershell
git add .
git status --short
git commit -m "Solo CDMX, visitante y administrador, 13 SP y prediccion de 7 dias por hora"
git push -u origin cdmx-admin-visitante
```

Revisa que `git status --short` **no** muestre `.venv`, `instance`, `etl/datos` ni `etl/respaldos`. Luego, en GitHub, pulsa **Compare & pull request**.

## 8. Al terminar

Ctrl+C en el portal y **apaga las dos RDS**:

```powershell
aws rds stop-db-instance --db-instance-identifier climate-mysql --region us-east-1 --query "DBInstance.DBInstanceStatus"
aws rds stop-db-instance --db-instance-identifier climate-postgres --region us-east-1 --query "DBInstance.DBInstanceStatus"
```

**Para tus compañeros:** pueden trabajar sin AWS con `python app.py` (modo demo). La página pública y el área de administrador funcionan con datos sintéticos de la CDMX.
