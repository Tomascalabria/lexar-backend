"""
LEXAR - Backend API
Scraping de Infoleg + Diputados/Senado Argentina
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
from bs4 import BeautifulSoup
import re
import csv
import io
import json
import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional
import unicodedata

app = FastAPI(title="LEXAR API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Cache simple en memoria ──────────────────────────────────────────────────
_cache: dict = {}
CACHE_TTL = 3600  # 1 hora

def cache_get(key: str):
    if key in _cache:
        val, ts = _cache[key]
        if datetime.now() - ts < timedelta(seconds=CACHE_TTL):
            return val
    return None

def cache_set(key: str, val):
    _cache[key] = (val, datetime.now())

# ─── Utilidades ───────────────────────────────────────────────────────────────

def infoleg_texto_url(norma_id: int) -> str:
    """Construye la URL del texto de la norma en infoleg."""
    base = (norma_id // 1000) * 1000
    top = base + 999
    return f"https://servicios.infoleg.gob.ar/infolegInternet/anexos/{base}-{top}/{norma_id}/norma.htm"

def infoleg_meta_url(norma_id: int) -> str:
    return f"https://servicios.infoleg.gob.ar/infolegInternet/verNorma.do?id={norma_id}"

def argentina_gob_url(norma_id: int, slug: str = "") -> str:
    return f"https://www.argentina.gob.ar/normativa/nacional/{slug}-{norma_id}/texto"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LexarBot/1.0; investigacion academica)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "es-AR,es;q=0.9",
}

# ─── Catálogo curado de leyes destacadas (IDs verificados en Infoleg) ─────────
LEYES_DESTACADAS = [
    # MEDIO AMBIENTE
    {
        "id": 174117, "tipo": "LEY", "numero": "26639",
        "titulo": "Régimen de Presupuestos Mínimos para la Preservación de los Glaciares y del Ambiente Periglacial",
        "categoria": "Medio Ambiente", "emoji": "🏔️",
        "tags": ["glaciares", "ambiente", "minería", "agua"],
        "estado": "VIGENTE — BAJO REFORMA (Proyecto PE 2025)",
        "resumen": "Protege glaciares y ambiente periglacial como reservas hídricas. Prohíbe minería e hidrocarburos en esas áreas. El PE envió un proyecto de modificación en diciembre 2025.",
    },
    {
        "id": 136125, "tipo": "LEY", "numero": "26331",
        "titulo": "Presupuestos Mínimos de Protección Ambiental de los Bosques Nativos",
        "categoria": "Medio Ambiente", "emoji": "🌳",
        "tags": ["bosques", "deforestación", "ambiente", "ordenamiento territorial"],
        "estado": "VIGENTE",
        "resumen": "Establece presupuestos mínimos para conservación de bosques nativos. Crea el Fondo Nacional para el Enriquecimiento y Conservación de los Bosques Nativos.",
    },
    {
        "id": 79980, "tipo": "LEY", "numero": "25675",
        "titulo": "Ley General del Ambiente — Política Ambiental Nacional",
        "categoria": "Medio Ambiente", "emoji": "🌎",
        "tags": ["ambiente", "desarrollo sustentable", "presupuestos mínimos"],
        "estado": "VIGENTE",
        "resumen": "Marco general de política ambiental. Define principios de prevención, precautorio, equidad intergeneracional y responsabilidad. Base de toda la legislación ambiental.",
    },
    {
        "id": 333515, "tipo": "LEY", "numero": "27520",
        "titulo": "Presupuestos Mínimos de Adaptación y Mitigación al Cambio Climático Global",
        "categoria": "Medio Ambiente", "emoji": "🌡️",
        "tags": ["cambio climático", "emisiones", "ambiente"],
        "estado": "VIGENTE",
        "resumen": "Establece presupuestos mínimos para adaptación y mitigación al cambio climático. Define metas de reducción de emisiones y creación del Gabinete Nacional de Cambio Climático.",
    },
    {
        "id": 350594, "tipo": "LEY", "numero": "27621",
        "titulo": "Educación Ambiental Integral",
        "categoria": "Medio Ambiente", "emoji": "📚",
        "tags": ["educación ambiental", "ambiente", "educación"],
        "estado": "VIGENTE",
        "resumen": "Establece la educación ambiental integral como política pública nacional, articulando con la Ley General del Ambiente y las leyes de Glaciares, Bosques y Manejo del Fuego.",
    },

    # PENAL / JUVENIL
    {
        "id": 114167, "tipo": "LEY", "numero": "22278",
        "titulo": "Régimen Penal de la Minoridad",
        "categoria": "Penal", "emoji": "⚖️",
        "tags": ["menores", "imputabilidad", "penal juvenil", "minoridad"],
        "estado": "BAJO REFORMA INTENSA (múltiples proyectos 2024-2025)",
        "resumen": "Fija la no punibilidad por debajo de 16 años. Establece el régimen de disposición tutelar. Principal foco del debate sobre baja de edad de imputabilidad y nuevo sistema penal juvenil.",
    },
    {
        "id": 110778, "tipo": "LEY", "numero": "26061",
        "titulo": "Protección Integral de los Derechos de las Niñas, Niños y Adolescentes",
        "categoria": "Penal", "emoji": "👶",
        "tags": ["niñez", "adolescencia", "derechos", "menores"],
        "estado": "VIGENTE",
        "resumen": "Marco de protección integral de derechos de NNyA. Crea el sistema de protección con organismos provinciales y nacionales. Complementa el régimen penal juvenil.",
    },
    {
        "id": 20066, "tipo": "LEY", "numero": "24660",
        "titulo": "Ejecución de la Pena Privativa de la Libertad",
        "categoria": "Penal", "emoji": "🔒",
        "tags": ["cárceles", "ejecución penal", "prisión", "rehabilitación"],
        "estado": "VIGENTE — modificada por Ley 27375",
        "resumen": "Regula el régimen carcelario, progresividad de la pena, libertad condicional y asistencia post-penitenciaria. Fue endurecida en 2017 para delitos graves.",
    },

    # LABORAL
    {
        "id": 25004, "tipo": "LEY", "numero": "20744",
        "titulo": "Ley de Contrato de Trabajo",
        "categoria": "Laboral", "emoji": "👷",
        "tags": ["trabajo", "contrato laboral", "empleados", "despido", "indemnización"],
        "estado": "BAJO REFORMA (Ley de Modernización Laboral 2025 en trámite)",
        "resumen": "Régimen general del contrato de trabajo: derechos y obligaciones de empleador y trabajador, causas de extinción, indemnizaciones. La más reformada de los últimos 50 años.",
    },

    # SALUD
    {
        "id": 175977, "tipo": "LEY", "numero": "26657",
        "titulo": "Derecho a la Protección de la Salud Mental",
        "categoria": "Salud", "emoji": "🧠",
        "tags": ["salud mental", "psiquiatría", "internación", "derechos"],
        "estado": "VIGENTE",
        "resumen": "Reconoce la salud mental como parte indisoluble de la salud integral. Regula internaciones involuntarias, equipos interdisciplinarios y sustitución del modelo manicomial.",
    },
    {
        "id": 101479, "tipo": "LEY", "numero": "25926",  # cannabis - corrección
        "titulo": "Investigación Médica y Científica del Uso Medicinal de la Planta de Cannabis",
        "categoria": "Salud", "emoji": "🌿",
        "tags": ["cannabis", "medicinal", "marihuana", "salud"],
        "estado": "VIGENTE — en debate ampliación",
        "resumen": "Autoriza la investigación médica y científica del uso medicinal del cannabis. Habilita el autocultivo con autorización del REPROCANN.",
    },
    {
        "id": 20778, "tipo": "LEY", "numero": "23798",
        "titulo": "Lucha contra el Síndrome de Inmunodeficiencia Adquirida (SIDA)",
        "categoria": "Salud", "emoji": "🎗️",
        "tags": ["VIH", "SIDA", "salud", "epidemiología"],
        "estado": "VIGENTE",
        "resumen": "Declara de interés nacional la lucha contra el SIDA. Garantiza el acceso a tratamiento gratuito, confidencialidad del diagnóstico y no discriminación.",
    },

    # DERECHOS HUMANOS / GÉNERO
    {
        "id": 152155, "tipo": "LEY", "numero": "26485",
        "titulo": "Protección Integral para Prevenir, Sancionar y Erradicar la Violencia contra las Mujeres",
        "categoria": "Género y DDHH", "emoji": "🟣",
        "tags": ["violencia de género", "femicidio", "mujeres", "derechos"],
        "estado": "VIGENTE — ampliada por Ley Olimpia (27736)",
        "resumen": "Define tipos de violencia contra la mujer (física, psicológica, sexual, económica, simbólica). Crea el sistema de protección integral y medidas cautelares urgentes.",
    },
    {
        "id": 319747, "tipo": "LEY", "numero": "27412",
        "titulo": "Paridad de Género en Ámbitos de Representación Política",
        "categoria": "Género y DDHH", "emoji": "⚥",
        "tags": ["paridad", "género", "elecciones", "listas"],
        "estado": "VIGENTE",
        "resumen": "Establece la paridad de género en las listas de candidatos para cargos electivos nacionales (50/50 entre mujeres y varones).",
    },
    {
        "id": 223586, "tipo": "LEY", "numero": "26743",
        "titulo": "Identidad de Género",
        "categoria": "Género y DDHH", "emoji": "🏳️‍⚧️",
        "tags": ["identidad de género", "trans", "rectificación registral"],
        "estado": "VIGENTE",
        "resumen": "Reconoce el derecho a la identidad de género autopercibida. Permite la rectificación registral del nombre y sexo sin requisito de cirugía ni diagnóstico médico.",
    },

    # TRANSPARENCIA / ESTADO
    {
        "id": 265949, "tipo": "LEY", "numero": "27275",
        "titulo": "Derecho de Acceso a la Información Pública",
        "categoria": "Transparencia", "emoji": "🔍",
        "tags": ["transparencia", "acceso información", "estado", "organismos"],
        "estado": "VIGENTE",
        "resumen": "Garantiza el acceso a información pública en poder del Estado. Presunción de publicidad, máxima divulgación. Crea la Agencia de Acceso a la Información Pública.",
    },
    {
        "id": 111500, "tipo": "LEY", "numero": "25188",
        "titulo": "Ética en el Ejercicio de la Función Pública",
        "categoria": "Transparencia", "emoji": "🏛️",
        "tags": ["ética", "función pública", "conflicto de intereses", "declaración jurada"],
        "estado": "VIGENTE",
        "resumen": "Establece deberes y obligaciones del funcionario público. Regula conflictos de interés, declaraciones juradas patrimoniales y sanciones.",
    },

    # EDUCACIÓN
    {
        "id": 123542, "tipo": "LEY", "numero": "26206",
        "titulo": "Ley de Educación Nacional",
        "categoria": "Educación", "emoji": "🎓",
        "tags": ["educación", "escuela", "docentes", "enseñanza obligatoria"],
        "estado": "VIGENTE",
        "resumen": "Regula el sistema educativo nacional. Establece la obligatoriedad desde los 5 años hasta finalizar el secundario. Define los niveles, modalidades y derechos educativos.",
    },
    {
        "id": 25394, "tipo": "LEY", "numero": "24521",
        "titulo": "Ley de Educación Superior",
        "categoria": "Educación", "emoji": "🏫",
        "tags": ["universidad", "educación superior", "autonomía universitaria"],
        "estado": "VIGENTE — en debate reforma financiamiento",
        "resumen": "Regula las instituciones de educación superior. Garantiza la autonomía universitaria y la gratuidad en las universidades nacionales. Base del debate sobre aranceles.",
    },

    # CONSUMIDOR / ECONOMÍA
    {
        "id": 638, "tipo": "LEY", "numero": "24240",
        "titulo": "Defensa del Consumidor",
        "categoria": "Consumidor", "emoji": "🛒",
        "tags": ["consumidor", "garantías", "proveedor", "reclamos"],
        "estado": "VIGENTE — modificaciones frecuentes",
        "resumen": "Protege al consumidor en la relación de consumo. Regula garantías, derecho de arrepentimiento, cláusulas abusivas y procedimiento de reclamos.",
    },
    {
        "id": 266833, "tipo": "LEY", "numero": "27253",
        "titulo": "Defensa del Consumidor — Obligación de Aceptar Tarjeta de Débito",
        "categoria": "Consumidor", "emoji": "💳",
        "tags": ["tarjeta débito", "consumidor", "pagos"],
        "estado": "VIGENTE",
        "resumen": "Obliga a comercios a aceptar tarjeta de débito como medio de pago. Complementa la Ley de Defensa del Consumidor.",
    },

    # AGRO / RECURSOS NATURALES
    {
        "id": 8785, "tipo": "LEY", "numero": "20247",
        "titulo": "Ley de Semillas y Creaciones Fitogenéticas",
        "categoria": "Agro", "emoji": "🌾",
        "tags": ["semillas", "agro", "propiedad intelectual", "fitogenética", "monsanto"],
        "estado": "VIGENTE — BAJO REFORMA INTENSA (debate 2023-2025)",
        "resumen": "Regula la producción, circulación y comercialización de semillas. El eje del debate es si los agricultores pueden guardar semilla propia y el alcance del royalty extendido.",
    },
    {
        "id": 9459, "tipo": "LEY", "numero": "20466",
        "titulo": "Código Alimentario Argentino — habilitación ANMAT",
        "categoria": "Agro", "emoji": "🍎",
        "tags": ["alimentos", "ANMAT", "inocuidad", "código alimentario"],
        "estado": "VIGENTE",
        "resumen": "Marco del código alimentario argentino. Regula habilitación de establecimientos, rotulado y control de alimentos.",
    },

    # TECNOLOGÍA / DATOS
    {
        "id": 68268, "tipo": "LEY", "numero": "25326",
        "titulo": "Protección de los Datos Personales (Habeas Data)",
        "categoria": "Tecnología", "emoji": "🔐",
        "tags": ["datos personales", "privacidad", "habeas data", "tecnología"],
        "estado": "VIGENTE — BAJO REFORMA (proyecto modernización 2025)",
        "resumen": "Regula la protección de datos personales. Habilita el habeas data como acción judicial. La ley data de 2000 y está desactualizada frente a GDPR e IA.",
    },

    # CÓDIGO CIVIL Y COMERCIAL
    {
        "id": 235975, "tipo": "LEY", "numero": "26994",
        "titulo": "Código Civil y Comercial de la Nación",
        "categoria": "Civil", "emoji": "📖",
        "tags": ["código civil", "contratos", "personas", "familia", "sucesiones"],
        "estado": "VIGENTE — modificaciones parciales frecuentes",
        "resumen": "Unificó el Código Civil y el Código de Comercio en 2015. Regula personas, familia, contratos, responsabilidad civil, sucesiones y derechos reales.",
    },

    # FISCAL / IMPOSITIVO
    {
        "id": 15300, "tipo": "LEY", "numero": "11683",
        "titulo": "Procedimiento Tributario",
        "categoria": "Fiscal", "emoji": "💰",
        "tags": ["AFIP", "ARCA", "impuestos", "procedimiento fiscal", "tributario"],
        "estado": "VIGENTE — modificaciones constantes",
        "resumen": "Regula el procedimiento de determinación y cobro de tributos nacionales. Establece las facultades de la AFIP/ARCA, prescripción y recursos.",
    },
    {
        "id": 255552, "tipo": "LEY", "numero": "27260",
        "titulo": "Programa Nacional de Reparación Histórica — Sinceramiento Fiscal",
        "categoria": "Fiscal", "emoji": "📊",
        "tags": ["blanqueo", "sinceramiento fiscal", "moratoria"],
        "estado": "VIGENTE (disposiciones permanentes)",
        "resumen": "Creó el programa de sinceramiento fiscal (blanqueo). Varios artículos con vigencia permanente vinculados a la reparación previsional.",
    },

    # JUBILACIONES
    {
        "id": 20594, "tipo": "LEY", "numero": "24241",
        "titulo": "Sistema Integrado de Jubilaciones y Pensiones (SIJP)",
        "categoria": "Previsional", "emoji": "👴",
        "tags": ["jubilaciones", "pensiones", "previsional", "ANSES", "SIPA"],
        "estado": "VIGENTE — fórmula de movilidad reformada múltiples veces",
        "resumen": "Base del sistema previsional. Regula aportes, requisitos de edad/años, cálculo del haber y movilidad previsional.",
    },
]

def get_leyes_destacadas_list() -> list:
    """Retorna el catálogo con URLs calculadas."""
    return [
        {**ley, "url_infoleg": infoleg_meta_url(ley["id"]), "url_texto": infoleg_texto_url(ley["id"])}
        for ley in LEYES_DESTACADAS
    ]

# ─── Carga del CSV muestreo de Infoleg (datos.jus.gob.ar) ────────────────────
# URL directa del muestreo (1000 registros, ~80KB, sin redirect)
CSV_MUESTREO_URL = "https://datos.jus.gob.ar/dataset/d9a963ea-8b1d-4ca3-9dd9-07a4773e8c23/resource/8b1c2310-564e-41e6-9a84-99cfa9939bbc/download/base-infoleg-normativa-nacional-muestreo.csv"

_normas_db: list[dict] = []
_normas_loaded = False

async def load_normas_csv():
    """Descarga el CSV muestreo de datos.jus.gob.ar al arrancar."""
    global _normas_db, _normas_loaded
    if _normas_loaded:
        return
    try:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(CSV_MUESTREO_URL)
            if r.status_code == 200:
                reader = csv.DictReader(io.StringIO(r.text))
                _normas_db = [row for row in reader]
                print(f"✅ CSV muestreo cargado: {len(_normas_db)} normas")
            else:
                print(f"⚠️  CSV muestreo: HTTP {r.status_code}")
    except Exception as e:
        print(f"⚠️  No se pudo cargar CSV muestreo: {e}")
    finally:
        _normas_loaded = True


@app.on_event("startup")
async def startup():
    asyncio.create_task(load_normas_csv())


# ─── Búsqueda de normas ───────────────────────────────────────────────────────

def normalizar(texto: str) -> str:
    return ''.join(
        c for c in unicodedata.normalize('NFD', texto.lower())
        if unicodedata.category(c) != 'Mn'
    )


async def _buscar_infoleg_scraping(q: str, tipo: Optional[str], limit: int) -> list:
    """
    Scraping del buscador real de Infoleg.
    URL: servicios.infoleg.gob.ar — el mismo que usa la web oficial.
    """
    resultados = []
    try:
        async with httpx.AsyncClient(timeout=25, headers=HEADERS, follow_redirects=True) as client:
            # El buscador de Infoleg usa un form POST
            url = "https://servicios.infoleg.gob.ar/infolegInternet/buscar.do"
            data = {
                "METHOD": "buscar",
                "TIPO_NORMA": tipo.upper() if tipo else "",
                "NUMERO": "",
                "ANIO": "",
                "ORGANISMO": "",
                "TITULO_SUMARIO": q,
                "TEXTO_ACTIVO": "true",
                "btnBuscar": "Buscar",
            }
            r = await client.post(url, data=data, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")

            # La tabla de resultados de Infoleg
            tabla = soup.find("table", {"class": "table"}) or soup.find("table")
            if tabla:
                filas = tabla.find_all("tr")[1:limit+1]
                for row in filas:
                    cols = row.find_all("td")
                    if len(cols) < 3:
                        continue
                    link = row.find("a")
                    norma_id = None
                    href = link.get("href", "") if link else ""
                    m = re.search(r"id=(\d+)", href)
                    if m:
                        norma_id = int(m.group(1))
                    # Columnas típicas: tipo | número | fecha | título
                    tipo_val   = cols[0].get_text(strip=True)
                    numero_val = cols[1].get_text(strip=True) if len(cols) > 1 else ""
                    fecha_val  = cols[2].get_text(strip=True) if len(cols) > 2 else ""
                    titulo_val = cols[3].get_text(strip=True) if len(cols) > 3 else cols[-1].get_text(strip=True)
                    if not titulo_val or len(titulo_val) < 4:
                        continue
                    resultados.append({
                        "id": norma_id,
                        "tipo": tipo_val,
                        "numero": numero_val,
                        "fecha": fecha_val,
                        "titulo": titulo_val,
                        "organismo": "",
                        "url_infoleg": infoleg_meta_url(norma_id) if norma_id else None,
                        "url_texto": infoleg_texto_url(norma_id) if norma_id else None,
                        "fuente": "infoleg_scraping",
                    })

            # Si la tabla no funcionó, buscar via argentina.gob.ar/normativa
            if not resultados:
                url2 = "https://www.argentina.gob.ar/normativa/buscar"
                params2 = {"keywords": q, "tipo": tipo or ""}
                r2 = await client.get(url2, params=params2)
                soup2 = BeautifulSoup(r2.text, "html.parser")
                for item in soup2.select(".normativa-item, .search-result-item, article")[:limit]:
                    titulo_el = item.find("h2") or item.find("h3") or item.find("a")
                    if not titulo_el:
                        continue
                    titulo = titulo_el.get_text(strip=True)
                    link2 = item.find("a")
                    href2 = link2.get("href", "") if link2 else ""
                    m2 = re.search(r"/(\d{5,})/", href2)
                    norma_id2 = int(m2.group(1)) if m2 else None
                    resultados.append({
                        "id": norma_id2,
                        "tipo": "",
                        "numero": "",
                        "fecha": "",
                        "titulo": titulo,
                        "organismo": "",
                        "url_infoleg": infoleg_meta_url(norma_id2) if norma_id2 else None,
                        "fuente": "argentina_gob_scraping",
                    })

    except Exception as e:
        print(f"⚠️  Scraping Infoleg falló: {e}")

    return resultados


@app.get("/api/buscar")
async def buscar_normas(
    q: str = Query(..., min_length=2),
    tipo: Optional[str] = Query(None),
    limit: int = Query(25, le=50),
):
    """
    Busca normas en cascada:
    1. Catálogo curado interno (siempre disponible, respuesta inmediata)
    2. CSV muestreo de datos.jus.gob.ar (1000 normas, cargado al arrancar)
    3. Scraping directo del buscador de Infoleg (tiempo real, cualquier norma)
    """
    cache_key = f"buscar:{q}:{tipo}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    q_norm = normalizar(q)
    resultados = []
    ids_vistos: set = set()

    # ── 1. Catálogo curado ──────────────────────────────────────────────────
    for ley in LEYES_DESTACADAS:
        titulo  = ley.get("titulo", "")
        tags    = " ".join(ley.get("tags", []))
        resumen = ley.get("resumen", "")
        match = (
            q_norm in normalizar(titulo) or
            q_norm in normalizar(tags) or
            q_norm in normalizar(resumen) or
            q_norm in normalizar(str(ley.get("numero", "")))
        )
        if match:
            if tipo and ley.get("tipo", "").upper() != tipo.upper():
                continue
            ids_vistos.add(ley["id"])
            resultados.append({
                "id": ley["id"],
                "tipo": ley.get("tipo", ""),
                "numero": ley.get("numero", ""),
                "organismo": "Poder Legislativo",
                "fecha": ley.get("fecha", ""),
                "titulo": titulo,
                "categoria": ley.get("categoria", ""),
                "estado": ley.get("estado", ""),
                "resumen": ley.get("resumen", ""),
                "url_infoleg": infoleg_meta_url(ley["id"]),
                "url_texto": infoleg_texto_url(ley["id"]),
                "fuente": "catalogo_curado",
            })

    # ── 2. CSV muestreo ─────────────────────────────────────────────────────
    if _normas_db:
        for row in _normas_db:
            titulo = (row.get("titulo_sumario") or row.get("titulo_resumido") or "").strip()
            if not titulo or q_norm not in normalizar(titulo):
                continue
            if tipo and row.get("tipo_norma", "").upper() != tipo.upper():
                continue
            try:
                norma_id = int(row.get("id_norma") or row.get("norma_id") or 0)
            except (TypeError, ValueError):
                continue
            if norma_id == 0 or norma_id in ids_vistos:
                continue
            ids_vistos.add(norma_id)
            resultados.append({
                "id": norma_id,
                "tipo": row.get("tipo_norma", ""),
                "numero": row.get("numero_norma", ""),
                "organismo": row.get("organismo_origen", ""),
                "fecha": row.get("fecha_boletin", ""),
                "titulo": titulo,
                "url_infoleg": infoleg_meta_url(norma_id),
                "url_texto": infoleg_texto_url(norma_id),
                "fuente": "csv_muestreo",
            })
            if len(resultados) >= limit:
                break

    # ── 3. Scraping Infoleg — siempre se ejecuta para dar resultados reales ──
    scraped = await _buscar_infoleg_scraping(q, tipo, limit)
    for item in scraped:
        nid = item.get("id")
        if nid and nid in ids_vistos:
            continue
        if nid:
            ids_vistos.add(nid)
        resultados.append(item)
        if len(resultados) >= limit:
            break

    resultados = resultados[:limit]
    cache_set(cache_key, resultados)
    return resultados


# ─── Texto completo de una ley ────────────────────────────────────────────────

@app.get("/api/ley/{norma_id}")
async def obtener_ley(norma_id: int):
    """Obtiene el texto completo de una ley por su ID de Infoleg."""
    cached = cache_get(f"ley:{norma_id}")
    if cached:
        return cached

    texto_url = infoleg_texto_url(norma_id)
    meta_url = infoleg_meta_url(norma_id)

    async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
        # Metadatos
        meta = {}
        try:
            r_meta = await client.get(meta_url)
            soup_meta = BeautifulSoup(r_meta.text, "html.parser")
            titulo_el = soup_meta.find("h2") or soup_meta.find("h1")
            meta["titulo"] = titulo_el.text.strip() if titulo_el else ""
            # buscar tabla de metadatos
            for row in soup_meta.select("table tr"):
                cols = row.find_all("td")
                if len(cols) == 2:
                    k = cols[0].text.strip().lower()
                    v = cols[1].text.strip()
                    if "tipo" in k:
                        meta["tipo"] = v
                    elif "número" in k or "numero" in k:
                        meta["numero"] = v
                    elif "fecha" in k:
                        meta["fecha_publicacion"] = v
                    elif "organismo" in k:
                        meta["organismo"] = v
        except Exception:
            pass

        # Texto completo
        texto_articulos = []
        texto_raw = ""
        try:
            r_texto = await client.get(texto_url)
            if r_texto.status_code == 200:
                soup = BeautifulSoup(r_texto.text, "html.parser")
                # Limpiar scripts/styles
                for tag in soup(["script", "style", "nav", "header", "footer"]):
                    tag.decompose()
                # Extraer texto párrafo a párrafo
                body = soup.find("body") or soup
                parrafos = []
                for tag in body.find_all(["p", "div", "h1", "h2", "h3", "h4", "li"]):
                    t = tag.get_text(" ", strip=True)
                    if t and len(t) > 10:
                        parrafos.append(t)

                # Agrupar en artículos
                articulo_actual = None
                art_pattern = re.compile(
                    r'^(art[íi]culo\s+\d+[\w°º]*|art\.\s*\d+[\w°º]*)',
                    re.IGNORECASE
                )
                for p in parrafos:
                    if art_pattern.match(p):
                        if articulo_actual:
                            texto_articulos.append(articulo_actual)
                        # Extraer número y título
                        partes = p.split("—", 1) if "—" in p else p.split("-", 1)
                        titulo_art = partes[1].strip()[:80] if len(partes) > 1 else ""
                        articulo_actual = {
                            "encabezado": partes[0].strip(),
                            "titulo": titulo_art,
                            "texto": p,
                        }
                    elif articulo_actual:
                        articulo_actual["texto"] += "\n\n" + p

                if articulo_actual:
                    texto_articulos.append(articulo_actual)

                texto_raw = "\n\n".join(parrafos)
            else:
                # Fallback a argentina.gob.ar
                r2 = await client.get(f"https://www.argentina.gob.ar/normativa/nacional/-{norma_id}/texto")
                soup2 = BeautifulSoup(r2.text, "html.parser")
                contenido = soup2.find("div", class_="field-items") or soup2.find("article")
                texto_raw = contenido.get_text("\n", strip=True) if contenido else ""
        except Exception as e:
            texto_raw = f"Error obteniendo texto: {e}"

        resultado = {
            "id": norma_id,
            "url_texto": texto_url,
            "url_meta": meta_url,
            **meta,
            "articulos": texto_articulos,
            "texto_completo": texto_raw,
            "total_articulos": len(texto_articulos),
        }
        cache_set(f"ley:{norma_id}", resultado)
        return resultado


# ─── Proyectos relacionados en Diputados ─────────────────────────────────────

@app.get("/api/proyectos/diputados")
async def proyectos_diputados(
    q: str = Query(..., description="Término de búsqueda"),
    anio: Optional[int] = Query(None),
    tipo: Optional[str] = Query(None),
):
    """Busca proyectos en Diputados via su buscador público."""
    cache_key = f"diputados:{q}:{anio}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    resultados = []

    try:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            # URL del buscador público de Diputados
            url = "https://www.diputados.gov.ar/proyectos/proyectos.html"
            params = {
                "strModo": "busqueda",
                "strTipoDocumento": "todos",
                "strCamaraOrigen": "AMBAS",
                "strPeriodoInicio": str(anio) if anio else "",
                "strPeriodoFin": "",
                "strNumExpediente": "",
                "strAutor": "",
                "strTema": q,
            }
            r = await client.get(url, params=params, timeout=25)
            soup = BeautifulSoup(r.text, "html.parser")

            # Intentar tabla principal
            tabla = soup.find("table", {"class": lambda c: c and ("table" in c or "proyecto" in c.lower())})
            if not tabla:
                tabla = soup.find("table")

            if tabla:
                filas = tabla.find_all("tr")[1:26]  # máx 25
                for row in filas:
                    cols = row.find_all("td")
                    if len(cols) < 2:
                        continue
                    link = row.find("a")
                    textos = [c.get_text(strip=True) for c in cols]
                    # Heurística: el texto más largo suele ser el título
                    titulo = max(textos, key=len) if textos else ""
                    exp = textos[0] if textos else ""
                    resultados.append({
                        "expediente": exp,
                        "titulo": titulo,
                        "autores": textos[2] if len(textos) > 2 else "",
                        "fecha": textos[1] if len(textos) > 1 else "",
                        "url": ("https://www.diputados.gov.ar" + link["href"]) if link and link.get("href", "").startswith("/") else (link["href"] if link else None),
                        "camara": "Diputados",
                    })

            # Si no hay tabla, intentar con el buscador alternativo
            if not resultados:
                url2 = "https://www.diputados.gov.ar/proyectos/resultado.html"
                form_data = {
                    "strModo": "busqueda",
                    "strTipoDocumento": "todos",
                    "strCamaraOrigen": "AMBAS",
                    "strPeriodoInicio": str(anio) if anio else "",
                    "strTema": q,
                    "btnBuscar": "Buscar",
                }
                r2 = await client.post(url2, data=form_data, timeout=25)
                soup2 = BeautifulSoup(r2.text, "html.parser")
                for row in soup2.find_all("tr")[1:26]:
                    cols = row.find_all("td")
                    if len(cols) < 2:
                        continue
                    link = row.find("a")
                    textos = [c.get_text(strip=True) for c in cols]
                    titulo = max(textos, key=len) if textos else ""
                    resultados.append({
                        "expediente": textos[0] if textos else "",
                        "titulo": titulo,
                        "autores": textos[2] if len(textos) > 2 else "",
                        "fecha": textos[1] if len(textos) > 1 else "",
                        "url": ("https://www.diputados.gov.ar" + link["href"]) if link and link.get("href", "").startswith("/") else None,
                        "camara": "Diputados",
                    })

    except Exception as e:
        resultados = [{"error": f"No se pudo conectar con Diputados: {str(e)[:120]}"}]

    # Filtrar filas vacías o sin título real
    resultados = [r for r in resultados if r.get("titulo") and len(r["titulo"]) > 5]

    cache_set(cache_key, resultados)
    return resultados


# ─── Proyectos en Senado ──────────────────────────────────────────────────────

@app.get("/api/proyectos/senado")
async def proyectos_senado(
    q: str = Query(...),
    anio: Optional[int] = Query(None),
):
    """Busca proyectos en el Senado via su buscador público."""
    cache_key = f"senado:{q}:{anio}"
    cached = cache_get(cache_key)
    if cached:
        return cached

    resultados = []
    try:
        async with httpx.AsyncClient(timeout=30, headers=HEADERS, follow_redirects=True) as client:
            # Buscador del Senado
            url = "https://www.senado.gob.ar/parlamentario/parlamentaria/busquedaAvanzada/search"
            params = {
                "pageNum": 1,
                "orderBy": "fecha",
                "tipoDoc": "PL",
                "textoBusqueda": q,
            }
            if anio:
                params["anio"] = anio

            r = await client.get(url, params=params, timeout=25)
            soup = BeautifulSoup(r.text, "html.parser")

            # El Senado usa una tabla con clase específica
            filas = soup.select("table tr")[1:26]
            if not filas:
                # Intentar selectores alternativos
                filas = soup.select(".resultado tr, .busqueda-resultado tr, tr")[1:26]

            for row in filas:
                cols = row.find_all("td")
                if len(cols) < 2:
                    continue
                link = row.find("a")
                textos = [c.get_text(strip=True) for c in cols]
                titulo = max(textos, key=len) if textos else ""
                if len(titulo) < 5:
                    continue
                href = link["href"] if link else ""
                full_url = None
                if href:
                    full_url = href if href.startswith("http") else f"https://www.senado.gob.ar{href}"
                resultados.append({
                    "expediente": textos[0] if textos else "",
                    "titulo": titulo,
                    "fecha": textos[1] if len(textos) > 1 else "",
                    "autores": textos[2] if len(textos) > 2 else "",
                    "url": full_url,
                    "camara": "Senado",
                })

    except Exception as e:
        resultados = [{"error": f"No se pudo conectar con el Senado: {str(e)[:120]}"}]

    resultados = [r for r in resultados if r.get("titulo") and len(r["titulo"]) > 5]
    cache_set(cache_key, resultados)
    return resultados


# ─── Normas que modifican a una ley ───────────────────────────────────────────

@app.get("/api/ley/{norma_id}/modificaciones")
async def modificaciones_de_ley(norma_id: int):
    """Qué otras normas modificaron a esta ley (árbol de modificaciones)."""
    cached = cache_get(f"mods:{norma_id}")
    if cached:
        return cached

    url = f"https://www.argentina.gob.ar/normativa/nacional/-{norma_id}/normas-modifican"
    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            mods = []
            for item in soup.select("li a, table tr td a"):
                texto = item.text.strip()
                href = item.get("href", "")
                id_match = re.search(r"(\d{5,})", href)
                if texto and id_match:
                    mods.append({
                        "texto": texto,
                        "id": int(id_match.group(1)),
                        "url": f"https://www.argentina.gob.ar{href}" if href.startswith("/") else href,
                    })
            resultado = {"norma_id": norma_id, "modificaciones": mods, "total": len(mods)}
            cache_set(f"mods:{norma_id}", resultado)
            return resultado
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ─── Health check ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "api": "LEXAR",
        "version": "1.0.0",
        "normas_en_cache": len(_normas_db),
        "endpoints": [
            "GET /api/buscar?q=glaciares&tipo=LEY",
            "GET /api/ley/{id}",
            "GET /api/ley/{id}/modificaciones",
            "GET /api/proyectos/diputados?q=glaciares&anio=2025",
            "GET /api/proyectos/senado?q=glaciares",
        ]
    }


# ─── Catálogo curado ──────────────────────────────────────────────────────────

@app.get("/api/catalogo")
async def catalogo_destacadas(
    categoria: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    """Lista las leyes curadas con IDs verificados. Ideal para el selector inicial."""
    leyes = get_leyes_destacadas_list()
    if categoria:
        leyes = [l for l in leyes if l["categoria"].lower() == categoria.lower()]
    if q:
        q_norm = normalizar(q)
        leyes = [
            l for l in leyes
            if q_norm in normalizar(l["titulo"])
            or any(q_norm in normalizar(t) for t in l.get("tags", []))
            or q_norm in normalizar(l.get("resumen", ""))
        ]
    return leyes


@app.get("/api/catalogo/categorias")
async def categorias_catalogo():
    """Todas las categorías del catálogo con conteo."""
    from collections import Counter
    cats = Counter(l["categoria"] for l in LEYES_DESTACADAS)
    return [{"categoria": k, "cantidad": v} for k, v in sorted(cats.items())]


@app.get("/api/catalogo/{norma_id}")
async def catalogo_item(norma_id: int):
    """Metadatos curados de una ley del catálogo."""
    for ley in LEYES_DESTACADAS:
        if ley["id"] == norma_id:
            return {**ley, "url_infoleg": infoleg_meta_url(norma_id), "url_texto": infoleg_texto_url(norma_id)}
    raise HTTPException(404, detail=f"Ley {norma_id} no en catálogo curado")


# ─── Modelos Pydantic ─────────────────────────────────────────────────────────

from pydantic import BaseModel

class ComparadorRequest(BaseModel):
    ley_a_titulo: str
    ley_a_numero: str
    ley_a_tipo: str
    ley_a_resumen: str
    ley_a_reforma: str = ""
    ley_b_titulo: str
    ley_b_numero: str
    ley_b_tipo: str
    ley_b_resumen: str
    ley_b_reforma: str = ""

class BuscarIARequest(BaseModel):
    q: str  # ej: "ley de glaciares", "contrato de trabajo", "defensa del consumidor"


def _get_api_key():
    key = (
        os.environ.get("ANTHROPIC_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_API_KEY")
    )
    if not key:
        env_keys = [k for k in os.environ if "anthrop" in k.lower() or "claude" in k.lower()]
        raise HTTPException(500, detail=f"ANTHROPIC_KEY no configurada en Railway. Variables encontradas: {env_keys or 'ninguna'}")
    return key


# ─── Búsqueda de leyes con Claude + web_search ────────────────────────────────

@app.post("/api/buscar-ia")
async def buscar_con_ia(req: BuscarIARequest):
    """
    Usa Claude con web_search para encontrar la ley solicitada en Infoleg
    y devuelve: título, número, ID Infoleg, URL del texto y resumen.
    """
    api_key = _get_api_key()

    prompt = f"""Buscá información sobre esta norma argentina: "{req.q}"

