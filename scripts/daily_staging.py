"""
daily_staging.py - Carga diaria de POS_STAGING desde PostgreSQL a HANA.

Ejecutar manualmente:
    venv/Scripts/python scripts/daily_staging.py

Programar en Windows Task Scheduler:
    Accion : venv/Scripts/python.exe
    Argumento: scripts/daily_staging.py
    Directorio: D:/ruta/al/proyecto
"""

import sys
import os

# Asegurar que el src/ esté en el path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from post_queries import populate_pos_staging, listar_tiendas_postgres
from config import logger


def main():
    tiendas = listar_tiendas_postgres()
    if not tiendas:
        logger.warning("daily_staging: no hay tiendas configuradas en stores.json")
        return

    logger.info("daily_staging: iniciando para %d tiendas: %s", len(tiendas), tiendas)

    ok = 0
    errores = []

    for tienda in tiendas:
        result = populate_pos_staging(tienda)
        if result["ok"]:
            ok += 1
            logger.info(
                "  ✓ %s — %d registros en %dms",
                tienda, result["registros"], result["duracion_ms"],
            )
        else:
            errores.append(tienda)
            logger.error("  ✗ %s — %s", tienda, result.get("error", "error desconocido"))

    logger.info(
        "daily_staging: finalizado. OK=%d  ERROR=%d",
        ok, len(errores),
    )
    if errores:
        logger.error("Tiendas con error: %s", errores)
        sys.exit(1)


if __name__ == "__main__":
    main()
