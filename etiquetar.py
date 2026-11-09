#!/usr/bin/env python3
"""
Piloto de etiquetado de fotos de Box con la API de Claude (Batch API).
alegria-activity

Uso:
  python etiquetar.py listar          Recorre la carpeta de Box y guarda datos/manifest.json
  python etiquetar.py enviar [--si]   Prepara miniaturas, estima el coste y crea un lote por modelo
  python etiquetar.py estado          Muestra el estado de los lotes
  python etiquetar.py recoger         Descarga resultados y genera datos/resultados.csv
  python etiquetar.py todo [--si]     Todo seguido, esperando a que terminen los lotes

Solo LEE de Box. No escribe, mueve ni borra nada allí.
"""
import argparse
import base64
import csv
import io
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

load_dotenv()

# ---------------------------------------------------------------- configuración
API = "https://api.box.com/2.0"
CARPETA_RAIZ = os.getenv("BOX_FOLDER_ID", "379464633435")  # 15. Web 2025 / Fotos y videos / Fotos
EXCLUIR = {x.strip() for x in os.getenv("BOX_EXCLUDE_IDS", "").split(",") if x.strip()}
MODELOS = [m.strip() for m in os.getenv("MODELOS", "claude-haiku-5-5,claude-sonnet-5-5").split(",") if m.strip()]
MAX_FOTOS = int(os.getenv("MAX_FOTOS", "250"))
LADO = int(os.getenv("LADO_MAX_PX", "1024"))
INCLUIR_RUTA = os.getenv("INCLUIR_RUTA", "false").lower() == "true"

# Precios Batch en USD por millón de tokens (entrada, salida). Orientativos: revísalos en la Console.
PRECIOS_BATCH = {
    "claude-haiku-5-5": (0.05, 0.25),
    "claude-sonnet-5-5": (1.00, 5.00),
    "claude-opus-5-5": (2.00, 10.00),
}

DATA = Path(os.getenv("DATA_DIR", "datos"))
MANIFEST = DATA / "manifest.json"
LOTES = DATA / "lotes.json"
CACHE = DATA / "miniaturas"
RESULTADOS_CSV = DATA / "resultados.csv"
RESULTADOS_JSONL = DATA / "resultados.jsonl"

EXTENSIONES = {"jpg", "jpeg", "png", "webp", "heic", "heif", "tif", "tiff"}

FLOTA = ["13,80", "Airstream", "CajaMar", "Camión rígido", "Cotazero",
         "Furgón", "Plataformas", "Semirremolque", "Set TV"]

LINEAS = ["Roadshow", "Acciones pedagógicas y sensibilización", "Marca y patrocinio",
          "Promoción y producto", "Manufacturing", "Agencia", "otro"]

# ---------------------------------------------------------------- prompt y esquema
SYSTEM = f"""Eres documentalista de alegria-activity, una agencia de marketing experiencial que hace campañas, \
roadshows y exposiciones itinerantes en vehículos de flota propia. Catalogas fotos del archivo de la agencia \
para poder encontrarlas después al preparar propuestas comerciales y la web.

Rellena SIEMPRE la ficha con la herramienta guardar_ficha. Reglas:

1. Describe solo lo que se ve. Si dudas, usa "desconocido" o confianza "baja". Nunca inventes.
2. Vehículo de la flota: {", ".join(FLOTA)}. Elige uno de ellos solo si lo reconoces con claridad \
(la Airstream es la caravana de aluminio pulido de formas redondeadas). Si hay un vehículo pero no puedes \
asegurar cuál de la flota es, usa "otro vehículo" o "desconocido" y rellena bien tipo_vehiculo.
3. Marca o cliente: solo si un logotipo o nombre se lee en la imagen. El nombre del archivo puede dar una pista; \
si la marca la sacas del nombre del archivo y no de la imagen, indica fuente_marca = "nombre de archivo".
4. Personas: NO identifiques a nadie ni describas rasgos físicos de personas concretas. Indica solo cuántas hay \
y qué están haciendo.
5. posibles_menores: "sí" si aparecen niños o adolescentes; "dudoso" si no se puede descartar; "no" en otro caso.
6. linea_servicio: la línea de negocio que mejor ilustra la foto.
7. Descripción (1-2 frases) y palabras clave (5-10) en castellano, pensadas para buscar después: vehículo, \
espacio (interior/exterior), actividad, tipo de lugar (plaza, recinto ferial, colegio, nave...) y elementos \
(pantallas, mostrador, carpa, photocall, gafas de realidad virtual...).
8. apta_propuesta: true si la foto tiene calidad y encuadre suficientes para enseñarla a un cliente.
"""