Necesito que uses web_search para encontrarla en infoleg.gob.ar o servicios.infoleg.gob.ar.

Devolvé SOLO un JSON con este formato exacto, sin texto adicional, sin markdown:
{{
  "resultados": [
    {{
      "titulo": "título completo de la ley",
      "tipo": "LEY / DECRETO / RESOLUCION",
      "numero": "número de la norma",
      "anio": "año de sanción",
      "infoleg_id": 12345,
      "url_texto": "https://servicios.infoleg.gob.ar/infolegInternet/anexos/...",
      "url_infoleg": "https://servicios.infoleg.gob.ar/infolegInternet/verNorma.do?id=...",
      "resumen": "resumen breve de qué regula esta ley"
    }}
  ]
}}

Incluí hasta 5 resultados relevantes. Si no encontrás el ID exacto de Infoleg, poné null en infoleg_id.
Priorizá resultados de servicios.infoleg.gob.ar."""

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    if r.status_code != 200:
        raise HTTPException(502, detail=f"Error Claude: {r.text[:300]}")

    data = r.json()

    # Extraer texto de todos los bloques de contenido
    texto = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            texto += block.get("text", "")

    # Limpiar posibles markdown fences
    texto_limpio = re.sub(r"```(?:json)?|```", "", texto).strip()

    # Intentar parsear JSON
    try:
        # Buscar el objeto JSON dentro del texto
        m = re.search(r'\{[\s\S]*"resultados"[\s\S]*\}', texto_limpio)
        if m:
            parsed = json.loads(m.group(0))
            resultados = parsed.get("resultados", [])
        else:
            resultados = json.loads(texto_limpio).get("resultados", [])
    except Exception:
        # Si no parsea, devolver como texto libre para debug
        return {"resultados": [], "raw": texto_limpio[:500]}

    # Enriquecer con URL de texto si tenemos el ID
    for item in resultados:
        if item.get("infoleg_id"):
            try:
                nid = int(item["infoleg_id"])
                if not item.get("url_texto"):
                    item["url_texto"] = infoleg_texto_url(nid)
                if not item.get("url_infoleg"):
                    item["url_infoleg"] = infoleg_meta_url(nid)
                item["id"] = nid
            except (ValueError, TypeError):
                pass

    return {"resultados": resultados, "query": req.q}


# ─── Comparador con IA ────────────────────────────────────────────────────────

@app.post("/api/comparar")
async def comparar_leyes(req: ComparadorRequest):
    """Llama a Claude para comparar dos normas. API key desde variable de entorno."""
    api_key = _get_api_key()

    prompt = f"""Sos un experto en derecho argentino. Compará estas dos normas en 4 puntos concisos:
