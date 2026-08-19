"""
Módulo de corrección — Push de datos corregidos desde HANA hacia POST.

Soporta dos modos:
1. UPDATE directo en PostgreSQL (modo por defecto)
2. API REST (preparado para migrar cuando esté disponible)

El push se hace por lote: el usuario selecciona las diferencias
a corregir desde el dashboard y se aplican en una transacción.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import pandas as pd
from sqlalchemy import text

from config import db, logger


# ============================================================
# Interfaz de estrategia de push
# ============================================================

class PushStrategy(Protocol):
    """Protocolo para estrategias de push (directo SQL o API)."""
    
    def push_correccion(
        self,
        sku: str,
        tienda: str,
        campo: str,
        valor_hana: Any,
    ) -> bool:
        ...


# ============================================================
# Estrategia: UPDATE directo en PostgreSQL
# ============================================================

@dataclass
class PushDirectoPostgres:
    """
    Corrige datos haciendo UPDATE directo en la tabla de PostgreSQL.
    
    Mapea el nombre de campo de fantasía a la columna real en la tabla.
    """
    
    # Mapeo: nombre de campo → columna en PostgreSQL
    campo_a_columna: dict[str, str] = field(default_factory=lambda: {
        "SKU": "sku",
        "EAN": "ean",
        "Precio": "precio",
        "Restringido Venta": "restringido_venta",
        "Medible / Pesable": "medible",
    })
    
    tabla: str = "public.tu_tabla_hash_plu"  # EDITAR
    
    def push_correccion(
        self,
        sku: str,
        tienda: str,
        campo: str,
        valor_hana: Any,
    ) -> bool:
        """
        Aplica una corrección individual.
        
        Returns
        -------
        bool
            True si se aplicó correctamente.
        """
        columna = self.campo_a_columna.get(campo)
        if columna is None:
            logger.warning("Campo '%s' no tiene mapeo a columna", campo)
            return False
        
        query = f"""
        UPDATE {self.tabla}
        SET {columna} = :valor,
            fecha_actualizacion = CURRENT_TIMESTAMP
        WHERE sku = :sku AND tienda = :tienda
        """
        
        try:
            with db.postgres.connect() as conn:
                result = conn.execute(
                    text(query),
                    {
                        "valor": valor_hana,
                        "sku": sku,
                        "tienda": tienda,
                    },
                )
                conn.commit()
                
                if result.rowcount == 0:
                    logger.warning(
                        "No se encontró registro: sku=%s, tienda=%s",
                        sku, tienda,
                    )
                    return False
                
                logger.info(
                    "Corregido: sku=%s, tienda=%s, campo=%s → %s",
                    sku, tienda, campo, valor_hana,
                )
                return True
                
        except Exception as e:
            logger.error(
                "Error corrigiendo sku=%s, tienda=%s, campo=%s: %s",
                sku, tienda, campo, e,
            )
            return False
    
    def push_lote(self, df_diff: pd.DataFrame) -> dict[str, Any]:
        """
        Aplica correcciones por lote a partir de un DataFrame de diferencias.
        
        Parameters
        ----------
        df_diff : pd.DataFrame
            DataFrame con columnas: sku, tienda, campo, valor_hana.
        
        Returns
        -------
        dict
            Resumen con resultados del lote.
        """
        exitos = 0
        errores = 0
        detalles: list[dict] = []
        
        for _, row in df_diff.iterrows():
            ok = self.push_correccion(
                sku=row["sku"],
                tienda=row["tienda"],
                campo=row["campo"],
                valor_hana=row["valor_hana"],
            )
            if ok:
                exitos += 1
                detalles.append({
                    "sku": row["sku"],
                    "tienda": row["tienda"],
                    "campo": row["campo"],
                    "resultado": "OK",
                })
            else:
                errores += 1
                detalles.append({
                    "sku": row["sku"],
                    "tienda": row["tienda"],
                    "campo": row["campo"],
                    "resultado": "ERROR",
                })
        
        resumen = {
            "total": exitos + errores,
            "exitos": exitos,
            "errores": errores,
            "detalles": detalles,
        }
        
        logger.info(
            "Push por lote completado: %d éxitos, %d errores de %d totales",
            exitos, errores, resumen["total"],
        )
        
        return resumen


# ============================================================
# Estrategia: API REST (placeholder para implementación futura)
# ============================================================

@dataclass
class PushAPI:
    """
    Corrige datos llamando a una API REST del POST.
    PENDIENTE: implementar cuando se conozca el endpoint.
    """
    
    endpoint: str = "https://api-post/actualizar"
    
    def push_correccion(
        self,
        sku: str,
        tienda: str,
        campo: str,
        valor_hana: Any,
    ) -> bool:
        logger.warning(
            "Push vía API no implementado. "
            "Pendiente endpoint para sku=%s, tienda=%s",
            sku, tienda,
        )
        return False
    
    def push_lote(self, df_diff: pd.DataFrame) -> dict[str, Any]:
        logger.warning("Push por lote vía API no implementado")
        return {"total": 0, "exitos": 0, "errores": 0, "detalles": []}


# ============================================================
# Fábrica
# ============================================================

def crear_pusher(modo: str = "directo") -> PushStrategy:
    """
    Crea la estrategia de push adecuada.
    
    Parameters
    ----------
    modo : str
        "directo" → UPDATE en PostgreSQL
        "api" → API REST (placeholder)
    """
    if modo == "api":
        return PushAPI()
    return PushDirectoPostgres()
