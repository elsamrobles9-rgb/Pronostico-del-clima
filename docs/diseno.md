# Guía de diseño · CDMX Weather

Diseño de la interfaz del portal del clima de la Ciudad de México, basado en el sistema **Pebble Flow** y adaptado a las reglas técnicas del proyecto (CSP estricta, sin dependencias externas, solo escritorio).

Capturas: `docs/capturas/antes/` (diseño anterior) y `docs/capturas/despues/` (diseño nuevo, a 1440 px, en modo demo).

## 1. Reglas técnicas que no cambian

- Todo el estilo vive en `static/style.css`, con una copia idéntica en `public/static/style.css`. No se usan `style="..."`, `<style>`, scripts en línea ni recursos de otros sitios.
- Las fuentes se sirven desde `static/fonts/`, nunca desde Google.
- Solo escritorio: de 1280 a 1920 px, con un mínimo de 1024 px (`body{min-width:1024px}`). Debajo de 1280 px se aplica un ajuste para pantallas más estrechas.
- Los textos visibles están en inglés. No se cambian los atributos `name` de los formularios, los `id` que usa JavaScript ni las variables que envían las rutas.

## 2. Colores

Todos los colores están en variables dentro de `:root`.

| Variable | Valor | Uso |
|---|---|---|
| `--bg` | `#efebe5` | Fondo de página (piedra) |
| `--surface` | `#f7f4ef` | Tarjetas, paneles y campos (crema) |
| `--text` | `#3c4235` | Texto y bloques oscuros (oliva) |
| `--accent` | `#ff8d5b` | Coral: rellenos, botón principal y adornos. **Nunca texto pequeño** |
| `--line` | `#d8d1c6` | Separadores |
| `--line-soft` | `#e5e7eb` | Borde suave de tarjetas |
| `--danger` | `#c94b3f` | Bordes y rellenos de error |

Tonos derivados, solo para legibilidad:

| Variable | Valor | Uso |
|---|---|---|
| `--text-2` | `#5c6152` | Texto secundario |
| `--danger-text` | `#a4392f` | Texto de error y botón de eliminar |
| `--field` | `#7d8174` | Borde de campos (contraste mínimo de 3:1) |
| `--accent-hover` | `#ff9a6c` | Botón principal al pasar el ratón |
| `--accent-tint`, `--olive-tint`, `--danger-tint` | `#f9ddce`, `#e1dfd9`, `#f6e3df` | Fondos suaves de avisos e insignias |
| `--on-dark`, `--on-dark-2` | `#f7f4ef`, `#c8c8c0` | Texto sobre bloques oliva |

### Contraste (fórmula WCAG)

| Combinación | Contraste | Uso permitido |
|---|---|---|
| Oliva sobre piedra / crema | 8.7 / 9.5 : 1 | Texto normal |
| `--text-2` sobre piedra / crema | 5.4 / 5.8 : 1 | Texto normal |
| Oliva sobre coral | 4.55 : 1 | Texto de botones |
| Oliva sobre coral hover | 5.0 : 1 | Texto de botones |
| `--danger-text` sobre piedra | 5.5 : 1 | Texto de error |
| Crema sobre `--danger-text` | 6.0 : 1 | Botón de eliminar |
| Crema sobre oliva | 9.5 : 1 | Texto sobre bloques oscuros |
| `--on-dark-2` sobre oliva | 6.2 : 1 | Texto secundario sobre oscuro |
| Coral sobre oliva | 4.55 : 1 | Etiquetas pequeñas sobre oscuro |
| `--field` sobre piedra / crema | 3.4 / 3.6 : 1 | Bordes de campos |
| Coral sobre piedra | 1.9 : 1 | **Solo adornos**: nunca texto |

## 3. Tipografía

- **Bricolage Grotesque** para títulos y números grandes, en lugar de Youth, que no es gratuita.
- **Figtree** para texto, botones y formularios, en lugar de Aeonik, que es de pago.
- Ambas tienen licencia OFL (ver `static/fonts/OFL-*.txt`), están recortadas al alfabeto latino y se sirven en formato WOFF variable.
- `h1` del panel: `clamp(2rem, 3vw, 3.25rem)`. Los tamaños gigantes solo aparecen en la página pública: temperatura actual, títulos de sección de 48 px y la marca del pie.
- Etiquetas pequeñas: 12 px, mayúsculas, con `letter-spacing: .08em`.

## 4. Formas y espacio

- Esquinas: botón principal 10 px, botón secundario 4 px, tarjetas 8 px, paneles 16 px, insignias en píldora.
- Sin sombras: la jerarquía sale del color, el tamaño y el espacio.
- Contenedor de hasta 1360 px con márgenes de 40 px. Entre secciones de la página pública hay 80 px.

## 5. Componentes (clases)

| Componente | Clases |
|---|---|
| Botones | `.ad-btn` (secundario), `.primario`, `.peligro`, `.suave-peligro`, `.chico`, `.icono`, `.regresar` |
| Insignias | `.ad-etiqueta` (`.ok`, `.mal`, `.alerta`), `.cond-*` (condición del clima), `.ad-rol` (`.super`), `.ad-pill` (`.modo`) |
| Avisos | `.ad-alerta` con `success`, `error`, `warning` o `info`, más ícono y botón `.cerrar` |
| Formularios | `.campo`, `.unidad`, `.ayuda`, `.ad-campos`, `.ad-barra`, `.ad-archivo`, `.ad-num` (número de sección) |
| Tablas | `.ad-tabla-caja` + `.ad-tabla`; en la pública, `.table-wrap` |
| Cabecera | `.ad-header` (fila de marca y usuario) + `.ad-nav` (menú, con `.activo` y `aria-current`) |
| Página pública | `.actual`, `.actual-datos`, `.pub-info`, `.dias`/`.dia`/`.elegido`, `.lluvia`, `.horas`, `.pub-sin-datos`, `.pub-pie` |
| Íconos del clima | Macros `wx` y `flecha` de `weather_icons.html`; colores por clase (`.wx-sol`, `.wx-nube`, `.oscura`, `.wx-gotas`, `.wx-luna`, `.wx-niebla`…) |

## 6. Permisos en la interfaz

Antes de integrar la rama de roles, cada condición se escribe así, para que hoy se vea todo y después obedezca los permisos sin tocar nada:

```
{% if puede is not defined or puede.eliminar %} … {% endif %}
```

| Dónde | Permiso |
|---|---|
| Menú: Import · Administrators · Audit log · My profile | `importar` · `gestionar_admins` · `ver_bitacora` · `mi_perfil` |
| Lecturas: New reading · Import · Edit · eliminar | `agregar` · `importar` · `editar` · `eliminar` |
| Formulario: Delete reading | `eliminar` |
| Inicio: accesos rápidos y «See the full audit log» | `importar`, `gestionar_admins`, `ver_bitacora` |
| Insignia del rol (cabecera e inicio) | `rol_texto` |

Además:
- En el inicio, «Active administrators» solo se muestra si `admins is not none`.
- En **Administradores**: hay columna de rol, las filas `SUPERADMIN` no muestran «Disable»/«Enable», y los formularios están separados con `intent = create` e `intent = password`.

## 7. Pendiente después de integrar con la Persona B

- Rediseñar `registros.html` y `perfil.html` con estos componentes.
- Cambiar `href="/registros"` y `href="/account/profile"` (en `public.html` y `base.html`) por `url_for('registros_publicos')` y `url_for('perfil')`.
- Correr las pruebas (deben ser 149) y revisar el panel como administrador y como superadministrador.
