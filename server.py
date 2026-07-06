#!/usr/bin/env python3
"""
tfaba-mcp — Servidor MCP del Tribunal Fiscal de Apelación de Buenos Aires.

Herramientas (patrón homogéneo con tfa-tucuman-mcp / comarb-mcp):
  tfaba_buscar_sentencias   — búsqueda full-text FTS5
  tfaba_traer_sentencia     — texto completo paginado (~6000 chars)
  tfaba_listar_sentencias   — listado filtrado (sala / año / carátula / CUIT)
  tfaba_estado_indice       — estadísticas del índice
  tfaba_actualizar_indice   — refresco incremental desde el listado del sitio
"""
from __future__ import annotations
import os, sqlite3
from typing import Optional
from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP

import ingest

PAGE_CHARS = 6000

# --- Servidor: stateless + JSON + bind Railway + sin DNS-rebinding ----------- #
# Fixes conocidos de despliegue en Railway:
#   * PORT: Railway inyecta $PORT; hay que bindear 0.0.0.0 en ese puerto.
#   * DNS rebinding: el guard de host del transporte streamable-http rechaza el
#     dominio *.up.railway.app; se desactiva vía settings.
mcp = FastMCP(
    "tfaba",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8080")),
    stateless_http=True,
    json_response=True,
)
# Desactivar protección DNS-rebinding (el proxy de Railway reescribe el Host).
try:
    from mcp.server.transport_security import TransportSecuritySettings
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False, allowed_hosts=["*"], allowed_origins=["*"])
except Exception:
    pass


def _row_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["organismo"] = ingest.ORGANISMOS.get(d.get("origen") or "", d.get("origen"))
    d["sala_romana"] = {1: "I", 2: "II", 3: "III", 99: "Única"}.get(d.get("sala"))
    return d


# --------------------------------------------------------------------------- #
class BuscarInput(BaseModel):
    consulta: str = Field(..., description="Términos de búsqueda full-text (voces, carátula, "
                          "doctrina o texto). Soporta operadores FTS5: AND, OR, NEAR, comillas para frases.")
    sala: Optional[int] = Field(None, description="Filtrar por sala: 1, 2 o 3.")
    anio: Optional[int] = Field(None, description="Filtrar por año de la sentencia (ej. 2026).")
    limite: int = Field(10, ge=1, le=50, description="Máximo de resultados (1-50).")


@mcp.tool(description="Busca sentencias del TFABA por texto (carátula, voces, doctrina o cuerpo "
          "completo) usando FTS5. Devuelve metadata + fragmento (snippet). Para el texto íntegro "
          "usar tfaba_traer_sentencia con el id devuelto.")
def tfaba_buscar_sentencias(input: BuscarInput) -> dict:
    con = ingest.get_db()
    q = ("SELECT s.id, s.fecha, s.sala, s.caratula, s.expediente, s.origen, s.cuit, "
         "s.firmantes, s.nro_gde, s.pdf_url, "
         "snippet(sentencias_fts,4,'«','»','…',18) AS extracto "
         "FROM sentencias_fts f JOIN sentencias s ON s.rowid=f.rowid "
         "WHERE sentencias_fts MATCH ?")
    args: list = [input.consulta]
    if input.sala is not None:
        q += " AND s.sala=?"; args.append(input.sala)
    if input.anio is not None:
        q += " AND s.anio=?"; args.append(input.anio)
    q += " ORDER BY rank LIMIT ?"; args.append(input.limite)
    try:
        rows = con.execute(q, args).fetchall()
    except sqlite3.OperationalError as e:
        con.close()
        return {"error": f"Consulta FTS inválida: {e}. Revisá la sintaxis (comillas, operadores)."}
    con.close()
    return {"cantidad": len(rows), "resultados": [_row_dict(r) for r in rows]}


# --------------------------------------------------------------------------- #
class TraerInput(BaseModel):
    id: str = Field(..., description="Identificador de la sentencia (formato {anio}-{vocalia}-{registro}, "
                    "ej. '2026-1-2705'), tal como lo devuelve buscar o listar.")
    pagina: int = Field(1, ge=1, description="Página del texto (cada página ~6000 caracteres).")


@mcp.tool(description="Devuelve el texto completo de una sentencia del TFABA, paginado (~6000 "
          "caracteres por página), junto con su metadata. Usar el id de buscar/listar.")
