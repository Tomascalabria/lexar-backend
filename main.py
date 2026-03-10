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

# ─── Descarga del CSV de Infoleg ──────────────────────────────────────────────

CSV_URL = "https://datos.gob.ar/dataset/jus-base-infoleg-normativa-nacional/archivo/jus_01"
_normas_db: list[dict] = []
_normas_loaded = False

async def load_normas_csv():
    """Descarga y parsea el CSV de infoleg una vez al arrancar."""
    global _normas_db, _normas_loaded
    if _normas_loaded:
        return

    # Intentar desde datos.gob.ar directo
    csv_urls = [
        "https://infra.datos.gob.ar/catalog/jus/dataset/1/distribution/1.1/download/base-infoleg-normativa-nacional.csv",
        "https://datos.gob.ar/dataset/jus-base-infoleg-normativa-nacional/archivo/jus_01",
    ]

    async with httpx.AsyncClient(timeout=60, headers=HEADERS, follow_redirects=True) as client:
        for url in csv_urls:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    content = r.text
                    reader = csv.DictReader(io.StringIO(content))
                    _normas_db = [row for row in reader]
                    _normas_loaded = True
                    print(f"✅ CSV cargado: {len(_normas_db)} normas")
                    return
            except Exception as e:
                print(f"⚠️  Error con {url}: {e}")
                continue

    print("⚠️  No se pudo cargar CSV, búsqueda degradada a scraping directo")
    _normas_loaded = True


@app.on_event("startup")
async def startup():
    asyncio.create_task(load_normas_csv())


# ─── Búsqueda de normas ───────────────────────────────────────────────────────

def normalizar(texto: str) -> str:
    """Quita tildes y pasa a minúsculas para comparar."""
    return ''.join(
        c for c in unicodedata.normalize('NFD', texto.lower())
        if unicodedata.category(c) != 'Mn'
    )

@app.get("/api/buscar")
async def buscar_normas(
    q: str = Query(..., min_length=2, description="Texto a buscar"),
    tipo: Optional[str] = Query(None, description="LEY, DECRETO, RESOLUCION, etc."),
    limit: int = Query(20, le=50),
):
    """
    Busca normas. Estrategia en cascada:
    1. Catálogo curado (inmediato, siempre funciona)
    2. CSV de datos.gob.ar (si cargó)
    3. Scraping directo de infoleg (último recurso)
    """
    cached = cache_get(f"buscar:{q}:{tipo}")
    if cached:
        return cached

    q_norm = normalizar(q)
    resultados = []

    # ── 1. Catálogo curado — siempre disponible, respuesta inmediata ──
    for ley in LEYES_DESTACADAS:
        titulo  = ley.get("titulo", "")
        tags    = " ".join(ley.get("tags", []))
        resumen = ley.get("resumen", "")
        if (q_norm in normalizar(titulo) or
            q_norm in normalizar(tags) or
            q_norm in normalizar(resumen) or
            q_norm in normalizar(ley.get("numero", ""))):
            if tipo and ley.get("tipo", "").upper() != tipo.upper():
                continue
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

    # ── 2. CSV de datos.gob.ar (si cargó) ──
    if _normas_db:
        ids_ya = {r["id"] for r in resultados}
        for row in _normas_db:
            titulo = row.get("titulo_sumario", "") or row.get("titulo_resumido", "")
            if q_norm in normalizar(titulo):
                if tipo and row.get("tipo_norma", "").upper() != tipo.upper():
                    continue
                try:
                    norma_id = int(row.get("id_norma") or row.get("norma_id") or 0)
                except (TypeError, ValueError):
                    continue
                if norma_id in ids_ya or norma_id == 0:
                    continue
                resultados.append({
                    "id": norma_id,
                    "tipo": row.get("tipo_norma", ""),
                    "numero": row.get("numero_norma", ""),
                    "organismo": row.get("organismo_origen", ""),
                    "fecha": row.get("fecha_boletin", ""),
                    "titulo": titulo,
                    "url_infoleg": infoleg_meta_url(norma_id),
                    "url_texto": infoleg_texto_url(norma_id),
                    "fuente": "csv_infoleg",
                })
                if len(resultados) >= limit:
                    break

    # ── 3. Scraping directo de infoleg si no hay nada todavía ──
    if not resultados:
        resultados = await _scrape_busqueda_infoleg(q, tipo, limit)

    resultados = resultados[:limit]
    cache_set(f"buscar:{q}:{tipo}", resultados)
    return resultados