HERRAMIENTA = {
    "name": "guardar_ficha",
    "description": "Guarda la ficha de catalogación de la foto.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vehiculo": {"type": "string", "enum": FLOTA + ["otro vehículo", "sin vehículo", "desconocido"]},
            "tipo_vehiculo": {"type": "string", "enum": [
                "caravana", "furgoneta", "camión", "semirremolque/tráiler", "plataforma/escenario móvil",
                "coche", "otro", "ninguno"]},
            "plano": {"type": "string", "enum": [
                "exterior del vehículo", "interior del vehículo", "evento con público", "montaje o producción",
                "detalle", "equipo u oficina", "otro"]},
            "linea_servicio": {"type": "string", "enum": LINEAS},
            "marca_visible": {"type": "string", "description": "Marca o cliente visible; vacío si ninguna."},
            "fuente_marca": {"type": "string", "enum": ["imagen", "nombre de archivo", "ninguna"]},
            "personas": {"type": "string", "enum": ["ninguna", "1-3", "4-15", "más de 15"]},
            "posibles_menores": {"type": "string", "enum": ["sí", "no", "dudoso"]},
            "calidad": {"type": "string", "enum": ["alta", "media", "baja"]},
            "apta_propuesta": {"type": "boolean"},
            "descripcion": {"type": "string"},
            "palabras_clave": {"type": "array", "items": {"type": "string"}},
            "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
        },
        "required": ["vehiculo", "tipo_vehiculo", "plano", "linea_servicio", "marca_visible", "fuente_marca",
                     "personas", "posibles_menores", "calidad", "apta_propuesta", "descripcion",
                     "palabras_clave", "confianza"],
    },
}


# ---------------------------------------------------------------- Box (solo lectura)
_token = {"v": None, "exp": 0.0}


def entorno(nombre):
    valor = os.getenv(nombre)
    if not valor:
        sys.exit(f"Falta la variable de entorno {nombre} (mira .env.example).")
    return valor


def box_token():
    if _token["v"] and time.time() < _token["exp"] - 60:
        return _token["v"]
    r = requests.post("https://api.box.com/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_id": entorno("BOX_CLIENT_ID"),
        "client_secret": entorno("BOX_CLIENT_SECRET"),
        "box_subject_type": os.getenv("BOX_SUBJECT_TYPE", "enterprise"),
        "box_subject_id": entorno("BOX_SUBJECT_ID"),
    }, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Box no ha dado token ({r.status_code}): {r.text[:300]}")
    j = r.json()
    _token.update(v=j["access_token"], exp=time.time() + j.get("expires_in", 3600))
    return _token["v"]


def box_get(url, params=None, headers=None):
    r = None
    for intento in range(6):
        h = {"Authorization": f"Bearer {box_token()}"}
        if headers:
            h.update(headers)
        r = requests.get(url, params=params, headers=h, timeout=120)
        if r.status_code == 401:
            _token["v"] = None
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(int(r.headers.get("Retry-After", 2 ** intento)))
            continue
        return r
    return r


def listar_carpeta(folder_id, ruta, salida):
    offset = 0
    while True:
        r = box_get(f"{API}/folders/{folder_id}/items",
                    params={"fields": "id,type,name,size,extension", "limit": 1000, "offset": offset})
        r.raise_for_status()
        j = r.json()
        for it in j["entries"]:
            if it["id"] in EXCLUIR:
                continue
            if it["type"] == "folder":
                listar_carpeta(it["id"], f"{ruta}/{it['name']}" if ruta else it["name"], salida)
            elif it["type"] == "file" and (it.get("extension") or "").lower() in EXTENSIONES:
                salida.append({"id": it["id"], "nombre": it["name"], "ruta": ruta, "tamano": it.get("size", 0)})
        offset += len(j["entries"])
        if not j["entries"] or offset >= j.get("total_count", 0):
            break


