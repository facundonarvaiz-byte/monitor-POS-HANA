"""
Consultas a SAP HANA — Monitor POS vs HANA (materiales activos).
"""
from __future__ import annotations

import re
import time

import pandas as pd
from sqlalchemy import text

from config import db, logger, get_store_manager, validar_ambiente

# Vista de comparacion HANA vs POS_STAGING (parametro $$WERKS_RUN$$)
VISTA_COMPARACION = '"_SYS_BIC"."Z_NCRCO.Pos_staging/POS_Comparacion"'

# Vista de comparacion POS vs HANA a nivel EAN (vista "_Ale"), por tienda.
# Parametros: $$WERKS_RUN$$ (tienda) y $$JOIN_TYPE$$ (ver JOIN_TYPES_EAN).
VISTA_EAN = '"_SYS_BIC"."Z_NCRCO.Pos_staging.POS_Vs_Hana/POS_vs_HANAxEAN_Ale"'

# Modalidades del placeholder $$JOIN_TYPE$$ de VISTA_EAN.
# La vista filtra su salida por SHOW_EAN segun esta modalidad.
JOIN_TYPES_EAN = {
    "LO": "Solo POS",
    "RO": "Solo HANA",
    "FO": "Solo POS + Solo HANA",
    "MA": "Coincidencias",
    "AA": "Todos",
}

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


# ============================================================
# POS vs HANA por EAN (vista "_Ale")
# ============================================================

def _leer_vista_ean(
    sql: str,
    ambiente: str,
    intentos: int = 3,
    espera: float = 8.0,
) -> pd.DataFrame:
    """
    Ejecuta una consulta contra VISTA_EAN con reintentos.

    La vista "_Ale" se regenera en HANA por ventanas y durante ese proceso
    responde 'invalid table name ... in schema _SYS_BIC' aunque la vista
    exista. Se reintenta unas pocas veces antes de propagar el error.
    """
    ambiente = validar_ambiente(ambiente)
    for intento in range(1, intentos + 1):
        try:
            return pd.read_sql(text(sql), db.get_hana(ambiente))
        except Exception as e:
            if intento >= intentos:
                raise
            logger.warning(
                "Vista EAN [%s]: intento %d/%d falló (%s); reintento en %.0fs...",
                ambiente,
                intento,
                intentos,
                e,
                espera,
            )
            time.sleep(espera)


def _sql_resumen_ean_tienda(tienda: str) -> str:
    """
    SELECT agregado de VISTA_EAN para una tienda.

    Una fila con el conteo de EAN presentes en el POS y ausentes en HANA
    (JOIN_TYPE=LO, solo POS).
    """
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")

    return (
        f"SELECT '{tienda}' AS \"tienda\", COUNT(*) AS \"solo_pos\" "
        f"FROM {VISTA_EAN}("
        f"'PLACEHOLDER' = ('$$WERKS_RUN$$', '{tienda}'), "
        f"'PLACEHOLDER' = ('$$JOIN_TYPE$$', 'LO'))"
    )


