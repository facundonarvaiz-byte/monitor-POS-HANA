"""
Consultas a SAP HANA — Monitor POS vs HANA (materiales activos).
"""
from __future__ import annotations

import re

import pandas as pd
from sqlalchemy import text

from config import db, logger, get_store_manager, validar_ambiente

# Vista de comparacion HANA vs POS_STAGING (parametro $$WERKS_RUN$$)
VISTA_COMPARACION = '"_SYS_BIC"."Z_NCRCO.Pos_staging/POS_Comparacion"'

# Tabla de log de staging
_TABLA_POS_STAGING_LOG = '"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING_LOG"'

# Tabla de logs CP_CVE
_TABLA_CP_LOGS = '"Z_NCR_CO"."Z_NCRCO.CP_CVE::CP_LOGS"'



def _sql_resumen_tienda(tienda: str) -> str:
    """
    SELECT agregado de VISTA_COMPARACION para una tienda.

    Devuelve una sola fila con los conteos de diferencias calculados en
    HANA (evita traer el detalle completo de la tienda a memoria).

    Los conteos son de SKUs distintos, no de filas: un SKU puede tener
    varios EAN en el POS y la vista repite una fila por cada uno.
    La existencia en el POS se detecta por "EAN_POS" (columna real del
    staging), no por "NOT_EXIST_POS" (que da True si falta la descripcion).

    cant_diffs_total cuenta cada SKU una sola vez aunque tenga diff de
    precio y de restringido a la vez (evita el doble conteo del resumen).
    """
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")

    inner = (
        f"SELECT * FROM {VISTA_COMPARACION}('PLACEHOLDER' = "
        f"('$$WERKS_RUN$$', '{tienda}'))"
    )
    sku = 'COALESCE(t."Sku", t."Sku_POS")'
    return (
        f"SELECT '{tienda}' AS tienda, "
        f"COUNT(DISTINCT CASE WHEN CAST(t.\"DIFF_PRECIO\" AS INTEGER) = 1 "
        f"THEN {sku} END) AS cant_diffs_precio, "
        f"COUNT(DISTINCT CASE WHEN t.\"EAN_POS\" IS NULL AND t.\"EAN\" IS NOT NULL "
        f"THEN {sku} END) AS cant_solo_hana, "
        f"COUNT(DISTINCT CASE WHEN CAST(t.\"DIFF_RESTRINGIDO\" AS INTEGER) = 1 "
        f"THEN {sku} END) AS cant_diffs_restringido, "
        f"COUNT(DISTINCT CASE WHEN CAST(t.\"DIFF_PRECIO\" AS INTEGER) = 1 "
        f"OR CAST(t.\"DIFF_RESTRINGIDO\" AS INTEGER) = 1 "
        f"OR (t.\"EAN_POS\" IS NULL AND t.\"EAN\" IS NOT NULL) "
        f"THEN {sku} END) AS cant_diffs_total, "
        f"MAX(t.\"POS_FECHA_CARGA\") AS ultima_carga_pos "
        f"FROM ({inner}) t"
    )


def _completar_resumen(df: pd.DataFrame) -> pd.DataFrame:
    """
    Agrega columnas derivadas (total_diffs, estado), normaliza
    ultima_carga_pos a texto y ordena por total_diffs.

    El estado se calcula solo con precio, restringido y solo-HANA:
    OK hasta 50 diferencias, ALERTA hasta 300, CRITICO por encima de 300.
    """
    if df.empty:
        return df

    df["total_diffs"] = df["cant_diffs_total"].astype(int)

    df["estado"] = "CRITICO"
    df.loc[df["total_diffs"] <= 300, "estado"] = "ALERTA"
    df.loc[df["total_diffs"] <= 50, "estado"] = "OK"

    df["ultima_carga_pos"] = (
        df["ultima_carga_pos"].where(df["ultima_carga_pos"].notna(), "")
        .astype(str)
    )

    return df.sort_values("total_diffs", ascending=False).reset_index(drop=True)


def _resumen_secuencial(tiendas: list[str], ambiente: str) -> pd.DataFrame:
    """Resumen con una query agregada por tienda, aislando errores por tienda."""
    filas = []
    errores = set()
    for t in tiendas:
        try:
            df = pd.read_sql(text(_sql_resumen_tienda(t)), db.get_hana(ambiente))
            r = df.iloc[0]
            filas.append({
                "tienda":                 t,
                "cant_diffs_precio":      int(r["cant_diffs_precio"]),
                "cant_solo_hana":         int(r["cant_solo_hana"]),
                "cant_diffs_restringido": int(r["cant_diffs_restringido"]),
                "cant_diffs_total":       int(r["cant_diffs_total"]),
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
                "cant_diffs_total":       0,
                "ultima_carga_pos":       "",
            })

    df = _completar_resumen(pd.DataFrame(filas))
    df.loc[df["tienda"].isin(errores), "estado"] = "ERROR"
    return df