def box_representacion(file_id):
    """Pide a Box una versión JPG de 1024 px para no descargar originales de 20-30 MB."""
    r = box_get(f"{API}/files/{file_id}", params={"fields": "representations"},
                headers={"x-rep-hints": "[jpg?dimensions=1024x1024]"})
    if r.status_code != 200:
        return None
    entradas = r.json().get("representations", {}).get("entries", [])
    if not entradas:
        return None
    rep = entradas[0]
    for _ in range(15):
        estado = rep.get("status", {}).get("state")
        if estado in ("success", "viewable"):
            url = rep["content"]["url_template"].replace("{+asset_path}", "")
            c = box_get(url)
            return c.content if c.status_code == 200 else None
        if estado == "error":
            return None
        time.sleep(2)
        info = box_get(rep["info"]["url"])
        if info.status_code != 200:
            return None
        rep = info.json()
    return None


def miniatura(foto):
    CACHE.mkdir(parents=True, exist_ok=True)
    destino = CACHE / f"{foto['id']}.jpg"
    if destino.exists():
        return destino.read_bytes()
    datos = box_representacion(foto["id"])
    if datos is None:
        r = box_get(f"{API}/files/{foto['id']}/content")
        r.raise_for_status()
        datos = r.content
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(datos))).convert("RGB")
    img.thumbnail((LADO, LADO))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    destino.write_bytes(buf.getvalue())
    return buf.getvalue()


# ---------------------------------------------------------------- utilidades
def leer_json(p, defecto=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else defecto


def escribir_json(p, datos):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def tokens_imagen(jpg):
    w, h = Image.open(io.BytesIO(jpg)).size
    return int(w * h / 750)


def precio(modelo):
    return PRECIOS_BATCH.get(modelo, (1.0, 5.0))


# ---------------------------------------------------------------- comandos
def cmd_listar(_args):
    raiz = box_get(f"{API}/folders/{CARPETA_RAIZ}", params={"fields": "name,path_collection"})
    raiz.raise_for_status()
    print(f"Carpeta: {raiz.json()['name']} ({CARPETA_RAIZ})")
    fotos = []
    listar_carpeta(CARPETA_RAIZ, "", fotos)
    total = len(fotos)
    fotos = fotos[:MAX_FOTOS]
    escribir_json(MANIFEST, fotos)
    print(f"Imágenes encontradas: {total} · en el piloto: {len(fotos)} (MAX_FOTOS={MAX_FOTOS})")
    print(f"Tamaño original: {sum(f['tamano'] for f in fotos) / 1e9:.2f} GB")
    for carpeta, n in Counter((f["ruta"].split("/")[0] or "(raíz)") for f in fotos).most_common():
        print(f"  {n:4d}  {carpeta}")


def cmd_enviar(args):
    fotos = leer_json(MANIFEST)
    if not fotos:
        sys.exit("No hay manifest. Ejecuta primero: python etiquetar.py listar")
    if leer_json(LOTES) and not args.forzar:
        sys.exit("Ya hay lotes enviados (datos/lotes.json). Usa --forzar para crear otros nuevos.")

    print(f"Preparando {len(fotos)} miniaturas (se guardan en {CACHE}/)...")
    peticiones_base, tokens_img, fallos = [], 0, []
    for i, foto in enumerate(fotos, 1):
        try:
            jpg = miniatura(foto)
        except Exception as e:  # noqa: BLE001
            fallos.append((foto["nombre"], str(e)[:120]))
            continue
        tokens_img += tokens_imagen(jpg)
        texto = f"Archivo: {foto['nombre']}"
        if INCLUIR_RUTA and foto["ruta"]:
            texto += f"\nCarpeta: {foto['ruta']}"
        peticiones_base.append((foto["id"], [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(jpg).decode()}},
            {"type": "text", "text": texto},
        ]))
        if i % 25 == 0:
            print(f"  {i}/{len(fotos)}")
    for nombre, err in fallos:
        print(f"  ⚠ no se pudo preparar {nombre}: {err}")
    if not peticiones_base:
        sys.exit("No se ha podido preparar ninguna imagen.")

    n = len(peticiones_base)
    tokens_fijos = 1500  # instrucciones + esquema, aprox.
    print("\nCoste estimado (precios Batch orientativos):")
    total = 0.0
    for modelo in MODELOS:
        pin, pout = precio(modelo)
        coste = ((tokens_img + n * tokens_fijos) * pin + n * 400 * pout) / 1e6
        total += coste
        print(f"  {modelo:22s} {n} fotos ≈ {coste:.2f} $")
    print(f"  TOTAL ≈ {total:.2f} $")
    if not args.si and input("\n¿Enviar los lotes? [s/N] ").strip().lower() not in ("s", "si", "sí"):
        sys.exit("Cancelado. No se ha enviado nada.")

    cliente = anthropic.Anthropic(api_key=entorno("ANTHROPIC_API_KEY"))
    lotes = {}
    for idx, modelo in enumerate(MODELOS):
        peticiones = [{
            "custom_id": f"m{idx}_{file_id}",
            "params": {
                "model": modelo,
                "max_tokens": 1024,
                "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                "tools": [HERRAMIENTA],
                "tool_choice": {"type": "tool", "name": "guardar_ficha"},
                "messages": [{"role": "user", "content": contenido}],
            },
        } for file_id, contenido in peticiones_base]
        lote = cliente.messages.batches.create(requests=peticiones)
        lotes[modelo] = lote.id
        print(f"Lote enviado para {modelo}: {lote.id}")
    escribir_json(LOTES, lotes)
    print("Los lotes suelen tardar de minutos a unas horas (máximo 24 h). Consulta con: python etiquetar.py estado")