async def _scrape_busqueda_infoleg(q: str, tipo: Optional[str], limit: int) -> list:
    """Scraping directo del buscador de infoleg como fallback."""
    url = "https://servicios.infoleg.gob.ar/infolegInternet/buscar.do"
    params = {
        "METHOD": "buscar",
        "TIPO_NORMA": tipo or "",
        "NUMERO": "",
        "ANIO": "",
        "ORGANISMO": "",
        "TITULO_SUMARIO": q,
        "TEXTO_ACTIVO": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=20, headers=HEADERS) as client:
            r = await client.get(url, params=params)
            soup = BeautifulSoup(r.text, "html.parser")
            resultados = []
            for row in soup.select("table.table tr")[1:limit+1]:
                cols = row.find_all("td")
                if len(cols) >= 4:
                    link = cols[0].find("a")
                    norma_id = None
                    if link and "id=" in link.get("href", ""):
                        norma_id = re.search(r"id=(\d+)", link["href"])
                        norma_id = int(norma_id.group(1)) if norma_id else None
                    resultados.append({
                        "id": norma_id,
                        "tipo": cols[0].text.strip(),
                        "numero": cols[1].text.strip(),
                        "fecha": cols[2].text.strip(),
                        "titulo": cols[3].text.strip(),
                        "url_infoleg": infoleg_meta_url(norma_id) if norma_id else None,
                    })
            return resultados
    except Exception as e:
        return [{"error": str(e)}]


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
    tipo: Optional[str] = Query(None, description="LEY, RESOLUCION, DECLARACION, etc."),
):
    """Scrapea el buscador de proyectos de Diputados."""
    cached = cache_get(f"diputados:{q}:{anio}:{tipo}")
    if cached:
        return cached

    # El buscador de diputados acepta GET con estos params
    url = "https://www.diputados.gov.ar/proyectos/proyectosbusqueda.json"
    params = {
        "tipo": tipo or "todos",
        "estado": "todos",
        "palabras": q,
        "fechaDesde": f"01/01/{anio}" if anio else "",
        "fechaHasta": "",
    }

    resultados = []
    try:
        async with httpx.AsyncClient(timeout=25, headers=HEADERS, follow_redirects=True) as client:
            # Intento 1: endpoint JSON no documentado
            r = await client.get(url, params=params)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                data = r.json()
                resultados = data.get("proyectos", data if isinstance(data, list) else [])
            else:
                # Intento 2: scraping HTML del formulario
                resultados = await _scrape_diputados_html(q, anio, tipo, client)
    except Exception as e:
        resultados = await _scrape_diputados_html_fallback(q, anio, tipo)

    cache_set(f"diputados:{q}:{anio}:{tipo}", resultados)
    return resultados


