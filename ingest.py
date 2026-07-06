#!/usr/bin/env python3
"""
Ingesta de sentencias del Tribunal Fiscal de Apelación de Buenos Aires (TFABA).

Fuente: http://www.tfaba.gov.ar/Apartados/VerSentencias.asp  (últimas sentencias)
        http://www.tfaba.gov.ar/Sentencias{AÑO}/{vocalia}-{registro}.pdf

Notas de reconocimiento (2026-07):
- El sitio SOLO responde por HTTP (el certificado HTTPS tiene el SAN mal
  configurado). Usar siempre http://.
- El HTML viene en Windows-1252.
- Los PDF son documentos GDE con TEXTO NATIVO -> no requieren OCR.
- En el nombre de archivo, el primer número es la VOCALÍA (1-8), NO la sala.
  La sala real (I/II/III) se lee del cuerpo del PDF.
- El buscador histórico (VerJuris.asp) suele devolver "ERROR ARBA"; por eso la
  vía primaria es el listado + enumeración por patrón para el backfill.
"""
from __future__ import annotations
import re, html, sqlite3, subprocess, urllib.request, collections, os, tempfile, time

BASE = "http://www.tfaba.gov.ar"
LISTADO_URL = f"{BASE}/Apartados/VerSentencias.asp"
DB_PATH = os.environ.get("TFABA_DB", os.path.join(os.path.dirname(__file__), "tfaba.db"))
UA = "Mozilla/5.0 (compatible; tfaba-mcp/1.0)"

MESES = {m: i for i, m in enumerate(
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split(), 1)}
ROMANO = {"I": 1, "II": 2, "III": 3, "IV": 4}
ORGANISMOS = {"2306": "Ministerio de Economía - Rentas",
              "2302": "Tribunal Fiscal de Apelación",
              "2335": "Ministerio de Economía - Catastro",
              "2360": "ARBA",
              "2403": "Dirección Provincial de la Energía"}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _get(url: str, binary: bool = False, timeout: int = 45):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    data = urllib.request.urlopen(req, timeout=timeout).read()
    return data if binary else data.decode("windows-1252", errors="replace")


def _clean(t: str) -> str:
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


# --------------------------------------------------------------------------- #
# Listado
# --------------------------------------------------------------------------- #
def scrape_listado() -> list[dict]:
    page = _get(LISTADO_URL)
    registros = []
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        row = m.group(1)
        pdf = re.search(r'href="([^"]+\.pdf)"', row, re.I)
        if not pdf:
            continue
        url = pdf.group(1)
        if not url.lower().startswith("http"):
            url = f"{BASE}/{url.lstrip('/')}"
        fn = re.search(r"Sentencias(\d{4})/(\d+)-(\d+)\.pdf", url, re.I)
        if not fn:
            continue
        celdas = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)]
        sumario = ""
        for c in celdas:
            if len(c) > len(sumario) and "Texto completo" not in c and c != "Extracto":
                sumario = c
        firmantes = ""
        fm = re.search(r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s*[–-]\s*[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,3})\s*$", sumario)
        if fm:
            firmantes = re.sub(r"\s*[–-]\s*", " - ", fm.group(1))
        registros.append({
            "anio": fn.group(1), "vocalia": int(fn.group(2)), "registro": int(fn.group(3)),
            "pdf_url": url, "sumario": sumario, "firmantes": firmantes,
        })
    return registros


# --------------------------------------------------------------------------- #
# PDF -> metadata + texto
# --------------------------------------------------------------------------- #
def pdf_a_texto(pdf_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes); path = f.name
    try:
        return subprocess.run(["pdftotext", "-layout", path, "-"],
                              capture_output=True, text=True, timeout=90).stdout
    finally:
        os.unlink(path)