def estados(cliente, lotes):
    res = {}
    for modelo, lote_id in lotes.items():
        b = cliente.messages.batches.retrieve(lote_id)
        res[modelo] = b
        c = b.request_counts
        print(f"  {modelo:22s} {b.processing_status:12s} ok={c.succeeded} error={c.errored} "
              f"en curso={c.processing} caducadas={c.expired}")
    return res


def cmd_estado(_args):
    lotes = leer_json(LOTES)
    if not lotes:
        sys.exit("No hay lotes enviados.")
    estados(anthropic.Anthropic(api_key=entorno("ANTHROPIC_API_KEY")), lotes)


def cmd_recoger(_args):
    lotes = leer_json(LOTES)
    fotos = {f["id"]: f for f in (leer_json(MANIFEST) or [])}
    if not lotes:
        sys.exit("No hay lotes enviados.")
    cliente = anthropic.Anthropic(api_key=entorno("ANTHROPIC_API_KEY"))
    st = estados(cliente, lotes)
    if any(b.processing_status != "ended" for b in st.values()):
        sys.exit("Todavía hay lotes en curso. Vuelve a probar más tarde.")

    campos = ["file_id", "nombre", "carpeta", "enlace_box", "modelo", "vehiculo", "tipo_vehiculo", "plano",
              "linea_servicio", "marca_visible", "fuente_marca", "personas", "posibles_menores", "calidad",
              "apta_propuesta", "descripcion", "palabras_clave", "confianza", "uso_autorizado", "coste_usd",
              "revision_ok", "notas_revision"]
    filas, crudo = [], []
    coste_modelo, errores = Counter(), Counter()
    for modelo, lote_id in lotes.items():
        pin, pout = precio(modelo)
        for r in cliente.messages.batches.results(lote_id):
            file_id = r.custom_id.split("_", 1)[1]
            foto = fotos.get(file_id, {"nombre": "?", "ruta": ""})
            if r.result.type != "succeeded":
                errores[modelo] += 1
                crudo.append({"modelo": modelo, "file_id": file_id, "error": r.result.type})
                continue
            msg = r.result.message
            ficha = next((b.input for b in msg.content if b.type == "tool_use"), {})
            u = msg.usage
            tin = (u.input_tokens + (u.cache_creation_input_tokens or 0) * 1.25
                   + (u.cache_read_input_tokens or 0) * 0.1)
            coste = (tin * pin + u.output_tokens * pout) / 1e6
            coste_modelo[modelo] += coste
            crudo.append({"modelo": modelo, "file_id": file_id, "ficha": ficha})
            filas.append({
                "file_id": file_id, "nombre": foto["nombre"], "carpeta": foto["ruta"],
                "enlace_box": f"https://app.box.com/file/{file_id}", "modelo": modelo,
                **{k: ficha.get(k, "") for k in HERRAMIENTA["input_schema"]["properties"]},
                "palabras_clave": ", ".join(ficha.get("palabras_clave", [])),
                "uso_autorizado": "publicada en web" if CARPETA_RAIZ == "379464633435" else "pendiente",
                "coste_usd": f"{coste:.5f}", "revision_ok": "", "notas_revision": "",
            })

    filas.sort(key=lambda f: (f["carpeta"], f["nombre"], f["modelo"]))
    with RESULTADOS_CSV.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    with RESULTADOS_JSONL.open("w", encoding="utf-8") as fh:
        for c in crudo:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    # ---- resumen automático
    print(f"\nResultados: {RESULTADOS_CSV} ({len(filas)} filas)")
    for modelo in lotes:
        print(f"  {modelo:22s} coste real ≈ {coste_modelo[modelo]:.3f} $   errores: {errores[modelo]}")
    por_foto = {}
    for f in filas:
        por_foto.setdefault(f["file_id"], {})[f["modelo"]] = f
    if len(MODELOS) >= 2:
        a, b = MODELOS[0], MODELOS[1]
        pares = [d for d in por_foto.values() if a in d and b in d]
        if pares:
            print(f"\nCoincidencia {a} vs {b} ({len(pares)} fotos):")
            for campo in ("vehiculo", "plano", "linea_servicio", "posibles_menores", "apta_propuesta"):
                igual = sum(str(d[a][campo]) == str(d[b][campo]) for d in pares)
                print(f"  {campo:18s} {igual / len(pares):5.0%}")
    print("\nAcierto de línea de servicio frente a la carpeta (solo carpetas con nombre de línea):")
    for modelo in lotes:
        evaluables = [f for f in filas if f["modelo"] == modelo and f["carpeta"].split("/")[0] in LINEAS]
        if evaluables:
            ok = sum(f["linea_servicio"] == f["carpeta"].split("/")[0] for f in evaluables)
            print(f"  {modelo:22s} {ok}/{len(evaluables)} = {ok / len(evaluables):.0%}")
    menores = {f["nombre"] for f in filas if f["posibles_menores"] in ("sí", "dudoso")}
    if menores:
        print(f"\nFotos marcadas con posibles menores ({len(menores)}): revisar antes de usarlas en material comercial.")


def cmd_todo(args):
    if not MANIFEST.exists():
        cmd_listar(args)
    if not leer_json(LOTES) or args.forzar:
        cmd_enviar(args)
    cliente = anthropic.Anthropic(api_key=entorno("ANTHROPIC_API_KEY"))
    while True:
        print(time.strftime("%H:%M:%S"), "estado de los lotes:")
        if all(b.processing_status == "ended" for b in estados(cliente, leer_json(LOTES)).values()):
            break
        time.sleep(120)
    cmd_recoger(args)


def main():
    p = argparse.ArgumentParser(description="Piloto de etiquetado de fotos de Box con Claude")
    p.add_argument("comando", choices=["listar", "enviar", "estado", "recoger", "todo"])
    p.add_argument("--si", action="store_true", help="no pedir confirmación antes de enviar")
    p.add_argument("--forzar", action="store_true", help="crear lotes nuevos aunque ya existan")
    args = p.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    {"listar": cmd_listar, "enviar": cmd_enviar, "estado": cmd_estado,
     "recoger": cmd_recoger, "todo": cmd_todo}[args.comando](args)


if __name__ == "__main__":
    main()
