#!/usr/bin/env python3
"""
Piloto de etiquetado de fotos de Box con la API de Claude (Batch API) · versión 3
alegria-activity

Uso:
  python etiquetar.py listar                 Recorre la carpeta de Box y guarda datos/manifest.json
                                             (agrupa las fotos repetidas en varias carpetas)
  python etiquetar.py enviar [--si]          Prepara miniaturas, estima el coste y crea un lote por modelo
         [--modelos m1,m2] [--forzar]        (solo esos modelos / sustituir lotes existentes)
  python etiquetar.py estado                 Muestra el estado de los lotes
  python etiquetar.py recoger                Descarga resultados y genera datos/resultados.csv
  python etiquetar.py todo [--si]            Todo seguido, esperando a que terminen los lotes

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
from concurrent.futures import ThreadPoolExecutor
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

VERSION = "v3"

# ---------------------------------------------------------------- configuración
API = "https://api.box.com/2.0"
CARPETA_RAIZ = os.getenv("BOX_FOLDER_ID", "379464633435")  # 15. Web 2025 / Fotos y videos / Fotos
EXCLUIR = {x.strip() for x in os.getenv("BOX_EXCLUDE_IDS", "").split(",") if x.strip()}
MODELOS = [m.strip() for m in os.getenv("MODELOS", "claude-haiku-5-5,claude-sonnet-5-5").split(",") if m.strip()]
MAX_FOTOS = int(os.getenv("MAX_FOTOS", "250"))
LADO = int(os.getenv("LADO_MAX_PX", "1024"))
LADO_REF = int(os.getenv("LADO_REF_PX", "512"))
INCLUIR_RUTA = os.getenv("INCLUIR_RUTA", "false").lower() == "true"
USAR_REFERENCIAS = os.getenv("USAR_REFERENCIAS", "true").lower() == "true"
HILOS = int(os.getenv("HILOS", "6"))                       # descargas de miniaturas en paralelo
MAX_MB_LOTE = int(os.getenv("MAX_MB_LOTE", "180"))         # la Batch API admite hasta 256 MB por lote
MAX_PETICIONES_LOTE = int(os.getenv("MAX_PETICIONES_LOTE", "5000"))

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

# Una foto exterior de cada vehículo, del "Maestro vehículos (Claude)" en Box (Cotazero solo tiene planos).
REFERENCIAS = [
    ("Airstream", "2400251591808", "remolque compacto rotulado que se abre por un lateral como escaparate, con rampa"),
    ("Furgón", "2400253604413", "furgoneta grande con carrocería tipo caja, puerta lateral, escalera y toldo"),
    ("Camión rígido", "2400250967290", "camión rígido con caja expositiva de laterales acristalados"),
    ("Semirremolque", "2400249539182", "tráiler de gran tamaño con laterales expandibles"),
    ("13,80", "2400262689058", "vehículo de 13,80 m con aspecto de autocar"),
    ("CajaMar", "2400551454784", "camión carrozado con escalera y rampa de acceso lateral"),
    ("Plataformas", "2400244907579", "módulo-remolque blanco grande tipo sala, con ventanas"),
    ("Set TV", "2400249995707", "módulo plató de TV con terraza superior"),
]

LINEAS = ["Roadshow", "Acciones pedagógicas y sensibilización", "Marca y patrocinio",
          "Promoción y producto", "Manufacturing", "Agencia"]

DEFINICIONES = """\
- Roadshow: giras itinerantes con un vehículo que recorre varias localidades.
- Acciones pedagógicas y sensibilización: talleres, exposiciones y actividades educativas o de concienciación \
(salud, prevención, ciencia, medioambiente, alimentación...).
- Marca y patrocinio: activaciones de marca, hospitality y presencia en eventos deportivos o patrocinios.
- Promoción y producto: lanzamientos, sampling y demostraciones de producto.
- Manufacturing: diseño y fabricación de unidades móviles a medida para clientes (sanitarias, oficinas o \
sucursales móviles, puestos de mando, hospitality, motorhomes, bibliotecas...), aunque no haya evento ni público.
- Agencia: el equipo de alegria-activity (retratos del personal), sus oficinas e instalaciones."""

# ---------------------------------------------------------------- prompt y esquema
SYSTEM = f"""Eres documentalista de alegria-activity, una agencia de marketing experiencial que hace campañas, \
roadshows y exposiciones itinerantes en vehículos de flota propia, y que además fabrica unidades móviles a \
medida para clientes. Catalogas fotos del archivo de la agencia para poder encontrarlas después al preparar \
propuestas comerciales y la web.

