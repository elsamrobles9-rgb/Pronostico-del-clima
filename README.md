# Pronóstico del clima — Ciudad de México

Portal Flask con el clima actual, el pronóstico de 7 días con desglose por hora y la administración de las
lecturas meteorológicas de la CDMX. Los datos viven en AWS: DynamoDB (lecturas, vía 4 Lambdas), RDS MySQL
(cuentas, sesiones, bitácora y catálogos) y RDS PostgreSQL (copia analítica y pronóstico).
La versión publicada corre en Vercel y habla con AWS solo por API Gateway (opción A).

## Perfiles y permisos

La matriz está en un solo lugar, `services/roles.py`, y la aplican el portal, la API REST y las Lambdas
ClimateApiV2 y ClimateDbV2. Ocultar un botón es solo comodidad: el servidor revisa cada petición.

| Funcionalidad | Visitante | Administrador | Superadministrador |
|---|---|---|---|
| Consultar clima y pronóstico (`/`) | Sí | Sí | Sí |
| Consultar registros (`/registros`, solo lectura) | Sí | Sí | Sí |
| Iniciar sesión | No la necesita | Sí | Sí |
| Agregar, modificar e importar registros | No | Sí | Sí |
| Eliminar registros | No | No | Sí |
| Crear, activar y desactivar administradores | No | No | Sí |
| Ver la lista de cuentas y la bitácora completa | No | No | Sí |
| Ver y editar su propio perfil (`/account/profile`) | — | Sí | Sí |

Reglas de las cuentas:

- No hay registro público: las cuentas las crea el superadministrador. Las cuentas nuevas siempre son ADMIN.
- Los roles solo cambian desde la línea de comandos (`manage_users.py` o la migración), nunca desde la web ni la API.
- Nadie puede desactivar, degradar ni cambiar la contraseña de un superadministrador desde el panel.
- El sistema nunca se queda sin un superadministrador activo (aplicación, `sp_guardar_admin` y triggers de MySQL).
- Un administrador solo ve su propia actividad y su propio perfil.
- Desactivar una cuenta o cambiar su contraseña revoca sus sesiones al instante.

## Ejecutar en tu computadora (modo demo, sin AWS)

Requiere Python 3.13. Desde la carpeta del repositorio, en PowerShell:

```
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage_users.py create super --role SUPERADMIN
python manage_users.py create admin
python app.py
```

- Página pública: http://127.0.0.1:5000 y registros públicos: http://127.0.0.1:5000/registros
- Administración: http://127.0.0.1:5000/login

Las cuentas del modo demo se guardan en `instance/portal.sqlite3` (no se sube a Git). Los datos son sintéticos y los
cambios se pierden al cerrar. `python app.py --datos-reales` usa las 10,000 lecturas reales de
`etl/datos/lecturas.jsonl`, si las generaste con el ETL.

Otras acciones de cuentas: `list`, `disable`, `enable`, `reset-password` y `set-role`
(por ejemplo `python manage_users.py set-role admin --role SUPERADMIN`). Con `--mysql` trabajan sobre RDS MySQL.

## Pruebas

```
python -m unittest discover -s tests
```

149 pruebas, sin AWS. `tests/test_roles.py` cubre los tres perfiles en la web, la API REST y las peticiones
directas a ClimateApiV2/ClimateDbV2; `tests/test_migracion_superadmin.py` cubre la migración de MySQL.

## Modos con AWS

| Modo | Comando | Necesita |
|---|---|---|
| Opción A (también Vercel) | `python app.py --mode api --api-url https://...amazonaws.com` | Solo la URL de API Gateway |
| Lambdas directas | `python app.py --mode lambda` | Credenciales del Learner Lab y usuario/contraseña de las 2 RDS |

En Vercel se configuran dos variables: `CLIMA_API_URL` y `CLIMA_SECRET_KEY` (32 caracteres o más). Las contraseñas
de las bases solo existen en la configuración de la Lambda ClimateDbV2. Nunca subas `.env`, contraseñas ni esas
variables al repositorio.

## Poner en marcha los roles en AWS

Hazlo con el laboratorio y las 2 RDS encendidos, en este orden:

1. **Subir el código nuevo a las 2 Lambdas del portal** (las 4 Lambdas de lecturas no cambian), desde esta rama:
   `python aws_opcion_a\desplegar.py --solo-codigo`
2. **Migrar MySQL:** `python etl\migrar_superadmin.py`. Pide el usuario y la contraseña de MySQL y el correo de la
   cuenta ADMIN que será superadministrador. Al final compara la huella de todas las contraseñas: ninguna debe
   cambiar. Usa `--revisar` para solo ver el estado y `--revertir` para deshacerlo. La versión manual está en
   `sql/migracion_superadmin.sql`.
3. **Publicar el portal:** integrar la rama en la rama que Vercel publica en producción
   (Vercel → Settings → Git → Production Branch).
4. **Probar en la versión publicada:** como visitante (sin sesión), como administrador y como superadministrador.
5. **Apagar las 2 RDS** al terminar.

Si las Lambdas se quedaran con el código anterior, el superadministrador no podría iniciar sesión en Vercel.

## Estructura

| Ruta | Contenido |
|---|---|
| `app.py` | Portal Flask: rutas, autorización por ruta, CSRF y cabeceras de seguridad |
| `services/roles.py` | Matriz de permisos y rutas declaradas |
| `services/access.py`, `services/mysql_store.py` | Cuentas, sesiones y bitácora (demo en SQLite y AWS en MySQL) |
| `services/api.py` | API REST `/api` (JWT, Idempotency-Key, If-Match) |
| `services/remoto.py`, `backend/` | Opción A: portal → API Gateway → ClimateApiV2 → ClimateDbV2 y las 4 Lambdas |
| `services/analytics.py`, `prediccion/` | Página pública, registros públicos y modelo de pronóstico |
| `templates/`, `static/`, `public/static/` | Interfaz (`public/static/` es la copia que sirve Vercel) |
| `sql/`, `etl/` | Procedimientos almacenados, migración y ETL |
| `tests/` | Pruebas automatizadas |

## Contrato con la interfaz

Todas las plantillas reciben `current_user` (con `role`: `SUPERADMIN` o `ADMIN`), `rol_texto` y `puede`, un
diccionario con `ver_registros`, `agregar`, `editar`, `importar`, `eliminar`, `gestionar_admins`, `ver_bitacora` y
`mi_perfil`. El formulario de alta de administradores acepta `intent=create` (rechaza correos repetidos) o
`intent=password` (exige que la cuenta exista); sin `intent` hace lo de antes: crea la cuenta o cambia su
contraseña.