def get_resumen_tiendas(ambiente: str) -> pd.DataFrame:
    """
    Resumen de diferencias por tienda en el ambiente indicado.

    Calcula los conteos en HANA con una sola query UNION ALL (una fila
    por tienda) en lugar de traer el detalle completo de cada tienda a
    memoria. Si la query conjunta falla, reintenta tienda por tienda
    para poder reportar errores individuales.
    """
    ambiente = validar_ambiente(ambiente)
    tiendas = get_store_manager(ambiente).list_stores()
    if not tiendas:
        logger.warning("get_resumen_tiendas [%s]: no hay tiendas configuradas.", ambiente)
        return pd.DataFrame()

    try:
        query = " UNION ALL ".join(_sql_resumen_tienda(t) for t in tiendas)
        logger.info(
            "Ejecutando query HANA [%s] (resumen agregado), tiendas=%d...",
            ambiente,
            len(tiendas),
        )
        return _completar_resumen(pd.read_sql(text(query), db.get_hana(ambiente)))
    except Exception as e:
        logger.error(
            "get_resumen_tiendas [%s]: falló el resumen conjunto (%s); reintento por tienda.",
            ambiente,
            e,
        )
        return _resumen_secuencial(tiendas, ambiente)


def get_detalle_tienda(tienda: str, ambiente: str) -> pd.DataFrame:
    """
    Detalle de diferencias para una tienda desde VISTA_COMPARACION.

    Columnas: tienda, sku, ean, descripcion_hana, descripcion_pos,
    precio_hana, precio_pos, restringido_pos, fecha_ult_mov, jobidn,
    origen_precio, precio_ant, fecha_carga_pos, diff_precio, not_exist_pos,
    tipo_diferencia (derivado: PRECIO | RESTRINGIDO | SOLO_HANA | OK).

    Solo se consideran materiales activos: las filas solo-POS (sin EAN de
    HANA) quedan excluidas. El sku se coalesce con Sku_POS.
    """
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")
    ambiente = validar_ambiente(ambiente)

    query = f"""
    SELECT
        "Tienda"                AS tienda,
        COALESCE("Sku", "Sku_POS") AS sku,
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
    WHERE "EAN" IS NOT NULL
    ORDER BY "Sku"
    """
    logger.info("Ejecutando query HANA [%s] (comparacion) tienda=%s...", ambiente, tienda)
    df = pd.read_sql(text(query), db.get_hana(ambiente))

    if not df.empty:
        for col in ("diff_precio", "diff_restringido"):
            df[col] = df[col].fillna(False).astype(bool)

        # Existencia real en el POS: la columna EAN_POS viene del staging.
        # "NOT_EXIST_POS" de la vista da True si falta la descripcion, no si
        # falta el articulo; se recalcula para no marcar falsos SOLO_HANA.
        df["not_exist_pos"] = df["ean_pos"].isna()

        def _tipo(row) -> str:
            if pd.isna(row["ean_pos"]):
                return "SOLO_HANA"
            if row["diff_precio"]:
                return "PRECIO"
            if row["diff_restringido"]:
                return "RESTRINGIDO"
            return "OK"
        df["tipo_diferencia"] = df.apply(_tipo, axis=1)
    else:
        df["tipo_diferencia"] = pd.Series(dtype=str)

    logger.info("Diferencias [%s] para tienda %s: %d filas", ambiente, tienda, len(df))
    return df


def get_logs_staging(ambiente: str, limite: int = 200) -> pd.DataFrame:
    """
    Últimos registros de POS_STAGING_LOG ordenados por ID descendente
    (los más recientes primero).

    Columnas: id, tienda, inicio, fin, registros, estado, mensaje.
    """
    ambiente = validar_ambiente(ambiente)
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
    logger.info("Ejecutando query HANA [%s] (log staging), limite=%s...", ambiente, limite)
    return pd.read_sql(text(query), db.get_hana(ambiente))


def get_cp_logs(ambiente: str, limite: int = 200) -> pd.DataFrame:
    """
    Últimos registros de CP_LOGS ordenados por TS descendente
    (los más recientes primero).

    Columnas: job_id, ts, level, message.
    """
    ambiente = validar_ambiente(ambiente)
    query = f"""
    SELECT
        "JOB_ID"  AS job_id,
        "TS"      AS ts,
        "LEVEL"   AS level,
        "MESSAGE" AS message
    FROM {_TABLA_CP_LOGS}
    ORDER BY "TS" DESC
    LIMIT {int(limite)}
    """
    logger.info("Ejecutando query HANA [%s] (logs CP_CVE), limite=%s...", ambiente, limite)
    return pd.read_sql(text(query), db.get_hana(ambiente))

