# Climate Portal V3 — Login e Import records

Esta versión amplía el portal Flask V2 recibido. Conserva contract.py, ReadingService y los clientes Demo/Lambda. No requiere modificar DynamoDB ni las funciones Lambda existentes. No carga archivos .env.

## 1. Abrir en VS Code (Windows)

Extrae el ZIP en una carpeta nueva. Abre la carpeta `climate_portal_v3` que contiene app.py. No sobrescribas V2: sirve para comparar versiones.

En la terminal de VS Code (PowerShell o CMD), ejecuta uno por uno:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage_users.py create admin --role ADMIN
```

Escribe una contraseña de 12 a 128 caracteres y repítela. No se muestra mientras escribes. No hay contraseña predeterminada. Si el usuario ya existe, usa ese usuario o crea otro nombre.

```powershell
.\.venv\Scripts\python.exe app.py --mode demo
```

Abre http://127.0.0.1:5000 e inicia sesión. No necesitas encender RDS para esta demostración. Este modo no hace peticiones AWS. El registro de ejemplo y las mediciones de demo se reinician al cerrar el programa; las cuentas locales persisten en `instance/portal.sqlite3`. No compartas esa carpeta.

## 2. Probar Import records

1. Entra con tu administrador y selecciona **Import records**.
2. Descarga **CSV template** o **JSON template** desde esa pantalla.
3. Sube el archivo. Pulsa **Validate file**.
4. Revisa claves normalizadas y valores UV/punto de rocío. Todavía no se escribió ninguna medición.
5. Pulsa **Import into demo**. Revisa el resultado individual de cada fila.
6. Abre **Records** y busca `IMPORT-001`.
7. Para mostrar validaciones, cambia `humidity_pct` a 101 y vuelve a subir el archivo. No podrás confirmar un archivo con errores.

Formatos: CSV con encabezados exactos en inglés o JSON con un arreglo de objetos. UTF-8. Máximo 256 KB y 50 registros por archivo. Se requieren todas las mediciones actuales. La fecha debe incluir zona horaria, por ejemplo `2026-10-16T12:30:00Z`; se normaliza al minuto UTC. Dos filas de una misma ubicación/minuto se rechazan dentro del archivo.

**Alcance:** esta primera importación agrega registros nuevos. Una clave existente no se sobrescribe automáticamente; usa Edit record para modificarla. Falta desarrollar la política completa de upsert con revisión de diferencias del requisito final del profesor. La validación del archivo es local; la existencia de claves se comprueba al enviar a la Lambda.

El envío es secuencial: un fallo no revierte filas que ya se guardaron. La confirmación de un mismo lote se reclama una sola vez en SQLite; esto no sustituye una Idempotency-Key implementada en el API. No hay reintentos automáticos. Si una respuesta se pierde, consulta el registro antes de volver a subirlo. Si se cierra el proceso durante la importación, algunas filas pueden estar guardadas. Las vistas previas caducan tras una hora y dejan de estar disponibles al reiniciar el proceso. Los lotes de más de un día se limpian al subir otro archivo.

## 3. Roles y cuentas locales

```powershell
.\.venv\Scripts\python.exe manage_users.py create operator --role DATA_ENTRY
.\.venv\Scripts\python.exe manage_users.py create analyst --role ANALYST
.\.venv\Scripts\python.exe manage_users.py disable operator
```

ADMIN y DATA_ENTRY pueden consultar, agregar, editar e importar. Solo ADMIN elimina. ANALYST solo consulta; el servidor bloquea sus rutas de escritura aunque escriba la URL manualmente. Esta asignación de eliminación exclusiva a ADMIN debe validarse con el cliente.

No hay registro público. Las contraseñas se guardan como hashes scrypt. El cierre por inactividad es de 30 minutos, comprobado en la siguiente petición. Deshabilitar una cuenta invalida sus sesiones para la siguiente petición; no cancela una operación ya iniciada. Hay bloqueo por usuario durante 15 minutos tras cinco intentos fallidos. Cerrar el programa obliga a iniciar sesión de nuevo. Formularios protegidos con CSRF, cookies HttpOnly/SameSite y plantillas con escape HTML.

**Esto es autenticación local funcional de desarrollo, no la autenticación final de RDS/JWT.** SQLite permite presentar el flujo sin crear infraestructura. No modifica las tablas users/roles/sessions de MySQL. No protege por sí mismo los endpoints públicos de AWS: su IAM/autorizador sigue siendo independiente. Antes de desplegar para otros usuarios se necesita HTTPS, cookies Secure, configuración de secretos estable, gestión central de cuentas, JWT/autorización del API y revisión de seguridad. Esta app se inicia solo en 127.0.0.1 y sin debug.

Referencia de controles web: https://flask.palletsprojects.com/en/stable/web-security/

## 4. Usar las Lambdas existentes (opcional)

Primero comprueba demo. Para el modo AWS:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements_aws.txt
aws sts get-caller-identity --region us-east-1 --query Account --output text
.\.venv\Scripts\python.exe app.py --mode lambda --region us-east-1
```

