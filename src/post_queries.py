"""
Carga de datos PostgreSQL → HANA POS_STAGING.

Lee articulos desde el PostgreSQL de cada tienda (stores.json)
y los escribe en la tabla POS_STAGING de HANA.

Tablas origen: article, article_extended (ae), department (d)
Tabla destino: Z_NCR_CO.Z_NCRCO.Pos_staging::POS_STAGING
"""

from __future__ import annotations

import re
import time
from datetime import datetime

import pandas as pd
from sqlalchemy import text

from config import logger, store_manager, db as hana_db

# Tablas HANA
_TABLA_POS_STAGING     = '"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING"'
_TABLA_POS_STAGING_LOG = '"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING_LOG"'

# Lectura desde el PostgreSQL de la tienda — mismo SELECT para la carga
# completa (populate_pos_staging) y para la actualización por SKU.
_SELECT_ARTICULO_PG = """
SELECT
    :tienda                                              AS tienda,
    LTRIM(TRIM(a.article_number::text), '0')          AS ean,
    COALESCE(ae.sku_number::bigint, a.mainplunumber::bigint)::text AS sku,
    CASE
        WHEN a.designation <> '' THEN a.designation
        ELSE (SELECT designation FROM article
              WHERE article_number =  a.mainplunumber
                AND designation <> ''
              LIMIT 1)
    END                                                  AS descripcion,
    CASE
        WHEN a.price IS NOT NULL AND a.price <> 0 THEN a.price
        ELSE (SELECT price FROM article
              WHERE article_number =  a.mainplunumber
                AND price <> 0
              LIMIT 1)
    END                                                  AS precio_pos,
    SUBSTRING(ae.ext_structure_code, 17, 1)              AS restringido_venta,
    TO_CHAR(TO_TIMESTAMP(a.datelastmodified::TEXT, 'YYMMDDHH24MISS'),
            'YYYY-MM-DD HH24:MI:SS')                      AS fecha_carga
FROM article a
LEFT JOIN article_extended ae ON a.article_number = ae.article_number
INNER JOIN department d       ON a.department_number = d.department_number
"""


# ============================================================
# LOG HELPER
# ============================================================

def _write_staging_log(
    tienda: str,
    inicio: str,
    fin: str,
    registros: int,
    estado: str,
    mensaje: str = "",
) -> None:
    """
    Escribe una fila en POS_STAGING_LOG.
    El ID se calcula como MAX(ID)+1 (XS Classic no expone IDENTITY desde JDBC).
    Falla silenciosamente para no enmascarar el error principal.
    """
    try:
        with hana_db.hana.connect() as conn:
            id_row = conn.execute(
                text(f'SELECT IFNULL(MAX("ID"), 0) + 1 FROM {_TABLA_POS_STAGING_LOG}')
            ).scalar()
            conn.execute(
                text(
                    f'INSERT INTO {_TABLA_POS_STAGING_LOG} '
                    f'("ID","TIENDA","INICIO","FIN","REGISTROS","ESTADO","MENSAJE") '
                    f'VALUES (:id,:tienda,:inicio,:fin,:registros,:estado,:mensaje)'
                ),
                {
                    "id":        int(id_row or 1),
                    "tienda":    tienda,
                    "inicio":    inicio,
                    "fin":       fin,
                    "registros": registros,
                    "estado":    estado[:10],
                    "mensaje":   mensaje[:500],
                },
            )
            conn.commit()
        logger.info("Log escrito en POS_STAGING_LOG: tienda=%s estado=%s", tienda, estado)
    except Exception as e:
        logger.warning("No se pudo escribir en POS_STAGING_LOG (tienda=%s): %s", tienda, e)


# ============================================================
# QUERIES
# ============================================================

def listar_tiendas_postgres() -> list[str]:
    """Lista las tiendas configuradas en stores.json."""
    return store_manager.list_stores()


