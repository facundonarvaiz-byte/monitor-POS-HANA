"""
Push de cambios desde el monitor HANA hacia el POS.

El SP `PUSH_FROM_MONITOR` solo encola los materiales en
`Z_NCR_CO.ZSD_PUSH_LOCAL_ENTRY`; el POS los procesa de forma asíncrona y
deja el resultado en `Z_NCR_CO.ZSD_PUSH_HIST_V3`.
"""

from __future__ import annotations

import re
import time

from sqlalchemy import text

from config import db, logger, validar_ambiente

# Vista que consume el SP para seleccionar los materiales a pushear.
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