1. Relación entre ambas normas
2. Principales diferencias y cambios que introduce B respecto de A
3. Cuál prevalece en caso de conflicto y por qué
4. Impacto práctico y contexto político-jurídico actual

LEY / NORMA A (VIGENTE): {req.ley_a_titulo} ({req.ley_a_tipo} {req.ley_a_numero})
{req.ley_a_resumen}
{("Reforma propuesta: " + req.ley_a_reforma) if req.ley_a_reforma else ""}

LEY / NORMA B (REFORMA O COMPARAR): {req.ley_b_titulo} ({req.ley_b_tipo} {req.ley_b_numero})
{req.ley_b_resumen}
{("Cambios: " + req.ley_b_reforma) if req.ley_b_reforma else ""}

Respondé en español, de forma clara y directa. Usá los 4 puntos numerados."""

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    if r.status_code != 200:
        raise HTTPException(502, detail=f"Error Anthropic: {r.text[:300]}")

    data = r.json()
    texto = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"analisis": texto}

# ─── Lector de PDF ────────────────────────────────────────────────────────────

class PDFRequest(BaseModel):
    base64: str
    nombre: str = "documento.pdf"

@app.post("/api/leer-pdf")
async def leer_pdf(req: PDFRequest):
    """
    Recibe un PDF en base64, extrae el texto con pypdf y lo devuelve.
    """
    import base64 as b64
    import io

    try:
        pdf_bytes = b64.b64decode(req.base64)
    except Exception:
        raise HTTPException(400, detail="base64 inválido")

    texto = ""
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        partes = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                partes.append(t.strip())
        texto = "\n\n".join(partes)
    except ImportError:
        # pypdf no instalado — intentar con pdfminer
        try:
            from pdfminer.high_level import extract_text as pdfminer_extract
            texto = pdfminer_extract(io.BytesIO(pdf_bytes))
        except ImportError:
            raise HTTPException(501, detail="Ninguna librería PDF disponible (pypdf / pdfminer.six)")
    except Exception as e:
        raise HTTPException(500, detail=f"Error al leer PDF: {str(e)}")

    if not texto.strip():
        raise HTTPException(422, detail="El PDF no contiene texto extraíble (puede ser escaneado/imagen)")

    return {
        "nombre": req.nombre,
        "texto": texto[:20000],        # máximo 20k chars para el análisis
        "total_chars": len(texto),
        "paginas": len(pypdf.PdfReader(io.BytesIO(b64.b64decode(req.base64))).pages) if "pypdf" in dir() else None,
    }