def _deduplicar_ean(df: pd.DataFrame) -> pd.DataFrame:
    """
    Elimina duplicados de (TIENDA, EAN) antes del INSERT.

    El PK de POS_STAGING es (TIENDA, EAN) y algunos POS tienen artículos
    con EANs que colisionan tras quitar ceros a la izquierda (ej. E813:
    '649106836009' basura y '00649106836009' válido). Se prioriza la fila
    con SKU no vacío y distinto de '0', y luego la primera aparición.

    Returns
    -------
    pd.DataFrame
        df sin duplicados de (TIENDA, EAN).
    """
    df = df.copy()
    df["_prioridad"] = df["sku"].astype(str).str.strip().isin(["", "0"]).astype(int)
    df = df.sort_values(by="_prioridad", ascending=True)
    n_antes = len(df)
    df = df.drop_duplicates(subset=["tienda", "ean"], keep="first")
    n_descartadas = n_antes - len(df)
    if n_descartadas:
        logger.warning(
            "Dedupe (TIENDA,EAN): %d filas descartadas (PK violado)", n_descartadas
        )
    return df.drop(columns=["_prioridad"])


def _insertar_lotes_staging(conn, df: pd.DataFrame) -> None:
    """
    Inserta df en POS_STAGING por lotes de 500 filas.
    Asume que el DELETE previo ya se ejecutó sobre la misma conexión.
    """
    BATCH = 500
    insert_sql = text(
        f'INSERT INTO {_TABLA_POS_STAGING} '
        f'("TIENDA","EAN","SKU","DESCRIPCION","PRECIO_POS","RESTRINGIDO_VENTA","FECHA_CARGA") '
        f'VALUES (:tienda,:ean,:sku,:descripcion,:precio_pos,:restringido_venta,:fecha_carga)'
    )
    for i in range(0, len(df), BATCH):
        chunk = df.iloc[i:i + BATCH]
        rows = []
        for _, r in chunk.iterrows():
            rows.append({
                "tienda":            str(r["tienda"]) if pd.notna(r["tienda"]) else "",
                "ean":               str(r["ean"])    if pd.notna(r["ean"])    else "",
                "sku":               str(r["sku"])    if pd.notna(r["sku"])    else "",
                "descripcion":       str(r["descripcion"])       if pd.notna(r["descripcion"])       else None,
                "precio_pos":        str(r["precio_pos"])        if pd.notna(r["precio_pos"])        else None,
                "restringido_venta": str(r["restringido_venta"]) if pd.notna(r["restringido_venta"]) else None,
                "fecha_carga":       str(r["fecha_carga"])       if pd.notna(r["fecha_carga"])       else None,
            })
        conn.execute(insert_sql, rows)


def populate_pos_staging(tienda: str) -> dict:
    """
    Lee datos del PostgreSQL local de la tienda y los escribe en HANA POS_STAGING.

    Reemplaza el trigger XSJS que falla en XS Classic porque $.db no soporta
    conexiones externas a PostgreSQL.

    Parameters
    ----------
    tienda : str
        Código de tienda (ej: 'E802'). Debe existir en stores.json.

    Returns
    -------
    dict
        {"tienda": str, "registros": int, "duracion_ms": int, "ok": bool, "error"?: str}
    """
    start = time.time()
    inicio = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 1. Leer desde PostgreSQL ──────────────────────────────
    query_pg = f"""
    {_SELECT_ARTICULO_PG}
    ORDER BY ae.sku_number
    """
    
    try:
        engine_pg = store_manager.get_engine(tienda)
        df = pd.read_sql(text(query_pg), engine_pg, params={"tienda": tienda})
        df = _deduplicar_ean(df)
        logger.info("populate_pos_staging: %d filas leídas de PostgreSQL tienda=%s", len(df), tienda)
    except Exception as e:
        fin = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dur = int((time.time() - start) * 1000)
        logger.error("populate_pos_staging: error leyendo PostgreSQL tienda=%s: %s", tienda, e)
        _write_staging_log(tienda, inicio, fin, 0, "ERROR", f"Lectura PG: {e}")
        return {"tienda": tienda, "registros": 0, "duracion_ms": dur, "ok": False, "error": str(e)}

    # ── 2. Escribir en HANA POS_STAGING ───────────────────────
    try:
        with hana_db.hana.connect() as conn:
            # DELETE previo de esta tienda
            conn.execute(
                text(f'DELETE FROM {_TABLA_POS_STAGING} WHERE "TIENDA" = :t'),
                {"t": tienda},
            )

            # INSERT por lotes de 500
            _insertar_lotes_staging(conn, df)
            conn.commit()

        fin = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dur = int((time.time() - start) * 1000)
        logger.info("populate_pos_staging: %d registros escritos en HANA tienda=%s (%dms)", len(df), tienda, dur)
        _write_staging_log(tienda, inicio, fin, len(df), "OK")
        return {"tienda": tienda, "registros": len(df), "duracion_ms": dur, "ok": True}

    except Exception as e:
        fin = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dur = int((time.time() - start) * 1000)
        logger.error("populate_pos_staging: error escribiendo HANA tienda=%s: %s", tienda, e)
        _write_staging_log(tienda, inicio, fin, 0, "ERROR", f"Escritura HANA: {e}")
        return {"tienda": tienda, "registros": 0, "duracion_ms": dur, "ok": False, "error": str(e)}


