#!/usr/bin/env python3
"""
Mini servidor para descargar los resultados del piloto desde el navegador.
Protegido con usuario y contraseña (DESCARGA_USUARIO / DESCARGA_PASSWORD).
Solo sirve resultados.csv y resultados.jsonl; nunca las miniaturas ni el .env.
"""
import base64
import hmac
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(os.getenv("DATA_DIR", "datos"))
USUARIO = os.getenv("DESCARGA_USUARIO", "alegria")
PASSWORD = os.getenv("DESCARGA_PASSWORD", "")
PUERTO = int(os.getenv("PORT", "8080"))
# Etiquetado nocturno de fotos nuevas (python etiquetar.py nocturno). Después de la actualización de box-oportunidades
# (03:00), que rehace la selección de fotos. Se desactiva con NOCTURNO=no.
NOCTURNO = os.getenv("NOCTURNO", "si").lower() not in ("no", "0", "false") and bool(os.getenv("ANTHROPIC_API_KEY"))
HORA = os.getenv("NOCTURNO_HORA", "05:30")
ZONA = ZoneInfo(os.getenv("NOCTURNO_ZONA", "Europe/Madrid"))
LOG_NOCHE = DATA / "noche" / "log.txt"
ULTIMA = DATA / ".ultimo_nocturno"

TIPOS = {"resultados.csv": "text/csv; charset=utf-8",
         "flota_campanas.csv": "text/csv; charset=utf-8",
         "resultados_flota.csv": "text/csv; charset=utf-8",
         "log.txt": "text/plain; charset=utf-8",
         "indice.json": "application/json; charset=utf-8",
         "resultados.jsonl": "application/x-ndjson; charset=utf-8"}


def disponibles():
    """resultados de datos/ y de sus subcarpetas (una por tanda: datos/2025/...)."""
    salida = {}
    for nombre, tipo in TIPOS.items():
        if (DATA / nombre).exists():
            salida[f"/{nombre}"] = (DATA / nombre, tipo)
        for sub in sorted(p for p in DATA.iterdir() if p.is_dir()):
            if (sub / nombre).exists() and sub.name.replace("-", "").replace("_", "").isalnum():
                salida[f"/{sub.name}/{nombre}"] = (sub / nombre, tipo)
    return salida


class Manejador(BaseHTTPRequestHandler):
    def _texto(self, codigo, texto, tipo="text/plain; charset=utf-8"):
        cuerpo = texto.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _autorizado(self):
        esperado = "Basic " + base64.b64encode(f"{USUARIO}:{PASSWORD}".encode()).decode()
        return hmac.compare_digest(self.headers.get("Authorization", ""), esperado)

    def do_GET(self):
        if not PASSWORD:
            return self._texto(503, "Descargas desactivadas: define DESCARGA_PASSWORD en el entorno.")
        if not self._autorizado():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="etiquetado-fotos"')
            self.end_headers()
            return
        archivos = disponibles()
        if self.path == "/":
            filas = [f'<li><a href="{ruta}">{ruta.lstrip("/")}</a> · {p.stat().st_size / 1024:.0f} KB</li>'
                     for ruta, (p, _) in archivos.items()] or ["<li>Todavía no hay resultados.</li>"]
            html = ("<!doctype html><meta charset='utf-8'><title>Etiquetado de fotos</title>"
                    "<body style='font-family:sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem'>"
                    "<h1>Piloto de etiquetado de fotos</h1><ul>" + "".join(filas) + "</ul></body>")
            return self._texto(200, html, "text/html; charset=utf-8")
        if self.path in archivos:
            p, tipo = archivos[self.path]
            nombre = self.path.strip("/").replace("/", "_")
            cuerpo = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("X-Robots-Tag", "noindex, nofollow")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(cuerpo)
            return
        self._texto(404, "No encontrado")


def programador():
    """Cada medio minuto mira la hora; a NOCTURNO_HORA lanza el etiquetado nocturno una vez al día."""
    while True:
        try:
            ahora = datetime.now(ZONA)
            hoy = ahora.strftime("%Y-%m-%d")
            hecha = ULTIMA.read_text().strip() if ULTIMA.exists() else ""
            if ahora.strftime("%H:%M") >= HORA and hecha != hoy:
                ULTIMA.write_text(hoy)
                LOG_NOCHE.parent.mkdir(parents=True, exist_ok=True)
                if LOG_NOCHE.exists() and LOG_NOCHE.stat().st_size > 5_000_000:
                    LOG_NOCHE.write_bytes(LOG_NOCHE.read_bytes()[-2_000_000:])
                with LOG_NOCHE.open("a", encoding="utf-8") as log:
                    log.write(f"\n--- lanzado {ahora:%Y-%m-%d %H:%M} ({ZONA.key}) ---\n")
                    log.flush()
                    subprocess.run([sys.executable, "etiquetar.py", "nocturno"], stdout=log, stderr=subprocess.STDOUT,
                                   cwd=Path(__file__).parent, check=False,
                                   env={**os.environ, "DATA_DIR": str(DATA), "PYTHONUNBUFFERED": "1"})
        except Exception as e:  # noqa: BLE001 — el programador no debe tumbar el servidor
            print(f"[nocturno] error: {e}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"Servidor de descargas en el puerto {PUERTO}")
    if NOCTURNO:
        if not ULTIMA.exists():   # recién instalado: el primero, esta noche
            ULTIMA.write_text(datetime.now(ZONA).strftime("%Y-%m-%d"))
        threading.Thread(target=programador, daemon=True).start()
        print(f"Etiquetado nocturno cada día a las {HORA} ({ZONA.key})")
    else:
        print("Etiquetado nocturno desactivado (NOCTURNO=no o falta ANTHROPIC_API_KEY)")
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()
