"""PASO 1 · Descargar datos climáticos reales desde Open-Meteo.

Qué hace: pide a Open-Meteo el clima hora por hora de cada ubicación de
config.py y guarda la respuesta original en etl/datos/crudos/.
No usa AWS ni cuesta créditos. Solo necesita internet.

Ejecutar desde la carpeta climate_portal_v3:
    .\\.venv\\Scripts\\python.exe etl\\paso1_descargar.py
"""
import json
import sys
import time
from datetime import datetime, timezone

import requests

import config
from comun import CRUDOS


def descargar(ubicacion, modelo):
    parametros = {
        "latitude": ubicacion["latitude"],
        "longitude": ubicacion["longitude"],
        "start_date": config.FECHA_INICIO,
        "end_date": config.FECHA_FIN,
        "hourly": ",".join(config.VARIABLES),
        "timezone": "GMT",
        "wind_speed_unit": "kmh",
    }
    if modelo:
        parametros["models"] = modelo
    respuesta = requests.get(config.API_URL, params=parametros, timeout=90)
    try:
        cuerpo = respuesta.json()
    except ValueError:
        raise RuntimeError(f"Respuesta no válida (HTTP {respuesta.status_code}).") from None
    if respuesta.status_code != 200 or cuerpo.get("error"):
        raise RuntimeError(f"Open-Meteo rechazó la petición: {cuerpo.get('reason', respuesta.status_code)}")
    return respuesta.text, cuerpo, respuesta.url


def faltantes(cuerpo):
    horas = cuerpo.get("hourly", {})
    total = len(horas.get("time", []))
    conteo = {v: sum(1 for x in horas.get(v, [None] * total) if x is None) for v in config.VARIABLES}
    return total, conteo


def main():
    CRUDOS.mkdir(parents=True, exist_ok=True)
    print(f"Descargando {config.FECHA_INICIO} a {config.FECHA_FIN} de {len(config.UBICACIONES)} ubicaciones...\n")
    resumen = []
    for ubicacion in config.UBICACIONES:
        lid = ubicacion["location_id"]
        mejor = None
        for modelo in config.MODELOS:
            try:
                texto, cuerpo, url = descargar(ubicacion, modelo)
            except (requests.RequestException, RuntimeError) as error:
                print(f"  {lid} · modelo {modelo or 'predeterminado'}: {error}")
                continue
            total, conteo = faltantes(cuerpo)
            peor = max(conteo.values()) / total if total else 1
            print(f"  {lid} · modelo {modelo or 'predeterminado'}: {total} horas, faltantes máx. {peor:.1%}")
            if mejor is None or peor < mejor["peor"]:
                mejor = dict(texto=texto, cuerpo=cuerpo, url=url, modelo=modelo or "predeterminado",
                             total=total, conteo=conteo, peor=peor)
            if peor <= config.MAX_FALTANTES:
                break
            time.sleep(1)
        if mejor is None:
            print(f"\nNo se pudo descargar {lid}. Revisa tu conexión a internet y vuelve a intentar.")
            sys.exit(1)
        (CRUDOS / f"{lid}.json").write_text(mejor["texto"], encoding="utf-8")
        meta = {
            "location_id": lid, "modelo": mejor["modelo"], "url": mejor["url"],
            "elevation": mejor["cuerpo"].get("elevation"), "horas": mejor["total"],
            "faltantes": mejor["conteo"], "descargado_utc": datetime.now(timezone.utc).isoformat(),
            "atribucion": config.API_ATRIBUCION,
        }
        (CRUDOS / f"{lid}.meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        resumen.append(meta)
        time.sleep(1)  # cortesía con el servicio gratuito

    print("\nResumen:")
    for meta in resumen:
        faltan = {k: v for k, v in meta["faltantes"].items() if v}
        print(f"  {meta['location_id']:<9} {meta['horas']} horas · modelo {meta['modelo']:<13} "
              f"· altitud {meta['elevation']} m · faltantes: {faltan or 'ninguno'}")
    print(f"\nListo. Archivos en {CRUDOS}")
    print("Siguiente: paso2_transformar.py")


if __name__ == "__main__":
    main()
