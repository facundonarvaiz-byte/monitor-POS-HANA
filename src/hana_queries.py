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


def _sql_resumen_tienda(tienda: str) -> str:
    """
    SELECT agregado de VISTA_COMPARACION para una tienda.

    Devuelve una sola fila con los conteos de diferencias calculados en
    HANA (evita traer el detalle completo de la tienda a memoria).
    Replica el filtro de subarticulos EAN de es_subarticulo().
    """
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")

    inner = (
        f"SELECT * FROM {VISTA_COMPARACION}('PLACEHOLDER' = "
        f"('$$WERKS_RUN$$', '{tienda}'))"
    )
    rest = 't."POS_RESTRINGIDO_VENTA"'
    notx = 't."NOT_EXIST_POS"'
    return (
        f"SELECT '{tienda}' AS tienda, "
        f"COALESCE(SUM(COALESCE(CAST(t.\"DIFF_PRECIO\" AS INTEGER), 0)), 0) AS cant_diffs_precio, "
        f"COALESCE(SUM(COALESCE(CAST(t.\"NOT_EXIST_POS\" AS INTEGER), 0)), 0) AS cant_solo_hana, "
        f"COALESCE(SUM(COALESCE(CAST(t.\"DIFF_RESTRINGIDO\" AS INTEGER), 0)), 0) AS cant_diffs_restringido, "
        f"MAX(t.\"POS_FECHA_CARGA\") AS ultima_carga_pos "
        f"FROM ({inner}) t "
        f"WHERE NOT (({rest} IS NULL OR TRIM({rest}) = '') "
        f"AND COALESCE({notx}, FALSE) = FALSE)"
    )


def _completar_resumen(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega columnas derivadas (cant_solo_post, total_diffs, estado),
    normaliza ultima_carga_pos a texto y ordena por total_diffs.
    """
    if df.empty:
        return df

    df["cant_solo_post"] = 0
    df["total_diffs"] = (
        df["cant_diffs_precio"] + df["cant_solo_hana"] + df["cant_diffs_restringido"]
    ).astype(int)

    df["estado"] = "CRITICO"
    df.loc[df["total_diffs"] < 50, "estado"] = "ALERTA"
    df.loc[df["total_diffs"] == 0, "estado"] = "OK"

    df["ultima_carga_pos"] = (
        df["ultima_carga_pos"].where(df["ultima_carga_pos"].notna(), "")
        .astype(str)
    )

    return df.sort_values("total_diffs", ascending=False).reset_index(drop=True)


def _resumen_secuencial(tiendas: list[str]) -> pd.DataFrame:
    """Resumen con una query agregada por tienda, aislando errores por tienda."""
    filas = []
    errores = set()
    for t in tiendas:
        try:
            df = pd.read_sql(text(_sql_resumen_tienda(t)), db.hana)
            r = df.iloc[0]
            filas.append({
                "tienda":                 t,
                "cant_diffs_precio":      int(r["cant_diffs_precio"]),
                "cant_solo_hana":         int(r["cant_solo_hana"]),
                "cant_diffs_restringido": int(r["cant_diffs_restringido"]),
                "ultima_carga_pos":       r["ultima_carga_pos"],
            })
        except Exception as e:
            logger.error("get_resumen_tiendas: error en tienda %s: %s", t, e)
            errores.add(t)
            filas.append({
                "tienda":                 t,
                "cant_diffs_precio":      0,
                "cant_solo_hana":         0,
                "cant_diffs_restringido": 0,
                "ultima_carga_pos":       "",
            })

    df = _completar_resumen(pd.DataFrame(filas))
    df.loc[df["tienda"].isin(errores), "estado"] = "ERROR"
    return df


def get_resumen_tiendas() -> pd.DataFrame:
    """
    Resumen de diferencias por tienda.

    Calcula los conteos en HANA con una sola query UNION ALL (una fila
    por tienda) en lugar de traer el detalle completo de cada tienda a
    memoria. Si la query conjunta falla, reintenta tienda por tienda
    para poder reportar errores individuales.
    """
    tiendas = store_manager.list_stores()
    if not tiendas:
        logger.warning("get_resumen_tiendas: no hay tiendas en stores.json.")
        return pd.DataFrame()

    try:
        query = " UNION ALL ".join(_sql_resumen_tienda(t) for t in tiendas)
        logger.info("Ejecutando query HANA (resumen agregado), tiendas=%d...", len(tiendas))
        return _completar_resumen(pd.read_sql(text(query), db.hana))
    except Exception as e:
        logger.error(
            "get_resumen_tiendas: falló el resumen conjunto (%s); reintento por tienda.", e
        )
        return _resumen_secuencial(tiendas)


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


def get_logs_staging(limite: int = 200) -> pd.DataFrame:
    """
    Últimos registros de POS_STAGING_LOG ordenados por ID descendente
    (los más recientes primero).

    Columnas: id, tienda, inicio, fin, registros, estado, mensaje.
    """
    query = f"""
    SELECT
        "ID"        AS id,
        "TIENDA"    AS tienda,
        "INICIO"    AS inicio,
        "FIN"       AS fin,
        "REGISTROS" AS registros,
        "ESTADO"    AS estado,
        "MENSAJE"   AS mensaje
    FROM {_TABLA_POS_STAGING_LOG}
    ORDER BY "ID" DESC
    LIMIT {int(limite)}
    """
    logger.info("Ejecutando query HANA (log staging), limite=%s...", limite)
    return pd.read_sql(text(query), db.hana)

