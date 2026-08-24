"""
Consultas a SAP HANA — Monitor POST vs HANA.
"""
from __future__ import annotations

import re

import pandas as pd
from sqlalchemy import text

from config import db, logger, store_manager

# Vista de comparacion HANA vs POS_STAGING (parametro $$WERKS_RUN$$)
VISTA_COMPARACION = '"_SYS_BIC"."Z_NCRCO.Pos_staging/POS_Comparacion"'

# Tabla de log de staging
_TABLA_POS_STAGING_LOG = '"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING_LOG"'



def es_subarticulo(df: pd.DataFrame) -> pd.Series:
    """
    True si la fila es un subarticulo EAN: sin restriccion de venta en POS
    (restringido_pos NULL o vacio) y con match en POS (no es solo HANA).

    Estas filas se excluyen de la vista principal y de los counts de diferencias.
    """
    if "restringido_pos" not in df.columns or "not_exist_pos" not in df.columns:
        return pd.Series(False, index=df.index)
    sin_restriccion = df["restringido_pos"].isna() | (
        df["restringido_pos"].astype(str).str.strip() == ""
    )
    existe_en_pos = ~df["not_exist_pos"].fillna(0).astype(bool)
    return sin_restriccion & existe_en_pos


def get_resumen_tiendas() -> pd.DataFrame:
    """
    Resumen de diferencias por tienda.

    Itera las tiendas de stores.json, llama a get_detalle_tienda()
    y agrega los conteos. Devuelve una fila por tienda con estado.
    """
    tiendas = store_manager.list_stores()
    if not tiendas:
        logger.warning("get_resumen_tiendas: no hay tiendas en stores.json.")
        return pd.DataFrame()

    filas = []
    for t in tiendas:
        try:
            df = get_detalle_tienda(t)
            # Los subarticulos EAN (restringido_pos NULL que existen en POS)
            # no se consideran en los counts de diferencias.
            df = df[~es_subarticulo(df)]
            cant_precio       = int(df["diff_precio"].fillna(0).astype(bool).sum())       if "diff_precio"       in df.columns else 0
            cant_hana         = int(df["not_exist_pos"].fillna(0).astype(bool).sum())       if "not_exist_pos"     in df.columns else 0
            cant_restringido  = int(df["diff_restringido"].fillna(0).astype(bool).sum())   if "diff_restringido" in df.columns else 0
            total = cant_precio + cant_hana + cant_restringido

            ultima = ""
            if "fecha_carga_pos" in df.columns:
                validas = df["fecha_carga_pos"].dropna()
                if not validas.empty:
                    ultima = str(validas.iloc[0])

            if total == 0:
                estado = "OK"
            elif total < 50:
                estado = "ALERTA"
            else:
                estado = "CRITICO"

            filas.append({
                "tienda":                 t,
                "cant_diffs_precio":      cant_precio,
                "cant_solo_hana":         cant_hana,
                "cant_solo_post":         0,
                "cant_diffs_restringido": cant_restringido,
                "total_diffs":            total,
                "ultima_carga_post":      ultima,
                "estado":                 estado,
            })
        except Exception as e:
            logger.error("get_resumen_tiendas: error en tienda %s: %s", t, e)
            filas.append({
                "tienda":                 t,
                "cant_diffs_precio":      0,
                "cant_solo_hana":         0,
                "cant_solo_post":         0,
                "cant_diffs_restringido": 0,
                "total_diffs":            0,
                "ultima_carga_post":      "",
                "estado":                 "ERROR",
            })

    return pd.DataFrame(filas).sort_values("total_diffs", ascending=False).reset_index(drop=True)


def get_detalle_tienda(tienda: str) -> pd.DataFrame:
    """
    Detalle de diferencias para una tienda desde VISTA_COMPARACION.

    Columnas: tienda, sku, ean, descripcion_hana, descripcion_pos,
    precio_hana, precio_pos, restringido_pos, fecha_ult_mov, jobidn,
    origen_precio, precio_ant, fecha_carga_pos, diff_precio, not_exist_pos,
    tipo_diferencia (derivado: PRECIO | SOLO_HANA | OK).
    """
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")

    query = f"""
    SELECT
        "Tienda"                AS tienda,
        "Sku"                   AS sku,
        "EAN"                   AS ean,
        "Descripcion"           AS descripcion_hana,
        "POS_DESCRIPCION"       AS descripcion_pos,
        "Precio_POS"            AS precio_hana,
        "POS_PRECIO_POS"        AS precio_pos,
        "POS_RESTRINGIDO_VENTA" AS restringido_pos,
        "Restringido"           AS restringido_hana,
        "Surtido"               AS surtido_hana,
        "Fecha_Ult_Act"         AS fecha_ult_mov,
        "JOBIDN"                AS jobidn,
        "Origen_PRECIO_POS"     AS origen_precio,
        "Precio_Ant"            AS precio_ant,
        "POS_FECHA_CARGA"       AS fecha_carga_pos,
        "EAN_POS"               AS ean_pos,
        "DIFF_PRECIO"           AS diff_precio,
        "NOT_EXIST_POS"         AS not_exist_pos,
        "DIFF_RESTRINGIDO"      AS diff_restringido
    FROM {VISTA_COMPARACION}('PLACEHOLDER' = ('$$WERKS_RUN$$', '{tienda}'))
    ORDER BY "Sku"
    """
    logger.info("Ejecutando query HANA (comparacion) tienda=%s...", tienda)
    df = pd.read_sql(text(query), db.hana)

    if not df.empty:
        def _tipo(row) -> str:
            if row["not_exist_pos"]:
                return "SOLO_HANA"
            if row["diff_precio"]:
                return "PRECIO"
            if row["diff_restringido"]:
                return "RESTRINGIDO"
            return "OK"
        df["tipo_diferencia"] = df.apply(_tipo, axis=1)
    else:
        df["tipo_diferencia"] = pd.Series(dtype=str)

    logger.info("Diferencias para tienda %s: %d filas", tienda, len(df))
    return df

