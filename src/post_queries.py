"""
Carga de datos PostgreSQL → HANA POS_STAGING.

Lee articulos desde el PostgreSQL de cada tienda (stores.json)
y los escribe en la tabla POS_STAGING de HANA.

Tablas origen: article, article_extended (ae), department (d)
Tabla destino: Z_NCR_CO.Z_NCRCO.Pos_staging::POS_STAGING
"""

from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
from sqlalchemy import text

from config import logger, store_manager, db as hana_db

# Tablas HANA
_TABLA_POS_STAGING     = '"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING"'
_TABLA_POS_STAGING_LOG = '"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING_LOG"'


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
    query_pg = """
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
    ORDER BY ae.sku_number
    """
    
    try:
        engine_pg = store_manager.get_engine(tienda)
        df = pd.read_sql(text(query_pg), engine_pg, params={"tienda": tienda})
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