async def _scrape_diputados_html(q: str, anio: Optional[int], tipo: Optional[str], client) -> list:
    """Scraping HTML del buscador de Diputados."""
    url = "https://www.diputados.gov.ar/proyectos/resultado.html"
    data = {
        "chkTipoDocumento[]": tipo or "DL",
        "strCamaraOrigen": "AMBAS",
        "strPeriodoInicio": str(anio) if anio else "",
        "strTema": q,
        "btnBuscar": "Buscar",
    }
    try:
        r = await client.post(url, data=data, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        proyectos = []
        for row in soup.select("table.table-bordered tr")[1:21]:
            cols = row.find_all("td")
            if len(cols) >= 5:
                link = cols[0].find("a")
                exp = cols[0].text.strip()
                proyectos.append({
                    "expediente": exp,
                    "tipo": cols[1].text.strip(),
                    "titulo": cols[4].text.strip(),
                    "autores": cols[2].text.strip(),
                    "fecha": cols[3].text.strip(),
                    "url": f"https://www.diputados.gov.ar{link['href']}" if link else None,
                    "camara": "Diputados",
                })
        return proyectos
    except Exception as e:
        return [{"error": f"Diputados no responde: {str(e)}", "sugerencia": "Intentar más tarde"}]


async def _scrape_diputados_html_fallback(q: str, anio: Optional[int], tipo: Optional[str]) -> list:
    async with httpx.AsyncClient(timeout=20, headers=HEADERS, follow_redirects=True) as client:
        return await _scrape_diputados_html(q, anio, tipo, client)


# ─── Proyectos en Senado ──────────────────────────────────────────────────────

@app.get("/api/proyectos/senado")
async def proyectos_senado(
    q: str = Query(...),
    anio: Optional[int] = Query(None),
):
    """Scrapea el buscador de proyectos del Senado."""
    cached = cache_get(f"senado:{q}:{anio}")
    if cached:
        return cached

    url = "https://www.senado.gob.ar/parlamentario/parlamentaria/busquedaAvanzada/search"
    params = {
        "pageNum": 1,
        "orderBy": "fecha",
        "tipoDoc": "PL",  # Proyectos de Ley
        "textoBusqueda": q,
        "anio": anio or "",
    }

    resultados = []
    try:
        async with httpx.AsyncClient(timeout=25, headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.select(".resultado-item, .search-result, table tr")[1:20]:
                cols = row.find_all("td")
                if len(cols) >= 3:
                    link = row.find("a")
                    resultados.append({
                        "expediente": cols[0].text.strip() if cols else "",
                        "titulo": cols[-1].text.strip() if cols else row.text.strip()[:200],
                        "fecha": cols[1].text.strip() if len(cols) > 1 else "",
                        "url": f"https://www.senado.gob.ar{link['href']}" if link and link.get("href") else None,
                        "camara": "Senado",
                    })
    except Exception as e:
        resultados = [{"error": f"Senado no responde: {str(e)}"}]

    cache_set(f"senado:{q}:{anio}", resultados)
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


# ─── Comparador con IA ────────────────────────────────────────────────────────

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

@app.post("/api/comparar")
async def comparar_leyes(req: ComparadorRequest):
    """
    Llama a la API de Anthropic (Claude) para comparar dos normas.
    La API key se lee de la variable de entorno ANTHROPIC_KEY.
    """
    api_key = os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(500, detail="ANTHROPIC_KEY no configurada en Railway Variables")

    prompt = f"""Sos un experto en derecho argentino. Compará estas dos normas en 4 puntos concisos:
1. Relación entre ambas normas
2. Posibles conflictos o superposiciones
3. Cuál prevalece en caso de conflicto y por qué
4. Contexto político-jurídico actual de cada una

LEY A: {req.ley_a_titulo} ({req.ley_a_tipo} {req.ley_a_numero})
{req.ley_a_resumen}
{("Reforma propuesta: " + req.ley_a_reforma) if req.ley_a_reforma else ""}

LEY B: {req.ley_b_titulo} ({req.ley_b_tipo} {req.ley_b_numero})
{req.ley_b_resumen}
{("Reforma propuesta: " + req.ley_b_reforma) if req.ley_b_reforma else ""}"""

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
                "max_tokens": 1200,
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    if r.status_code != 200:
        raise HTTPException(502, detail=f"Error de Anthropic: {r.text[:200]}")

    data = r.json()
    texto = "".join(b.get("text", "") for b in data.get("content", []))
    return {"analisis": texto}