def parse_metadata(txt: str) -> dict:
    d: dict = {}
    m = re.search(r"Número:\s*(INLEG-\d{4}-\d+-GDEBA-TFA)", txt)
    d["nro_gde"] = m.group(1) if m else None
    m = re.search(r"(\d{1,2}) de ([A-Za-zñáéíóú]+) de (\d{4})", txt)
    d["fecha"] = (f"{m.group(3)}-{MESES[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
                  if m and m.group(2).lower() in MESES else None)
    ctx = re.search(r"(?:Referencia:|expediente\s+n[úu]mero|AUTOS Y VISTOS.{0,40})(.{0,120})", txt, re.I | re.S)
    zona = ctx.group(1) if ctx else txt
    m = (re.search(r"\b(2306|2302|2335|2360|2403)-?(\d{6,})\b", zona)
         or re.search(r"\b(2306|2302|2335|2360|2403)-?(\d{6,})\b", txt))
    if m:
        d["expediente"] = f"{m.group(1)}-{m.group(2)}"
        d["origen"] = m.group(1)
        ay = re.search(r"a[ñn]o\s*(\d{2,4})|/\s*(\d{2,4})", zona)
        d["exp_anio"] = (ay.group(1) or ay.group(2)) if ay else None
    else:
        d["expediente"] = d["origen"] = d["exp_anio"] = None
    m = re.search(r"caratulad[oa]s?\s*[“\"']([^”\"']+)[”\"']", txt, re.I) \
        or re.search(r"Referencia:\s*[“\"']([^”\"']+)[”\"']", txt)
    d["caratula"] = re.sub(r"\s+", " ", m.group(1)).strip() if m else None
    m = re.search(r"C\.?U\.?I\.?T\.?\s*(?:N[°º]\s*)?(\d{2}-\d{8}-\d)", txt)
    d["cuit"] = m.group(1) if m else None
    salas = re.findall(r"\bSala\s+(I{1,3}V?|IV)\b", txt)
    if salas:
        c = collections.Counter(s.upper() for s in salas)
        d["sala"] = ROMANO.get(c.most_common(1)[0][0])
    else:
        d["sala"] = None
    return d


# --------------------------------------------------------------------------- #
# SQLite + FTS5
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS sentencias (
    id TEXT PRIMARY KEY,           -- {anio}-{vocalia}-{registro}
    anio INTEGER, vocalia INTEGER, registro INTEGER,
    sala INTEGER, nro_gde TEXT, fecha TEXT,
    expediente TEXT, origen TEXT, exp_anio TEXT,
    caratula TEXT, cuit TEXT, firmantes TEXT,
    sumario TEXT, pdf_url TEXT, texto TEXT,
    indexado_en TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS sentencias_fts USING fts5(
    id UNINDEXED, caratula, sumario, firmantes, texto,
    content='sentencias', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS sentencias_ai AFTER INSERT ON sentencias BEGIN
    INSERT INTO sentencias_fts(rowid, id, caratula, sumario, firmantes, texto)
    VALUES (new.rowid, new.id, new.caratula, new.sumario, new.firmantes, new.texto);
END;
CREATE TRIGGER IF NOT EXISTS sentencias_ad AFTER DELETE ON sentencias BEGIN
    INSERT INTO sentencias_fts(sentencias_fts, rowid, id, caratula, sumario, firmantes, texto)
    VALUES ('delete', old.rowid, old.id, old.caratula, old.sumario, old.firmantes, old.texto);
END;
"""


SCHEMA_PROGRESO = """
CREATE TABLE IF NOT EXISTS backfill_progreso (
    vocalia INTEGER PRIMARY KEY,
    proximo_reg INTEGER,   -- desde dónde seguir bajando (hacia atrás)
    agotada INTEGER DEFAULT 0,
    hallados INTEGER DEFAULT 0
);
"""


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.executescript(SCHEMA_PROGRESO)
    return con


def _id(anio, vocalia, registro) -> str:
    return f"{anio}-{vocalia}-{registro}"


def upsert(con: sqlite3.Connection, rec: dict) -> bool:
    """Descarga el PDF, parsea y guarda. Devuelve True si insertó/actualizó."""
    sid = _id(rec["anio"], rec["vocalia"], rec["registro"])
    ya = con.execute("SELECT texto FROM sentencias WHERE id=? AND texto IS NOT NULL AND length(texto)>200",
                     (sid,)).fetchone()
    if ya:
        return False
    try:
        txt = pdf_a_texto(_get(rec["pdf_url"], binary=True))
    except Exception:
        txt = ""
    meta = parse_metadata(txt) if txt else {}
    con.execute("DELETE FROM sentencias WHERE id=?", (sid,))
    con.execute("""INSERT INTO sentencias
        (id, anio, vocalia, registro, sala, nro_gde, fecha, expediente, origen,
         exp_anio, caratula, cuit, firmantes, sumario, pdf_url, texto)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, int(rec["anio"]), rec["vocalia"], rec["registro"],
         meta.get("sala"), meta.get("nro_gde"), meta.get("fecha"),
         meta.get("expediente"), meta.get("origen"), meta.get("exp_anio"),
         meta.get("caratula"), meta.get("cuit"), rec.get("firmantes"),
         rec.get("sumario"), rec["pdf_url"], txt))
    con.commit()
    return True


