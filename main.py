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
    """Construye la URL del texto de la norma en infoleg (rangos de 5000)."""
    base = (norma_id // 5000) * 5000
    top = base + 4999
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

# ─── Descarga del CSV de Infoleg ──────────────────────────────────────────────

# URLs directas de descarga (datos.gob.ar) - algunas pueden cambiar
CSV_URLS = [
    "https://infra.datos.gob.ar/catalog/jus/dataset/1/distribution/1.1/download/base-infoleg-normativa-nacional.csv",
    "https://datos.gob.ar/api/3/action/resources_show?id=jus-base-infoleg-normativa-nacional",
]

# Fallback hardcodeado: normas conocidas cuando CSV/scraping fallan (IDs válidos de Infoleg)
NORMAS_FALLBACK: list[dict] = [
    {"id": 174117, "tipo": "LEY", "numero": "26.639", "titulo": "LEY DE PRESUPUESTOS MINIMOS PARA LA PROTECCION DE GLACIARES Y DEL AMBIENTE PERIGLACIAL", "fecha": "28/10/2010", "organismo": "Congreso de la Nación"},
    {"id": 25552, "tipo": "LEY", "numero": "20.744", "titulo": "LEY DE CONTRATO DE TRABAJO", "fecha": "25/09/1974", "organismo": "Congreso de la Nación"},
    {"id": 136125, "tipo": "LEY", "numero": "26.331", "titulo": "LEY DE PRESUPUESTOS MINIMOS DE PROTECCION AMBIENTAL DE LOS BOSQUES NATIVOS", "fecha": "26/11/2007", "organismo": "Congreso de la Nación"},
    {"id": 34822, "tipo": "LEY", "numero": "20.247", "titulo": "LEY DE SEMILLAS Y CREACIONES FITOGENETICAS", "fecha": "30/03/1973", "organismo": "Congreso de la Nación"},
    {"id": 267573, "tipo": "LEY", "numero": "27.275", "titulo": "LEY DE DERECHO DE ACCESO A LA INFORMACION PUBLICA", "fecha": "14/09/2016", "organismo": "Congreso de la Nación"},
    {"id": 20999, "tipo": "LEY", "numero": "24.240", "titulo": "LEY DE DEFENSA DEL CONSUMIDOR", "fecha": "22/09/1993", "organismo": "Congreso de la Nación"},
    {"id": 968, "tipo": "LEY", "numero": "11.723", "titulo": "LEY DE PROPIEDAD INTELECTUAL", "fecha": "28/09/1933", "organismo": "Congreso de la Nación"},
    {"id": 24254, "tipo": "LEY", "numero": "20.417", "titulo": "LEY GENERAL DEL AMBIENTE", "fecha": "06/11/1973", "organismo": "Congreso de la Nación"},
]
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
    """Busca normas por texto en el catálogo de Infoleg."""
    cached = cache_get(f"buscar:{q}:{tipo}")
    if cached:
        return cached

    q_norm = normalizar(q)
    resultados = []

    # Búsqueda en CSV local
    if _normas_db:
        for row in _normas_db:
            titulo = row.get("titulo_sumario", "") or row.get("titulo_resumido", "")
            if q_norm in normalizar(titulo):
                if tipo and row.get("tipo_norma", "").upper() != tipo.upper():
                    continue
                resultados.append({
                    "id": row.get("id_norma") or row.get("norma_id"),
                    "tipo": row.get("tipo_norma", ""),
                    "numero": row.get("numero_norma", ""),
                    "organismo": row.get("organismo_origen", ""),
                    "fecha": row.get("fecha_boletin", ""),
                    "titulo": titulo,
                    "url_infoleg": infoleg_meta_url(int(row.get("id_norma") or row.get("norma_id") or 0)),
                })
                if len(resultados) >= limit:
                    break

    # Fallback: scraping del buscador de infoleg si CSV no cargó
    if not resultados:
        resultados = await _scrape_busqueda_infoleg(q, tipo, limit)

    # Fallback final: normas hardcodeadas cuando todo falla
    if not resultados:
        for n in NORMAS_FALLBACK:
            titulo_n = normalizar(n.get("titulo", ""))
            palabras = [p for p in q_norm.split() if len(p) >= 2]
            coincide = q_norm in titulo_n or (palabras and all(p in titulo_n for p in palabras))
            if coincide:
                if tipo and n.get("tipo", "").upper() != tipo.upper():
                    continue
                resultados.append({
                    "id": n["id"],
                    "tipo": n["tipo"],
                    "numero": n["numero"],
                    "titulo": n["titulo"],
                    "fecha": n.get("fecha", ""),
                    "organismo": n.get("organismo", ""),
                    "url_infoleg": infoleg_meta_url(n["id"]),
                })
                if len(resultados) >= limit:
                    break

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
