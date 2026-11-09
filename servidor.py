#!/usr/bin/env python3
"""
Mini servidor para descargar los resultados del piloto desde el navegador.
Protegido con usuario y contraseña (DESCARGA_USUARIO / DESCARGA_PASSWORD).
Solo sirve resultados.csv y resultados.jsonl; nunca las miniaturas ni el .env.
"""
import base64
import hmac
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA = Path(os.getenv("DATA_DIR", "datos"))
USUARIO = os.getenv("DESCARGA_USUARIO", "alegria")
PASSWORD = os.getenv("DESCARGA_PASSWORD", "")
PUERTO = int(os.getenv("PORT", "8080"))

ARCHIVOS = {
    "/resultados.csv": ("resultados.csv", "text/csv; charset=utf-8"),
    "/resultados.jsonl": ("resultados.jsonl", "application/x-ndjson; charset=utf-8"),
}


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
        if self.path == "/":
            filas = []
            for ruta, (nombre, _) in ARCHIVOS.items():
                p = DATA / nombre
                estado = f"{p.stat().st_size / 1024:.0f} KB" if p.exists() else "todavía no existe"
                filas.append(f'<li><a href="{ruta}">{nombre}</a> · {estado}</li>')
            html = ("<!doctype html><meta charset='utf-8'><title>Etiquetado de fotos</title>"
                    "<body style='font-family:sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem'>"
                    "<h1>Piloto de etiquetado de fotos</h1><ul>" + "".join(filas) + "</ul></body>")
            return self._texto(200, html, "text/html; charset=utf-8")
        if self.path in ARCHIVOS:
            nombre, tipo = ARCHIVOS[self.path]
            p = DATA / nombre
            if not p.exists():
                return self._texto(404, "Todavía no hay resultados. Ejecuta: python etiquetar.py recoger")
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


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"Servidor de descargas en el puerto {PUERTO}")
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()