def actualizar_incremental(max_nuevas: int = 100) -> dict:
    """Vía primaria: scrapea el listado y agrega las que falten."""
    con = get_db()
    regs = scrape_listado()
    nuevas = 0
    for r in regs:
        if nuevas >= max_nuevas:
            break
        if upsert(con, r):
            nuevas += 1
    tot = con.execute("SELECT COUNT(*) FROM sentencias").fetchone()[0]
    con.close()
    return {"listado": len(regs), "nuevas": nuevas, "total_indice": tot}


def _existe(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
        return urllib.request.urlopen(req, timeout=15).status == 200
    except Exception:
        return False


def semillas_por_vocalia() -> dict[int, int]:
    """Máximo registro conocido por vocalía, tomado del listado vigente del sitio.
    Sirve como punto de arranque para caminar hacia atrás en el backfill."""
    sem: dict[int, int] = {}
    for r in scrape_listado():
        v, reg = r["vocalia"], r["registro"]
        sem[v] = max(sem.get(v, 0), reg)
    return sem


AÑOS_CANDIDATOS = [2026, 2025, 2024, 2023, 2022, 2021, 2020, 2019]


def _resolver_carpeta(voc: int, reg: int, ultima: int | None) -> tuple[int | None, str | None]:
    """Ubica en qué carpeta-año vive Sentencias{AÑO}/{voc}-{reg}.pdf.
    Prueba primero la última carpeta exitosa (localidad de banda) para minimizar
    requests, luego el resto de años candidatos."""
    orden = ([ultima] + [a for a in AÑOS_CANDIDATOS if a != ultima]) if ultima else AÑOS_CANDIDATOS
    for a in orden:
        url = f"{BASE}/Sentencias{a}/{voc}-{reg}.pdf"
        if _existe(url):
            return a, url
    return None, None


def backfill_enumeracion(hasta_anio: int | None = None, vocalia_range=range(1, 9),
                         max_404_seguidos=40, tope_por_vocalia=4000) -> dict:
    """Backfill histórico por enumeración del correlativo continuo por vocalía.

    Diseño (validado en reconocimiento 2026-07):
      * El registro es un correlativo por vocalía que NO reinicia por año y cruza
        de carpeta al cambiar de año. Por eso se camina el correlativo hacia atrás
        desde el máximo conocido (semilla del listado), sin fijar el año.
      * Para cada registro se resuelve la carpeta-año por prueba, con localidad
        (la última carpeta exitosa se prueba primero).
      * La sentencia se clasifica por su año real (carpeta) y por la fecha del PDF.
      * El corte por racha de 404 marca el fin real de datos de la vocalía.

    hasta_anio: si se indica, corta la caminata cuando la carpeta resuelta es
    anterior a ese año (para backfills parciales, p.ej. hasta 2022).
    """
    con = get_db()
    semillas = semillas_por_vocalia()
    resumen: dict[str, int] = {}
    encontradas = 0
    for voc in vocalia_range:
        inicio = semillas.get(voc)
        if not inicio:
            continue
        fallos = 0; pasos = 0; ultima = None; reg = inicio + 1
        while fallos < max_404_seguidos and pasos < tope_por_vocalia and reg > 1:
            pasos += 1; reg -= 1
            anio_c, url = _resolver_carpeta(voc, reg, ultima)
            if anio_c is None:
                fallos += 1
                continue
            fallos = 0; ultima = anio_c
            if hasta_anio and anio_c < hasta_anio:
                break
            rec = {"anio": str(anio_c), "vocalia": voc, "registro": reg,
                   "pdf_url": url, "sumario": "", "firmantes": ""}
            if upsert(con, rec):
                encontradas += 1
                resumen[str(anio_c)] = resumen.get(str(anio_c), 0) + 1
            time.sleep(0.12)  # throttling cortés
    tot = con.execute("SELECT COUNT(*) FROM sentencias").fetchone()[0]
    con.close()
    return {"por_anio": resumen, "encontradas": encontradas, "total_indice": tot}


def backfill_tramo(vocalia: int, max_nuevas: int = 15, tope_pasos: int = 400,
                   max_404_seguidos: int = 40) -> dict:
    """Backfill reanudable de UNA vocalía, en tramos cortos para no exceder el
    timeout del gateway. Guarda el progreso en 'backfill_progreso' y se retoma
    en la siguiente llamada. Devuelve cuando junta 'max_nuevas' o agota el tramo.
    """
    con = get_db()
    fila = con.execute("SELECT proximo_reg, agotada, hallados FROM backfill_progreso WHERE vocalia=?",
                       (vocalia,)).fetchone()
    if fila and fila["agotada"]:
        st = con.execute("SELECT MIN(registro), MAX(registro), COUNT(*) FROM sentencias WHERE vocalia=?",
                         (vocalia,)).fetchone()
        con.close()
        return {"vocalia": vocalia, "estado": "agotada", "nuevas": 0,
                "acumulado_vocalia": fila["hallados"],
                "rango_registros": [st[0], st[1]], "total_vocalia": st[2]}
    if fila and fila["proximo_reg"]:
        reg = fila["proximo_reg"]
        acum = fila["hallados"]
    else:
        semillas = semillas_por_vocalia()
        if vocalia not in semillas:
            con.close()
            return {"vocalia": vocalia, "estado": "sin_semilla",
                    "detalle": "La vocalía no aparece en el listado actual.", "nuevas": 0}
        reg = semillas[vocalia] + 1
        acum = 0

    nuevas = 0; fallos = 0; pasos = 0; ultima = None
    while nuevas < max_nuevas and pasos < tope_pasos and fallos < max_404_seguidos and reg > 1:
        pasos += 1; reg -= 1
        anio_c, url = _resolver_carpeta(vocalia, reg, ultima)
        if anio_c is None:
            fallos += 1
            continue
        fallos = 0; ultima = anio_c
        rec = {"anio": str(anio_c), "vocalia": vocalia, "registro": reg,
               "pdf_url": url, "sumario": "", "firmantes": ""}
        if upsert(con, rec):
            nuevas += 1; acum += 1

    agotada = 1 if (fallos >= max_404_seguidos or reg <= 1) else 0
    con.execute("""INSERT INTO backfill_progreso (vocalia, proximo_reg, agotada, hallados)
                   VALUES (?,?,?,?)
                   ON CONFLICT(vocalia) DO UPDATE SET proximo_reg=?, agotada=?, hallados=?""",
                (vocalia, reg, agotada, acum, reg, agotada, acum))
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM sentencias").fetchone()[0]
    por_anio = {str(r[0]): r[1] for r in con.execute(
        "SELECT anio, COUNT(*) FROM sentencias GROUP BY anio ORDER BY anio DESC")}
    con.close()
    return {"vocalia": vocalia, "estado": "agotada" if agotada else "en_curso",
            "nuevas": nuevas, "acumulado_vocalia": acum, "proximo_registro": reg,
            "total_indice": tot, "por_anio": por_anio}


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        print(json.dumps(backfill_enumeracion(hasta_anio=int(sys.argv[2]) if len(sys.argv)>2 else None), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(actualizar_incremental(), ensure_ascii=False, indent=2))