def _completar_resumen_ean(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza tipos y ordena por solo_pos descendente."""
    if df.empty:
        return df

    df["solo_pos"] = df["solo_pos"].astype(int)

    if "estado" not in df.columns:
        df["estado"] = "OK"

    return df.sort_values("solo_pos", ascending=False).reset_index(drop=True)


def _resumen_ean_secuencial(tiendas: list[str], ambiente: str) -> pd.DataFrame:
    """Resumen por EAN con una query por tienda, aislando errores por tienda."""
    filas = []
    errores = set()
    for t in tiendas:
        try:
            df = _leer_vista_ean(_sql_resumen_ean_tienda(t), ambiente)
            r = df.iloc[0]
            filas.append({
                "tienda":   t,
                "solo_pos": int(r["solo_pos"]),
            })
        except Exception as e:
            logger.error("get_resumen_ean: error en tienda %s: %s", t, e)
            errores.add(t)
            filas.append({
                "tienda":   t,
                "solo_pos": 0,
            })

    df = _completar_resumen_ean(pd.DataFrame(filas))
    df.loc[df["tienda"].isin(errores), "estado"] = "ERROR"
    return df


def get_resumen_ean(ambiente: str) -> pd.DataFrame:
    """
    Resumen por tienda de VISTA_EAN en el ambiente indicado.

    Columnas: tienda, solo_pos, estado.
    Intenta un solo UNION ALL (una fila por tienda); si falla, reintenta
    tienda por tienda para reportar errores individuales.
    """
    ambiente = validar_ambiente(ambiente)
    tiendas = get_store_manager(ambiente).list_stores()
    if not tiendas:
        logger.warning("get_resumen_ean [%s]: no hay tiendas configuradas.", ambiente)
        return pd.DataFrame()

    try:
        query = " UNION ALL ".join(_sql_resumen_ean_tienda(t) for t in tiendas)
        logger.info(
            "Ejecutando query HANA [%s] (resumen por EAN), tiendas=%d...",
            ambiente,
            len(tiendas),
        )
        return _completar_resumen_ean(_leer_vista_ean(query, ambiente))
    except Exception as e:
        logger.error(
            "get_resumen_ean [%s]: falló el resumen conjunto (%s); reintento por tienda.",
            ambiente,
            e,
        )
        return _resumen_ean_secuencial(tiendas, ambiente)


def get_detalle_ean(tienda: str, join_type: str, ambiente: str) -> pd.DataFrame:
    """
    Detalle por EAN de una tienda y modalidad de VISTA_EAN.

    join_type: LO (solo POS) | RO (solo HANA) | FO (ambos singles) |
    MA (coincidencias) | AA (todos).

    Columnas: hana_tienda, pos_ean, pos_sku, pos_descripcion, hana_ean,
    hana_sku, hana_ausente, pos_ausente, match, hana_precio, pos_precio,
    pos_tienda, hana_activo, hana_umv, hana_ppal, pos_activo,
    fecha_carga_pos, hana_descripcion, show_ean, tipo (derivado:
    SOLO_POS | SOLO_HANA | MATCH | SKU_DISTINTO) y diff_precio (derivado).

    Nota: POS_ACTIVO (= RESTRINGIDO_VENTA del POS) viene NULL en el 100%
    de las filas porque la vista excluye los articulos con EAN = SKU.
    """
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")
    if join_type not in JOIN_TYPES_EAN:
        raise ValueError(
            f"JOIN_TYPE inválido: {join_type!r}. Válidos: {list(JOIN_TYPES_EAN)}"
        )
    ambiente = validar_ambiente(ambiente)

    query = f"""
    SELECT
        "HANA_Tienda"      AS "hana_tienda",
        "POS_EAN"          AS "pos_ean",
        "POS_SKU"          AS "pos_sku",
        "POS_DESCRIPCION"  AS "pos_descripcion",
        "HANA_EAN"         AS "hana_ean",
        "HANA_Sku"         AS "hana_sku",
        "HANA_AUSENTE"     AS "hana_ausente",
        "POS_AUSENTE"      AS "pos_ausente",
        "MATCH"            AS "match",
        "HANA_Precio"      AS "hana_precio",
        "POS_PRECIO"       AS "pos_precio",
        "POS_TIENDA"       AS "pos_tienda",
        "HANA_Activo"      AS "hana_activo",
        "HANA_UMV"         AS "hana_umv",
        "HANA_PPAL"        AS "hana_ppal",
        "POS_ACTIVO"       AS "pos_activo",
        "FECHA_CARGA_POS"  AS "fecha_carga_pos",
        "HANA_Descripcion" AS "hana_descripcion",
        "SHOW_EAN"         AS "show_ean"
    FROM {VISTA_EAN}('PLACEHOLDER' = ('$$WERKS_RUN$$', '{tienda}'),
                     'PLACEHOLDER' = ('$$JOIN_TYPE$$', '{join_type}'))
    ORDER BY "HANA_Sku", "POS_SKU"
    """
    logger.info(
        "Ejecutando query HANA [%s] (detalle EAN) tienda=%s join_type=%s...",
        ambiente,
        tienda,
        join_type,
    )
    df = _leer_vista_ean(query, ambiente)

    if not df.empty:
        for col in ("hana_ausente", "pos_ausente", "match"):
            df[col] = df[col].fillna(False).astype(bool)

        def _tipo(row) -> str:
            if row["hana_ausente"]:
                return "SOLO_POS"
            if row["pos_ausente"]:
                return "SOLO_HANA"
            if row["match"]:
                return "MATCH"
            return "SKU_DISTINTO"

        df["tipo"] = df.apply(_tipo, axis=1)

        precios = df[["hana_precio", "pos_precio"]].apply(pd.to_numeric, errors="coerce")
        df["diff_precio"] = (
            precios["hana_precio"].notna()
            & precios["pos_precio"].notna()
            & (precios["hana_precio"] != precios["pos_precio"])
        )

        # Fecha homogénea para grilla/CSV (algunas filas del POS vienen nulas)
        df["fecha_carga_pos"] = (
            df["fecha_carga_pos"]
            .astype(str)
            .replace({"NaT": "", "None": "", "nan": ""})
        )
    else:
        df["tipo"] = pd.Series(dtype=str)
        df["diff_precio"] = pd.Series(dtype=bool)

    logger.info(
        "Detalle EAN [%s] tienda=%s join_type=%s: %d filas",
        ambiente,
        tienda,
        join_type,
        len(df),
    )
    return df


# ============================================================
# Push de cambios al POS (SP PUSH_FROM_MONITOR)
# ============================================================

_VISTA_PUSH_ACTIVOS = (
    '"_SYS_BIC"."Z_NCRCO.Pos_staging.REPROCESS_Views/'
    'API_ArticulosJson_POS_ACTIVOS_PUSH_MASIVO"'
)
_SP_PUSH_FROM_MONITOR = (
    '"Z_NCR_CO"."Z_NCRCO.Pos_staging::PUSH_FROM_MONITOR"'
)
_TABLA_PUSH_ENTRY = '"Z_NCR_CO"."ZSD_PUSH_LOCAL_ENTRY"'
_TABLA_PUSH_HIST = '"Z_NCR_CO"."ZSD_PUSH_HIST_V3"'


def contar_materiales_push(tienda: str, ambiente: str) -> int:
    """
    Cuenta los materiales que el SP PUSH_FROM_MONITOR encolaría para la tienda.

    Usa la misma vista que consume el SP. Si devuelve 0 no tiene sentido
    ejecutar el push.
    """
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")
    ambiente = validar_ambiente(ambiente)

    query = (
        f"SELECT COUNT(*) FROM {_VISTA_PUSH_ACTIVOS}("
        f"PLACEHOLDER.\"$$SOLO_ACTIVOS$$\" => 'Y', "
        f"PLACEHOLDER.\"$$WERKS_RUN$$\" => '{tienda}')"
    )
    with db.get_hana(ambiente).connect() as conn:
        return int(conn.execute(text(query)).scalar() or 0)


def push_from_monitor(tienda: str, ambiente: str) -> dict:
    """
    Ejecuta el SP que encola los cambios de la tienda hacia el POS.

    El SP solo encola en ZSD_PUSH_LOCAL_ENTRY; el POS lo procesa después de
    forma asíncrona y deja el resultado en ZSD_PUSH_HIST_V3.

    Returns
    -------
    dict
        {"ok", "tienda", "ambiente", "run_key", "run_floor", "encolados",
         "sp_ms", "ts_antes", "error"?}
    """
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")
    ambiente = validar_ambiente(ambiente)

    start = time.perf_counter()
    try:
        engine = db.get_hana(ambiente)
        with engine.connect() as conn:
            ts_antes = conn.execute(
                text("SELECT CURRENT_TIMESTAMP FROM DUMMY")
            ).scalar()
            conn.exec_driver_sql(
                f"CALL {_SP_PUSH_FROM_MONITOR}('{tienda}')"
            )
        if ts_antes is None:
            raise RuntimeError("HANA no devolvió CURRENT_TIMESTAMP")

        run_floor = ts_antes.strftime("%Y%m%d%H%M%S")
        run_key = None
        encolados = 0
        with engine.connect() as conn:
            filas = conn.execute(
                text(
                    f'SELECT "DATUM", "UZEIT", COUNT(*) AS "N" '
                    f'FROM {_TABLA_PUSH_ENTRY} '
                    f"WHERE \"WERKS\" = :t AND \"BNAME\" = 'MONITOR' "
                    f'AND ("DATUM" || "UZEIT") >= :floor '
                    f'GROUP BY "DATUM", "UZEIT" '
                    f'ORDER BY "DATUM" DESC, "UZEIT" DESC'
                ),
                {"t": tienda, "floor": run_floor},
            ).fetchall()
            if filas:
                run_key = f"{filas[0][0]}{filas[0][1]}"
                encolados = int(filas[0][2] or 0)
            else:
                filas = conn.execute(
                    text(
                        f'SELECT "DATUM", "UZEIT", COUNT(*) AS "N" '
                        f'FROM {_TABLA_PUSH_HIST} '
                        f"WHERE \"WERKS\" = :t AND \"BNAME\" = 'HANAMONITOR' "
                        f'AND ("DATUM" || "UZEIT") >= :floor '
                        f'GROUP BY "DATUM", "UZEIT" '
                        f'ORDER BY "DATUM" DESC, "UZEIT" DESC'
                    ),
                    {"t": tienda, "floor": run_floor},
                ).fetchall()
                if filas:
                    run_key = f"{filas[0][0]}{filas[0][1]}"
                    encolados = int(filas[0][2] or 0)

        sp_ms = int((time.perf_counter() - start) * 1000)
        logger.info(
            "Push_from_monitor [%s]: tienda=%s run=%s encolados=%d sp_ms=%d",
            ambiente,
            tienda,
            run_key,
            encolados,
            sp_ms,
        )
        return {
            "ok": True,
            "tienda": tienda,
            "ambiente": ambiente,
            "run_key": run_key,
            "run_floor": run_floor,
            "encolados": encolados,
            "sp_ms": sp_ms,
            "ts_antes": str(ts_antes),
        }
    except Exception as e:
        logger.error(
            "Push_from_monitor [%s]: error tienda=%s: %s",
            ambiente,
            tienda,
            e,
        )
        return {
            "ok": False,
            "tienda": tienda,
            "ambiente": ambiente,
            "error": str(e),
        }


def estado_push(
    tienda: str,
    run_key: str | None,
    run_floor: str | None,
    ambiente: str,
) -> dict:
    """
    Consulta el avance del push en las tablas de cola e histórico.

    Returns
    -------
    dict
        {"pendientes", "procesados", "completado", "procesado_ts"}
    """
    if not re.match(r'^[A-Za-z0-9]+$', tienda):
        raise ValueError(f"Código de tienda inválido: {tienda!r}")
    ambiente = validar_ambiente(ambiente)

    if not run_key and not run_floor:
        return {
            "pendientes": 0,
            "procesados": 0,
            "completado": False,
            "procesado_ts": None,
        }

    if run_key:
        filtro = 'AND ("DATUM" || "UZEIT") = :run'
        params = {"t": tienda, "run": run_key}
    else:
        filtro = 'AND ("DATUM" || "UZEIT") >= :floor'
        params = {"t": tienda, "floor": run_floor}

    with db.get_hana(ambiente).connect() as conn:
        pendientes = int(
            conn.execute(
                text(
                    f'SELECT COUNT(*) FROM {_TABLA_PUSH_ENTRY} '
                    f"WHERE \"WERKS\" = :t AND \"BNAME\" = 'MONITOR' "
                    f"{filtro}"
                ),
                params,
            ).scalar()
            or 0
        )
        fila = conn.execute(
            text(
                f'SELECT COUNT(*) AS "N", MAX("PROCESADO") AS "P" '
                f'FROM {_TABLA_PUSH_HIST} '
                f"WHERE \"WERKS\" = :t AND \"BNAME\" = 'HANAMONITOR' "
                f"{filtro}"
            ),
            params,
        ).fetchone()
        procesados = int(fila[0] or 0) if fila else 0
        procesado_ts = fila[1] if fila else None

    return {
        "pendientes": pendientes,
        "procesados": procesados,
        "completado": pendientes == 0 and procesados > 0,
        "procesado_ts": str(procesado_ts) if procesado_ts else None,
    }

