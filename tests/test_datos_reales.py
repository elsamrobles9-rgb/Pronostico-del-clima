"""Modo sin conexión con lecturas reales (python app.py --datos-reales): sin AWS ni internet."""
import random
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import create_app
from contract import dumps, normalize_record
from prediccion import demo
from services.analytics import DemoAnalytics, hora_desde_lectura, leer_lecturas
from services.endpoints import DemoClient, sample

FIN = datetime(2026, 11, 19, 6, tzinfo=timezone.utc)   # igual que los datos reales: terminan el 18/11 local


def lecturas_de_prueba(dias=40):
    """Lecturas con el mismo formato que escribe etl/paso2_transformar.py en lecturas.jsonl."""
    filas = []
    for h in demo.historia(dias=dias, hasta=FIN):
        registro = dict(sample(), source_id="SRC-OPENMETEO",
                        timestamp_utc=h["ts_utc"].strftime("%Y-%m-%dT%H:%MZ"),
                        temperature_c=h["temperature_c"], feels_like_c=h["feels_like_c"],
                        humidity_pct=h["humidity_pct"], precipitation_mm=h["precipitation_mm"],
                        wind_speed_kmh=h["wind_speed_kmh"], wind_direction=h["wind_direction"],
                        uv_index=h["uv_index"], cloud_cover_pct=h["cloud_cover_pct"],
                        condition_id=h["condition_id"], dew_point_c=round(h["temperature_c"] - 6, 1),
                        pressure_hpa=780.0)
        filas.append(normalize_record(registro))
    return filas


class DatosRealesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.lecturas = lecturas_de_prueba()
        self.ruta = Path(self.temp.name) / "lecturas.jsonl"
        revueltas = self.lecturas[:]
        random.Random(7).shuffle(revueltas)
        otra = dict(revueltas[0], location_id="LOC-GDL")
        with open(self.ruta, "w", encoding="utf-8", newline="\n") as archivo:
            archivo.write(dumps(otra) + "\n\n")
            for fila in revueltas:
                archivo.write(dumps(fila) + "\n")

    def test_lee_solo_cdmx_y_en_orden(self):
        leidas = leer_lecturas(self.ruta)
        self.assertEqual(len(leidas), len(self.lecturas))
        self.assertTrue(all(r["location_id"] == "LOC-CDMX" for r in leidas))
        horas = [hora_desde_lectura(r)["ts_utc"] for r in leidas]
        self.assertEqual(horas, sorted(horas))

    def test_archivo_sin_cdmx_da_error_claro(self):
        vacio = Path(self.temp.name) / "vacio.jsonl"
        vacio.write_text("\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            leer_lecturas(vacio)

    def test_pronostico_con_las_lecturas_del_archivo(self):
        analytics = DemoAnalytics.desde_lecturas(leer_lecturas(self.ruta))
        self.assertTrue(analytics.datos_reales)
        pagina = analytics.pagina(ahora_local=datetime(2026, 11, 20, 10, 30))
        self.assertEqual(len(pagina["dias"]), 7)
        self.assertTrue(pagina["horas"])
        ultima = self.lecturas[-1]
        self.assertEqual(analytics.ultima_lectura()["temperature_c"], float(ultima["temperature_c"]))
        self.assertEqual(pagina["actual"]["temperatura"], round(float(ultima["temperature_c"])))

    def test_records_tiene_todas_las_lecturas(self):
        cliente = DemoClient(records=leer_lecturas(self.ruta))
        self.assertEqual((cliente.loaded, cliente.rejected), (len(self.lecturas), 0))
        vistas, cursor = 0, ""
        while True:
            filas, cursor = cliente.list_records(cursor=cursor)
            vistas += len(filas)
            if not cursor:
                break
        self.assertEqual(vistas, len(self.lecturas))
        una = self.lecturas[100]
        leida = cliente.get({"location_id": una["location_id"], "timestamp_utc": una["timestamp_utc"]})
        self.assertEqual(leida.get("version"), 1)

    def test_editar_cambia_el_pronostico_como_sp_upsert(self):
        analytics = DemoAnalytics.desde_lecturas(leer_lecturas(self.ruta))
        antes = analytics.modelo()
        editada = dict(self.lecturas[-1], temperature_c=float(self.lecturas[-1]["temperature_c"]) + 10)
        analytics.upsert([editada], "edicion", [], [])
        self.assertEqual(analytics.ultima_lectura()["temperature_c"], editada["temperature_c"])
        self.assertEqual(len(analytics.horas), len(self.lecturas))
        self.assertIsNot(analytics.modelo(), antes)

    def test_demo_normal_no_cambia(self):
        analytics = DemoAnalytics(demo.historia(dias=60, hasta=FIN))
        n = len(analytics.horas)
        analytics.upsert([self.lecturas[-1]], "alta", [], [])
        self.assertFalse(analytics.datos_reales)
        self.assertEqual(len(analytics.horas), n)

    def test_portal_avisa_que_esta_sin_conexion(self):
        lecturas = leer_lecturas(self.ruta)
        app = create_app(DemoClient(records=lecturas), "demo", auth_path=self.temp.name + "/local.sqlite3",
                         analytics=DemoAnalytics.desde_lecturas(lecturas))
        app.testing = True
        web = app.test_client()
        publica = web.get("/").get_data(as_text=True)
        self.assertIn("Modo sin conexión", publica)
        self.assertIn(f"{len(lecturas):,} lecturas reales", publica)
        self.assertNotIn("datos sintéticos", publica)
        login = web.get("/login").get_data(as_text=True)
        self.assertIn("Offline · real data", login)


if __name__ == "__main__":
    unittest.main()
