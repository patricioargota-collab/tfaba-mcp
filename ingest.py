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


def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
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


def backfill_enumeracion(anio: int, vocalia_range=range(1, 9),
                         reg_desde=1, reg_hasta=6000, max_404_seguidos=60) -> dict:
    """Backfill histórico por patrón Sentencias{AÑO}/{vocalia}-{registro}.pdf.
    Corta cada vocalía tras 'max_404_seguidos' fallos consecutivos."""
    con = get_db()
    encontradas = 0
    for voc in vocalia_range:
        fallos = 0
        for reg in range(reg_desde, reg_hasta + 1):
            if fallos >= max_404_seguidos:
                break
            url = f"{BASE}/Sentencias{anio}/{voc}-{reg}.pdf"
            try:
                data = _get(url, binary=True, timeout=30)
                if not data or len(data) < 1000:
                    fallos += 1; continue
            except Exception:
                fallos += 1; continue
            fallos = 0
            rec = {"anio": str(anio), "vocalia": voc, "registro": reg,
                   "pdf_url": url, "sumario": "", "firmantes": ""}
            if upsert(con, rec):
                encontradas += 1
            time.sleep(0.15)  # throttling cortés
    tot = con.execute("SELECT COUNT(*) FROM sentencias").fetchone()[0]
    con.close()
    return {"anio": anio, "encontradas": encontradas, "total_indice": tot}


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        print(json.dumps(backfill_enumeracion(int(sys.argv[2])), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(actualizar_incremental(), ensure_ascii=False, indent=2))