Usa SIEMPRE la herramienta guardar_ficha, una sola vez por foto. Usa exactamente los valores permitidos de cada \
lista, sin inventar otros. Reglas:

1. Describe solo lo que se ve. Si dudas, usa "desconocido" o confianza "baja". Nunca inventes.
2. Vehículo de la flota ({", ".join(FLOTA)}): al principio del mensaje tienes una foto de referencia de cada \
uno (salvo Cotazero). Elige uno de la flota solo si coincide claramente con su referencia en forma y estructura \
(la rotulación cambia en cada campaña, no te fíes de los colores). Muchas fotos son de unidades fabricadas para \
clientes, que NO son de la flota: en ese caso usa "otro vehículo". Si no hay vehículo, "sin vehículo".
3. Marca: el nombre corto de la marca o cliente principal (p. ej. "Repsol", "Fundación DISA"), sin frases. \
Prioridad: lo que se lee en la imagen; si no se lee nada, el nombre de la carpeta (si te la doy, suele ser el \
cliente); si no, el nombre del archivo. Indica de dónde la sacas en fuente_marca. Vacío si no hay ninguna.
4. Personas: NO identifiques a nadie ni describas rasgos físicos de personas concretas. Indica solo cuántas hay \
y qué están haciendo.
5. posibles_menores: "sí" si aparecen niños o adolescentes; "dudoso" si no se puede descartar; "no" en otro caso.
6. Líneas de servicio (puedes marcar de 1 a 3 si la foto sirve para varias; la primera es la principal):
{DEFINICIONES}
7. Descripción (1-2 frases) y palabras clave (5-10) en castellano, pensadas para buscar después: vehículo, \
espacio (interior/exterior), actividad, tipo de lugar (plaza, recinto ferial, colegio, nave...) y elementos \
(pantallas, mostrador, carpa, photocall, gafas de realidad virtual...).
8. apta_propuesta: true si la foto tiene calidad y encuadre suficientes para enseñarla a un cliente.
"""

TIPOS_VEHICULO = ["caravana", "remolque", "furgoneta", "camión", "semirremolque/tráiler", "autobús/autocar",
                  "plataforma/escenario móvil", "coche", "otro", "ninguno"]

HERRAMIENTA = {
    "name": "guardar_ficha",
    "description": "Guarda la ficha de catalogación de la foto.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vehiculo": {"type": "string", "enum": FLOTA + ["otro vehículo", "sin vehículo", "desconocido"]},
            "tipo_vehiculo": {"type": "string", "enum": TIPOS_VEHICULO},
            "plano": {"type": "string", "enum": [
                "exterior del vehículo", "interior del vehículo", "evento con público", "montaje o producción",
                "detalle", "retrato o equipo", "otro"]},
            "lineas_servicio": {"type": "array", "minItems": 1, "maxItems": 3,
                                "items": {"type": "string", "enum": LINEAS}},
            "marca_visible": {"type": "string", "description": "Nombre corto de la marca; vacío si ninguna."},
            "fuente_marca": {"type": "string", "enum": ["imagen", "carpeta", "nombre de archivo", "ninguna"]},
            "personas": {"type": "string", "enum": ["ninguna", "1-3", "4-15", "más de 15"]},
            "posibles_menores": {"type": "string", "enum": ["sí", "no", "dudoso"]},
            "calidad": {"type": "string", "enum": ["alta", "media", "baja"]},
            "apta_propuesta": {"type": "boolean"},
            "descripcion": {"type": "string"},
            "palabras_clave": {"type": "array", "items": {"type": "string"}},
            "confianza": {"type": "string", "enum": ["alta", "media", "baja"]},
        },
        "required": ["vehiculo", "tipo_vehiculo", "plano", "lineas_servicio", "marca_visible", "fuente_marca",
                     "personas", "posibles_menores", "calidad", "apta_propuesta", "descripcion",
                     "palabras_clave", "confianza"],
    },
}
PROPS = HERRAMIENTA["input_schema"]["properties"]


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
                    params={"fields": "id,type,name,size,extension,sha1", "limit": 1000, "offset": offset})
        r.raise_for_status()
        j = r.json()
        for it in j["entries"]:
            if it["id"] in EXCLUIR:
                continue
            if it["type"] == "folder":
                listar_carpeta(it["id"], f"{ruta}/{it['name']}" if ruta else it["name"], salida)
            elif it["type"] == "file" and (it.get("extension") or "").lower() in EXTENSIONES:
                salida.append({"id": it["id"], "nombre": it["name"], "ruta": ruta,
                               "tamano": it.get("size", 0), "sha1": it.get("sha1")})
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


def miniatura(file_id, lado=LADO, calidad=85):
    CACHE.mkdir(parents=True, exist_ok=True)
    destino = CACHE / (f"{file_id}.jpg" if lado == LADO else f"{file_id}_{lado}.jpg")
    if destino.exists():
        return destino.read_bytes()
    base = CACHE / f"{file_id}.jpg"
    if base.exists():
        datos = base.read_bytes()
    else:
        datos = box_representacion(file_id)
        if datos is None:
            r = box_get(f"{API}/files/{file_id}/content")
            r.raise_for_status()
            datos = r.content
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(datos))).convert("RGB")
    img.thumbnail((lado, lado))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=calidad)
    destino.write_bytes(buf.getvalue())
    return buf.getvalue()


def bloque_imagen(jpg):
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.b64encode(jpg).decode()}}


def bloques_referencia():
    """Fotos de referencia de la flota; van primero y se cachean, así se pagan casi una sola vez."""
    bloques, tokens = [{"type": "text", "text": "FOTOS DE REFERENCIA DE LA FLOTA (no son la foto a catalogar):"}], 0
    for nombre, file_id, desc in REFERENCIAS:
        try:
            jpg = miniatura(file_id, lado=LADO_REF, calidad=80)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠ referencia de {nombre} no disponible: {str(e)[:100]}")
            continue
        tokens += tokens_imagen(jpg)
        bloques += [{"type": "text", "text": f"Referencia «{nombre}»: {desc}"}, bloque_imagen(jpg)]
    bloques[-1]["cache_control"] = {"type": "ephemeral"}
    return bloques, tokens


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


def rutas(foto):
    return foto.get("rutas") or [foto.get("ruta", "")]


def carpetas_linea(foto):
    return {r.split("/")[0] for r in rutas(foto)} & set(LINEAS)


def normalizar(ficha):
    """Corrige mayúsculas y aparta los valores fuera de lista. Devuelve los valores corregidos."""
    fuera = []
    for campo, spec in PROPS.items():
        v = ficha.get(campo)
        if "enum" in spec:
            mapa = {e.lower(): e for e in spec["enum"]}
            if isinstance(v, str) and v.lower() in mapa:
                ficha[campo] = mapa[v.lower()]
            elif v not in (None, ""):
                fuera.append(f"{campo}={v}")
                ficha[campo] = next((x for x in ("desconocido", "otro", "ninguna") if x in spec["enum"]), "")
        elif spec.get("type") == "array" and "enum" in spec.get("items", {}):
            mapa = {e.lower(): e for e in spec["items"]["enum"]}
            lista = v if isinstance(v, list) else [v] if v else []
            buenos = []
            for x in lista:
                if isinstance(x, str) and x.lower() in mapa:
                    if mapa[x.lower()] not in buenos:
                        buenos.append(mapa[x.lower()])
                else:
                    fuera.append(f"{campo}={x}")
            ficha[campo] = buenos
    return fuera


def modelos_activos(args):
    if getattr(args, "modelos", None):
        return [m.strip() for m in args.modelos.split(",") if m.strip()]
    return MODELOS


def ids_lote(valor):
    """lotes.json guarda una lista de lotes por modelo (versiones antiguas: un solo id)."""
    return valor if isinstance(valor, list) else [valor]


def incompleta(ficha):
    return [c for c in HERRAMIENTA["input_schema"]["required"]
            if ficha.get(c) in (None, "", []) and c not in ("marca_visible",)]


def normal(texto):
    import unicodedata
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode().lower()
    return "".join(ch for ch in t if ch.isalnum())


def marca_en_carpeta(marca, foto):
    """¿La marca detectada aparece en el nombre de la carpeta de cliente (o al revés)?"""
    m = normal(marca)
    if len(m) < 3:
        return None
    for r in rutas(foto):
        c = normal(r.split("/")[0]) if r else ""
        if len(c) >= 3:
            return m in c or c in m or any(len(p) >= 4 and p in m for p in
                                          [normal(x) for x in r.split("/")[0].replace("_", " ").split()])
    return None


# ---------------------------------------------------------------- comandos
def cmd_listar(_args):
    raiz = box_get(f"{API}/folders/{CARPETA_RAIZ}", params={"fields": "name"})
    raiz.raise_for_status()
    print(f"Carpeta: {raiz.json()['name']} ({CARPETA_RAIZ})")
    archivos = []
    listar_carpeta(CARPETA_RAIZ, "", archivos)
    unicas = {}
    for f in archivos:
        clave = f.get("sha1") or f["id"]
        if clave in unicas:
            unicas[clave]["rutas"].append(f["ruta"])
            unicas[clave]["ids"].append(f["id"])
        else:
            unicas[clave] = {**f, "rutas": [f["ruta"]], "ids": [f["id"]]}
    fotos = list(unicas.values())
    total = len(fotos)
    fotos = fotos[:MAX_FOTOS]
    escribir_json(MANIFEST, fotos)
    print(f"Archivos de imagen: {len(archivos)} · fotos distintas: {total} "
          f"({len(archivos) - total} repetidas en varias carpetas)")
    print(f"En el piloto: {len(fotos)} (MAX_FOTOS={MAX_FOTOS}) · "
          f"tamaño original {sum(f['tamano'] for f in fotos) / 1e9:.2f} GB")
    for carpeta, n in Counter((r.split("/")[0] or "(raíz)") for f in fotos for r in rutas(f)).most_common():
        print(f"  {n:4d}  {carpeta}")


def cmd_enviar(args):
    fotos = leer_json(MANIFEST)
    if not fotos:
        sys.exit("No hay manifest. Ejecuta primero: python etiquetar.py listar")
    modelos = modelos_activos(args)
    lotes = leer_json(LOTES, {}) or {}
    repetidos = [m for m in modelos if m in lotes]
    if repetidos and not args.forzar:
        sys.exit(f"Ya hay lote para {', '.join(repetidos)}. Usa --forzar para sustituirlo "
                 f"(o --modelos para enviar solo otros).")

    refs, tokens_refs = bloques_referencia() if USAR_REFERENCIAS else ([], 0)
    bytes_refs = len(json.dumps(refs))
    if refs:
        print(f"Referencias de flota: {(len(refs) - 1) // 2} fotos (~{tokens_refs} tokens, en caché)")

    # 1) miniaturas en paralelo (quedan en caché en disco; no se guardan en memoria)
    print(f"Preparando {len(fotos)} miniaturas con {HILOS} descargas a la vez (caché en {CACHE}/)...")
    hechas = {"n": 0}

    def preparar(foto):
        try:
            jpg = miniatura(foto["id"])
            res = (foto, tokens_imagen(jpg), len(jpg), None)
        except Exception as e:  # noqa: BLE001
            res = (foto, 0, 0, str(e)[:120])
        hechas["n"] += 1
        if hechas["n"] % 50 == 0:
            print(f"  {hechas['n']}/{len(fotos)}")
        return res

    with ThreadPoolExecutor(max_workers=HILOS) as ex:
        preparadas = list(ex.map(preparar, fotos))
    validas = [(f, t, b) for f, t, b, err in preparadas if err is None]
    for f, _, _, err in preparadas:
        if err:
            print(f"  ⚠ no se pudo preparar {f['nombre']}: {err}")
    if not validas:
        sys.exit("No se ha podido preparar ninguna imagen.")

    # 2) repartir en lotes por tamaño
    limite = MAX_MB_LOTE * 1024 * 1024
    grupos, actual, peso = [], [], 0
    for f, t, b in validas:
        p = bytes_refs + int(b * 1.37) + 8000
        if actual and (peso + p > limite or len(actual) >= MAX_PETICIONES_LOTE):
            grupos.append(actual)
            actual, peso = [], 0
        actual.append(f)
        peso += p
    if actual:
        grupos.append(actual)

    n = len(validas)
    tokens_img = sum(t for _, t, _ in validas)
    tokens_fijos = 2200  # instrucciones + esquema, aprox.
    print(f"\n{n} fotos en {len(grupos)} lote(s) por modelo.")
    print("Coste estimado (precios Batch orientativos; referencias contadas al 30 % por la caché):")
    total = 0.0
    for modelo in modelos:
        pin, pout = precio(modelo)
        coste = ((tokens_img + n * (tokens_fijos + 0.3 * tokens_refs)) * pin + n * 450 * pout) / 1e6
        total += coste
        print(f"  {modelo:22s} {n} fotos ≈ {coste:.2f} $")
    print(f"  TOTAL ≈ {total:.2f} $")
    if not args.si and input("\n¿Enviar los lotes? [s/N] ").strip().lower() not in ("s", "si", "sí"):
        sys.exit("Cancelado. No se ha enviado nada.")

    # 3) enviar lote a lote, construyendo las peticiones solo de ese lote
    cliente = anthropic.Anthropic(api_key=entorno("ANTHROPIC_API_KEY"))
    for modelo in modelos:
        lotes[modelo] = []
        for k, grupo in enumerate(grupos, 1):
            peticiones = []
            for foto in grupo:
                texto = f"Archivo: {foto['nombre']}"
                if INCLUIR_RUTA and any(rutas(foto)):
                    texto += "\nCarpeta: " + " | ".join(r for r in rutas(foto) if r)
                contenido = refs + [{"type": "text", "text": "FOTO A CATALOGAR:"},
                                    bloque_imagen(miniatura(foto["id"])), {"type": "text", "text": texto}]
                # tool_choice "auto": algunos modelos (p. ej. Sonnet 5.5) no aceptan forzar la herramienta.
                peticiones.append({
                    "custom_id": f"f_{foto['id']}",
                    "params": {
                        "model": modelo,
                        "max_tokens": 1500,
                        "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                        "tools": [HERRAMIENTA],
                        "tool_choice": {"type": "auto"},
                        "messages": [{"role": "user", "content": contenido}],
                    },
                })
            lote = cliente.messages.batches.create(requests=peticiones)
            lotes[modelo].append(lote.id)
            escribir_json(LOTES, lotes)
            print(f"Lote {k}/{len(grupos)} enviado para {modelo}: {lote.id} ({len(peticiones)} fotos)")
            del peticiones
    print("Los lotes suelen tardar de minutos a unas horas (máximo 24 h). Consulta con: python etiquetar.py estado")


class EstadoModelo:
    def __init__(self, procesando, ok, error, curso, caducadas):
        self.processing_status = procesando
        self.ok, self.error, self.curso, self.caducadas = ok, error, curso, caducadas


def estados(cliente, lotes):
    res = {}
    for modelo, valor in lotes.items():
        ok = err = curso = cad = 0
        terminado = True
        for lote_id in ids_lote(valor):
            b = cliente.messages.batches.retrieve(lote_id)
            c = b.request_counts
            ok, err, curso, cad = ok + c.succeeded, err + c.errored, curso + c.processing, cad + c.expired
            terminado &= b.processing_status == "ended"
        e = EstadoModelo("ended" if terminado else "in_progress", ok, err, curso, cad)
        res[modelo] = e
        print(f"  {modelo:22s} {e.processing_status:12s} ok={ok} error={err} en curso={curso} caducadas={cad} "
              f"({len(ids_lote(valor))} lote/s)")
    return res


def cmd_estado(_args):
    lotes = leer_json(LOTES)
    if not lotes:
        sys.exit("No hay lotes enviados.")
    estados(anthropic.Anthropic(api_key=entorno("ANTHROPIC_API_KEY")), lotes)


def resultados_de(cliente, valor):
    for lote_id in ids_lote(valor):
        yield from cliente.messages.batches.results(lote_id)


def cmd_recoger(_args):
    lotes = leer_json(LOTES)
    fotos = {f["id"]: f for f in (leer_json(MANIFEST) or [])}
    if not lotes:
        sys.exit("No hay lotes enviados.")
    cliente = anthropic.Anthropic(api_key=entorno("ANTHROPIC_API_KEY"))
    st = estados(cliente, lotes)
    if any(b.processing_status != "ended" for b in st.values()):
        sys.exit("Todavía hay lotes en curso. Vuelve a probar más tarde.")

    campos = ["file_id", "nombre", "carpetas", "enlace_box", "modelo", "vehiculo", "tipo_vehiculo", "plano",
              "linea_principal", "lineas_servicio", "marca_visible", "fuente_marca", "personas",
              "posibles_menores", "calidad", "apta_propuesta", "descripcion", "palabras_clave", "confianza",
              "fuera_de_lista", "incompleta", "marca_en_carpeta", "uso_autorizado", "coste_usd", "version", "revision_ok", "notas_revision"]
    filas, crudo = [], []
    coste_modelo, errores, motivos = Counter(), Counter(), {}
    for modelo, valor in lotes.items():
        pin, pout = precio(modelo)
        for r in resultados_de(cliente, valor):
            file_id = r.custom_id.split("_", 1)[1]
            foto = fotos.get(file_id, {"nombre": "?", "ruta": ""})
            if r.result.type != "succeeded":
                errores[modelo] += 1
                motivo = r.result.type
                err = getattr(r.result, "error", None)
                if err is not None:
                    interno = getattr(err, "error", err)
                    motivo = f"{r.result.type}: {getattr(interno, 'message', interno)}"
                motivos.setdefault(modelo, motivo)
                crudo.append({"modelo": modelo, "file_id": file_id, "error": motivo})
                continue
            msg = r.result.message
            ficha = next((b.input for b in msg.content if b.type == "tool_use"), None)
            if ficha is None:  # sin herramienta: intentar leer un JSON del texto
                texto = "".join(getattr(b, "text", "") for b in msg.content)
                try:
                    ficha = json.loads(texto[texto.index("{"): texto.rindex("}") + 1])
                except ValueError:
                    errores[modelo] += 1
                    motivos.setdefault(modelo, "respuesta sin ficha")
                    crudo.append({"modelo": modelo, "file_id": file_id, "error": "sin ficha", "texto": texto[:500]})
                    continue
            ficha = dict(ficha)
            fuera = normalizar(ficha)
            faltan = incompleta(ficha)
            u = msg.usage
            tin = (u.input_tokens + (u.cache_creation_input_tokens or 0) * 1.25
                   + (u.cache_read_input_tokens or 0) * 0.1)
            coste = (tin * pin + u.output_tokens * pout) / 1e6
            coste_modelo[modelo] += coste
            crudo.append({"modelo": modelo, "file_id": file_id, "ficha": ficha, "fuera_de_lista": fuera})
            lineas = ficha.get("lineas_servicio") or []
            filas.append({
                "file_id": file_id, "nombre": foto["nombre"],
                "carpetas": " | ".join(r or "(raíz)" for r in rutas(foto)),
                "enlace_box": f"https://app.box.com/file/{file_id}", "modelo": modelo,
                **{k: ficha.get(k, "") for k in PROPS},
                "linea_principal": lineas[0] if lineas else "",
                "lineas_servicio": ", ".join(lineas),
                "palabras_clave": ", ".join(ficha.get("palabras_clave", []) or []),
                "fuera_de_lista": "; ".join(fuera),
                "incompleta": ", ".join(faltan),
                "marca_en_carpeta": {True: "sí", False: "no", None: ""}[marca_en_carpeta(ficha.get("marca_visible", ""), foto)]
                if INCLUIR_RUTA else "",
                "uso_autorizado": "publicada en web" if CARPETA_RAIZ == "379464633435" else "pendiente",
                "coste_usd": f"{coste:.5f}", "version": VERSION, "revision_ok": "", "notas_revision": "",
                "_lineas": set(lineas), "_carpetas": carpetas_linea(foto),
            })

    filas.sort(key=lambda f: (f["carpetas"], f["nombre"], f["modelo"]))
    with RESULTADOS_CSV.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    with RESULTADOS_JSONL.open("w", encoding="utf-8") as fh:
        for c in crudo:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    # ---- resumen automático
    print(f"\nResultados {VERSION}: {RESULTADOS_CSV} ({len(filas)} filas)")
    for modelo in lotes:
        fuera_n = sum(1 for f in filas if f["modelo"] == modelo and f["fuera_de_lista"])
        inc_n = sum(1 for f in filas if f["modelo"] == modelo and f["incompleta"])
        n_mod = sum(1 for f in filas if f["modelo"] == modelo)
        print(f"  {modelo:22s} fichas {n_mod}   coste real ≈ {coste_modelo[modelo]:.3f} $   errores: {errores[modelo]}   "
              f"incompletas: {inc_n}   con valores fuera de lista: {fuera_n}")
        if modelo in motivos:
            print(f"      primer error: {motivos[modelo]}")

    if INCLUIR_RUTA:
        print("\nMarca detectada frente al cliente de la carpeta:")
        for modelo in lotes:
            c = Counter(f["marca_en_carpeta"] for f in filas if f["modelo"] == modelo)
            ev = c["sí"] + c["no"]
            if ev:
                print(f"  {modelo:22s} coincide {c['sí']}/{ev} = {c['sí'] / ev:.0%}   "
                      f"(sin marca o sin carpeta: {c['']})")

    print("\nLínea de servicio frente a la carpeta (fotos en carpetas con nombre de línea):")
    for modelo in lotes:
        ev = [f for f in filas if f["modelo"] == modelo and f["_carpetas"]]
        if ev:
            alguna = sum(bool(f["_lineas"] & f["_carpetas"]) for f in ev)
            principal = sum(f["linea_principal"] in f["_carpetas"] for f in ev)
            print(f"  {modelo:22s} alguna coincide {alguna}/{len(ev)} = {alguna / len(ev):.0%}   "
                  f"la principal {principal}/{len(ev)} = {principal / len(ev):.0%}")
        por_linea = Counter()
        aciertos = Counter()
        for f in ev:
            for c in f["_carpetas"]:
                por_linea[c] += 1
                aciertos[c] += c in f["_lineas"]
        for c in LINEAS:
            if por_linea[c]:
                print(f"      {c:42s} {aciertos[c]}/{por_linea[c]}")

    por_foto = {}
    for f in filas:
        por_foto.setdefault(f["file_id"], {})[f["modelo"]] = f
    mods = list(lotes)
    if len(mods) >= 2:
        a, b = mods[0], mods[1]
        pares = [d for d in por_foto.values() if a in d and b in d]
        if pares:
            print(f"\nCoincidencia {a} vs {b} ({len(pares)} fotos):")
            for campo in ("vehiculo", "tipo_vehiculo", "plano", "linea_principal", "posibles_menores",
                          "apta_propuesta"):
                igual = sum(str(d[a][campo]) == str(d[b][campo]) for d in pares)
                print(f"  {campo:18s} {igual / len(pares):5.0%}")

    for modelo in lotes:
        flota = Counter(f["vehiculo"] for f in filas if f["modelo"] == modelo and f["vehiculo"] in FLOTA)
        if flota:
            print(f"\nVehículos de la flota detectados por {modelo}: "
                  + ", ".join(f"{k} {v}" for k, v in flota.most_common()))
    menores = {f["nombre"] for f in filas if f["posibles_menores"] in ("sí", "dudoso")}
    if menores:
        print(f"\nFotos marcadas con posibles menores ({len(menores)}): revisar antes de usarlas en material comercial.")


def cmd_todo(args):
    if not MANIFEST.exists():
        cmd_listar(args)
    lotes = leer_json(LOTES, {}) or {}
    if args.forzar or any(m not in lotes for m in modelos_activos(args)):
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
    p.add_argument("--forzar", action="store_true", help="sustituir el lote de los modelos indicados")
    p.add_argument("--modelos", help="modelos a enviar, separados por comas (por defecto, MODELOS)")
    args = p.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    {"listar": cmd_listar, "enviar": cmd_enviar, "estado": cmd_estado,
     "recoger": cmd_recoger, "todo": cmd_todo}[args.comando](args)


if __name__ == "__main__":
    main()