def actualizar_pos_staging_por_sku(tienda: str, sku: str) -> dict:
    """
    Actualiza el staging de un único producto (SKU) para una tienda.

    Lee del PostgreSQL de la tienda todos los registros con ese SKU
    (puede haber varios EAN del mismo SKU) y los reemplaza en HANA
    POS_STAGING (DELETE por TIENDA+SKU + INSERT).

    Si el producto ya no existe en el POS de la tienda, elimina sus filas
    del staging (equivale a la baja que haría la carga completa).

    Returns
    -------
    dict
        {"tienda", "sku", "registros", "duracion_ms", "ok", "error"?}
    """
    sku = str(sku).strip()
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")
    if not sku:
        raise ValueError("SKU vacío")

    start = time.time()
    inicio = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    query_pg = f"""
    {_SELECT_ARTICULO_PG}
    WHERE COALESCE(ae.sku_number::bigint, a.mainplunumber::bigint)::text = :sku
    ORDER BY ae.sku_number
    """

    try:
        engine_pg = store_manager.get_engine(tienda)
        df = pd.read_sql(
            text(query_pg),
            engine_pg,
            params={"tienda": tienda, "sku": sku},
        )
        df = _deduplicar_ean(df)
        logger.info("actualizar_pos_staging_por_sku: %d filas leídas de PostgreSQL tienda=%s sku=%s", len(df), tienda, sku)
    except Exception as e:
        fin = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dur = int((time.time() - start) * 1000)
        logger.error("actualizar_pos_staging_por_sku: error leyendo PostgreSQL tienda=%s sku=%s: %s", tienda, sku, e)
        _write_staging_log(tienda, inicio, fin, 0, "ERROR", f"SKU {sku} — Lectura PG: {e}")
        return {"tienda": tienda, "sku": sku, "registros": 0, "duracion_ms": dur, "ok": False, "error": str(e)}

    try:
        with hana_db.hana.connect() as conn:
            # DELETE de las filas de este SKU (y reemplazo total abajo)
            conn.execute(
                text(f'DELETE FROM {_TABLA_POS_STAGING} WHERE "TIENDA" = :t AND "SKU" = :s'),
                {"t": tienda, "s": sku},
            )

            if not df.empty:
                _insertar_lotes_staging(conn, df)
            conn.commit()

        fin = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dur = int((time.time() - start) * 1000)
        mensaje = f"SKU {sku}"
        if df.empty:
            mensaje += " — no existe en POS, se eliminó del staging"
        logger.info("actualizar_pos_staging_por_sku: %d registros en staging tienda=%s sku=%s (%dms)", len(df), tienda, sku, dur)
        _write_staging_log(tienda, inicio, fin, len(df), "OK", mensaje)
        return {"tienda": tienda, "sku": sku, "registros": len(df), "duracion_ms": dur, "ok": True}

    except Exception as e:
        fin = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dur = int((time.time() - start) * 1000)
        logger.error("actualizar_pos_staging_por_sku: error escribiendo HANA tienda=%s sku=%s: %s", tienda, sku, e)
        _write_staging_log(tienda, inicio, fin, 0, "ERROR", f"SKU {sku} — Escritura HANA: {e}")
        return {"tienda": tienda, "sku": sku, "registros": 0, "duracion_ms": dur, "ok": False, "error": str(e)}