def tfaba_traer_sentencia(input: TraerInput) -> dict:
    con = ingest.get_db()
    r = con.execute("SELECT * FROM sentencias WHERE id=?", (input.id,)).fetchone()
    con.close()
    if not r:
        return {"error": f"No existe la sentencia '{input.id}'. Verificá el id con tfaba_buscar_sentencias."}
    d = _row_dict(r)
    texto = d.pop("texto") or ""
    total = max(1, -(-len(texto) // PAGE_CHARS))
    ini = (input.pagina - 1) * PAGE_CHARS
    d["texto"] = texto[ini:ini + PAGE_CHARS]
    d["pagina"] = input.pagina
    d["paginas_totales"] = total
    return d


# --------------------------------------------------------------------------- #
class ListarInput(BaseModel):
    sala: Optional[int] = Field(None, description="Sala 1, 2 o 3.")
    anio: Optional[int] = Field(None, description="Año de la sentencia.")
    caratula: Optional[str] = Field(None, description="Coincidencia parcial en la carátula (contribuyente).")
    cuit: Optional[str] = Field(None, description="CUIT del contribuyente (formato NN-NNNNNNNN-N).")
    origen: Optional[str] = Field(None, description="Organismo de origen: 2360 (ARBA), 2306, 2302, 2335, 2403.")
    limite: int = Field(20, ge=1, le=100)


@mcp.tool(description="Lista sentencias del TFABA filtrando por sala, año, carátula, CUIT u "
          "organismo de origen, ordenadas por fecha descendente. Sin texto completo.")
def tfaba_listar_sentencias(input: ListarInput) -> dict:
    con = ingest.get_db()
    q = ("SELECT id, fecha, sala, caratula, expediente, origen, cuit, firmantes, nro_gde, pdf_url "
         "FROM sentencias WHERE 1=1")
    args: list = []
    if input.sala is not None: q += " AND sala=?"; args.append(input.sala)
    if input.anio is not None: q += " AND anio=?"; args.append(input.anio)
    if input.caratula: q += " AND caratula LIKE ?"; args.append(f"%{input.caratula}%")
    if input.cuit: q += " AND cuit=?"; args.append(input.cuit)
    if input.origen: q += " AND origen=?"; args.append(input.origen)
    q += " ORDER BY fecha DESC, registro DESC LIMIT ?"; args.append(input.limite)
    rows = con.execute(q, args).fetchall()
    con.close()
    return {"cantidad": len(rows), "resultados": [_row_dict(r) for r in rows]}


# --------------------------------------------------------------------------- #
@mcp.tool(description="Estadísticas del índice de sentencias del TFABA: total, desglose por año "
          "y por sala, cantidad con texto, y fecha de la última sentencia indexada.")
def tfaba_estado_indice() -> dict:
    con = ingest.get_db()
    tot = con.execute("SELECT COUNT(*) FROM sentencias").fetchone()[0]
    con_txt = con.execute("SELECT COUNT(*) FROM sentencias WHERE length(texto)>200").fetchone()[0]
    por_anio = {str(r[0]): r[1] for r in con.execute(
        "SELECT anio, COUNT(*) FROM sentencias GROUP BY anio ORDER BY anio DESC")}
    por_sala = {str(r[0]): r[1] for r in con.execute(
        "SELECT sala, COUNT(*) FROM sentencias GROUP BY sala")}
    ult = con.execute("SELECT MAX(fecha) FROM sentencias").fetchone()[0]
    con.close()
    return {"total": tot, "con_texto": con_txt, "por_anio": por_anio,
            "por_sala": por_sala, "ultima_fecha": ult}


# --------------------------------------------------------------------------- #
class ActualizarInput(BaseModel):
    max_nuevas: int = Field(75, ge=1, le=200,
                            description="Tope de sentencias nuevas a incorporar en esta corrida.")


@mcp.tool(description="Refresca el índice incorporando las últimas sentencias publicadas en el "
          "listado del sitio del TFABA (vía incremental). Idempotente: sólo agrega las que faltan. "
          "El backfill histórico masivo se corre aparte por script.")
def tfaba_actualizar_indice(input: ActualizarInput) -> dict:
    # Corrida acotada para no exceder el timeout de gateway de Railway.
    return ingest.actualizar_incremental(max_nuevas=input.max_nuevas)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