Usa tus credenciales temporales vigentes del laboratorio mediante el mecanismo que ya tienes configurado para AWS CLI/boto3. No pegues credenciales en el código ni las envíes por chat. Las credenciales puestas en variables de una terminal solo estarán disponibles para el proceso iniciado desde esa terminal.

Este proyecto recibido usa invocación directa de Lambda con boto3, no API Gateway, en `--mode lambda`:

- ClimateCreateReadingV2
- ClimateReadReadingV2
- ClimateUpdateReadingV2
- ClimateDeleteReadingV2

Import records reutiliza ClimateCreateReadingV2. No se modificó ni desplegó ninguna función. Las peticiones reales pueden generar cargos normales del laboratorio. No se probó contra tu cuenta AWS en esta entrega. No necesitas encender MySQL/PostgreSQL para este portal con autenticación local.

## 5. Pruebas automatizadas

Desde la carpeta que contiene app.py:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Cubren CRUD y concurrencia anteriores, permisos, CSRF, expiración, revocación, bloqueo de login, validación de archivos, propiedad de lotes, confirmación repetida, claves existentes y plantillas CSV/JSON. No usan AWS ni tus credenciales. El resultado entregado está en TEST_RESULTS.txt. Ejecutadas en Python 3.12 del entorno de entrega; debes repetirlas en tu Python 3.14 para verificar esa versión.

## 6. Capturas para el profesor

| Captura | Acción | Qué demuestra |
|---|---|---|
| 01 — V2 anterior | Abre tu copia anterior y captura su listado | Punto de comparación real; no sustituirlo por una recreación |
| 02 — Login V3 | Abre V3 sin iniciar sesión | Nueva interfaz y acceso restringido |
| 03 — Records V3 | Inicia sesión | Navegación, usuario, rol y CRUD conservado |
| 04 — Import records | Abre el módulo | CSV/JSON y plantillas |
| 05 — Error de validación | Sube humedad 101 | Prevención de mediciones inválidas |
| 06 — Preview | Sube la plantilla correcta | Revisión antes de escribir |
| 07 — Results y Records | Confirma y busca IMPORT-001 | Trazabilidad visual del resultado |
| 08 — Analyst | Inicia sesión como analyst | Interfaz de solo lectura |

Justificación: Login responde al acceso por roles; Import records reduce captura manual y prepara la ingestión híbrida. El límite de 50 es para esta entrega local síncrona, no una solución definitiva para 10,000 registros semanales. Pendientes: JWT/MySQL, importación asíncrona de mayor volumen, upsert revisable, auditoría central, autorización consistente del API e idempotencia entre clientes.

## 7. Cerrar

En la terminal del portal pulsa Ctrl+C. Esto cierra Flask; no apaga recursos AWS ya encendidos por separado. Para demo no se encendió ni creó ningún recurso AWS. Conserva tu V2 como respaldo y no publiques `instance/`, credenciales ni entornos virtuales.
