"""Roles y permisos: una sola matriz para el portal, la API REST, las Lambdas y las plantillas.

Perfiles:
  - Visitante (sin cuenta ni sesión): consulta el clima, el pronóstico y los registros públicos.
  - ADMIN: inicia sesión; consulta, agrega, edita e importa registros. Solo ve y edita su propio perfil.
  - SUPERADMIN: todo lo anterior y además elimina registros, gestiona administradores y ve la bitácora.

DATA_ENTRY y ANALYST siguen en la tabla roles de MySQL (vienen del portal v3 y no se borran datos),
pero no pueden iniciar sesión.

Regla de oro: cualquier acción o ruta que no esté aquí se niega. Las plantillas reciben `puede`
(permisos(usuario)) y `rol_texto` para mostrar solo lo que cada perfil puede usar; la seguridad
la hace el servidor con puede(), no la plantilla.
"""
SUPERADMIN = "SUPERADMIN"
ADMIN = "ADMIN"
STAFF = (SUPERADMIN, ADMIN)                 # roles con cuenta que pueden iniciar sesión
HEREDADOS = ("DATA_ENTRY", "ANALYST")       # del portal v3: existen en MySQL, no inician sesión
VISITANTE = None
SESION = "sesion"                           # cualquier cuenta activa (ADMIN o SUPERADMIN)
DESCONOCIDA = "desconocida"                 # no está en la matriz: siempre se niega

ETIQUETAS = {SUPERADMIN: "Super administrator", ADMIN: "Administrator"}

# acción -> roles que la pueden ejecutar (VISITANTE = sin sesión)
MATRIZ = {
    "ver_registros": (VISITANTE, ADMIN, SUPERADMIN),
    "agregar": (ADMIN, SUPERADMIN),
    "editar": (ADMIN, SUPERADMIN),
    "importar": (ADMIN, SUPERADMIN),
    "eliminar": (SUPERADMIN,),
    "gestionar_admins": (SUPERADMIN,),
    "ver_bitacora": (SUPERADMIN,),
    "mi_perfil": (ADMIN, SUPERADMIN),
}

# Cada ruta web declara lo que necesita: None = pública, SESION = cualquier cuenta, o una acción.
RUTAS = {
    "public_home": None, "registros_publicos": None, "login": None,
    "health": None, "favicon": None, "static": None,
    "logout": SESION, "inicio": SESION, "index": SESION, "ir_a_hora": SESION,
    "add": "agregar", "edit": "editar", "delete": "eliminar",
    "import_records": "importar", "import_preview": "importar", "import_confirm": "importar",
    "import_template": "importar",
    "admin_users": "gestionar_admins", "admin_user_status": "gestionar_admins",
    "admin_audit": "ver_bitacora", "admin_audit_rotate": "ver_bitacora",
    "account_password": "mi_perfil", "perfil": "mi_perfil",
}


def rol(usuario):
    """Rol de una cuenta (dict con 'role') o VISITANTE si no hay sesión."""
    if not usuario:
        return VISITANTE
    return usuario.get("role") if isinstance(usuario, dict) else VISITANTE


def es_personal(usuario):
    """True si es una cuenta que puede iniciar sesión (ADMIN o SUPERADMIN)."""
    return rol(usuario) in STAFF


def es_super(usuario):
    return rol(usuario) == SUPERADMIN


def puede(usuario, accion):
    """¿Este usuario (o el visitante, si usuario es None) puede hacer esta acción?"""
    if accion == SESION:
        return es_personal(usuario)
    permitidos = MATRIZ.get(accion)
    if permitidos is None:
        return False
    actual = rol(usuario)
    if actual is not VISITANTE and actual not in STAFF:
        return False                     # roles heredados o desconocidos: nada
    return actual in permitidos


def permisos(usuario):
    """Diccionario para las plantillas: {"eliminar": False, ...}."""
    return {accion: puede(usuario, accion) for accion in MATRIZ}


def etiqueta(usuario):
    """Texto del rol para la interfaz ('Super administrator', 'Administrator') o None."""
    return ETIQUETAS.get(rol(usuario))
