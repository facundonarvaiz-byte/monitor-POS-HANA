# Spec Calculation Views HANA — Monitor POST vs HANA
**Para:** Alejandra Dantur / Florencia Onostre  
**Objetivo:** Crear dos Calculation Views en HANA que comparen `POS_STAGING` (cacheado del PostgreSQL vía XSJS) con la vista `Articulos_IMAGEN_POS`. Python solo lee estas vistas.

**Prerequisito:** XSJS de Facundo poblando `"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING"`.  
Ver `docs/spec_xsjs.md` y `pos_staging/POS_STAGING.hdbtable` para estructura completa.

### Columnas de `POS_STAGING` disponibles para el join
| Columna HANA | Tipo | Origen PostgreSQL |
|---|---|---|
| `TIENDA` | NVARCHAR(10) | constante por tienda |
| `EAN` | NVARCHAR(50) | `article.article_number` |
| `SKU` | NVARCHAR(50) | `article.mainplunumber` ← **clave join con HANA** |
| `DESCRIPCION` | NVARCHAR(200) | `article.designation` |
| `PRECIO_POS` | DECIMAL(18,2) | `article.price` |
| `RESTRINGIDO_VENTA` | NVARCHAR(1) | `ext_structure_code[17]` |
| `FECHA_CARGA` | TIMESTAMP | timestamp de carga XSJS |

---

## Vista 1: Resumen por tienda — `NCR_Monitor/DIF_RESUMEN_TIENDAS`

**Input parameter:** `$$FECHA_REF$$` (DATE, opcional — default: hoy)  
**Output:** Una fila por tienda con conteo de diferencias.

| Columna | Tipo | Descripción |
|---------|------|-------------|
| `TIENDA` | NVARCHAR(10) | Código de tienda |
| `CANT_DIFFS_PRECIO` | INTEGER | Materiales con precio diferente entre HANA y POST |
| `CANT_SOLO_HANA` | INTEGER | Materiales en HANA sin match en POST (altas no consumidas) |
| `CANT_SOLO_POST` | INTEGER | Materiales en POST sin match en HANA |
| `CANT_DIFFS_RESTRINGIDO` | INTEGER | Materiales con restringido_venta diferente |
| `TOTAL_DIFFS` | INTEGER | Total de diferencias (suma de las anteriores) |
| `ULTIMA_CARGA_POST` | TIMESTAMP | Timestamp de la última carga del XSJS para esa tienda |
| `ESTADO` | NVARCHAR(10) | 'OK' (<50 diffs) / 'ALERTA' (50-200) / 'CRITICO' (>200) |

### Lógica sugerida

```sql
-- Join entre Articulos_IMAGEN_POS (HANA) y POS_STAGING (cacheado del PostgreSQL)
-- Columnas de POS_STAGING: TIENDA, EAN, SKU, DESCRIPCION, PRECIO_POS,
--                           RESTRINGIDO_VENTA, FECHA_CARGA
WITH comparacion AS (
    SELECT
        h."Tienda"              AS tienda,
        h."Sku"                 AS sku,
        h."EAN"                 AS ean_hana,
        p."EAN"                 AS ean_post,
        h."Precio_POS"          AS precio_hana,
        p."PRECIO_POS"           AS precio_pos,
        h."Restringido_Venta"   AS rest_hana,   -- PENDIENTE: Alejandra agrega col a vista
        p."RESTRINGIDO_VENTA"   AS rest_post,
        p."FECHA_CARGA"         AS ultima_carga_post
    FROM "_SYS_BIC"."Z_NCR_CO.NCR_Monitor/Articulos_IMAGEN_POS"  -- vista existente
         ('PLACEHOLDER' = ('$$WERKS_RUN$$', '%%')) h             -- sin filtro de tienda
    FULL OUTER JOIN "Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING" p
        ON h."Sku" = p."SKU" AND h."Tienda" = p."TIENDA"
)
SELECT
    COALESCE(tienda, 'SIN_TIENDA')                                              AS TIENDA,
    SUM(CASE WHEN ABS(COALESCE(precio_hana,0) - COALESCE(precio_pos,0)) > 0.01
             THEN 1 ELSE 0 END)                                                 AS CANT_DIFFS_PRECIO,
    SUM(CASE WHEN ean_post IS NULL THEN 1 ELSE 0 END)                           AS CANT_SOLO_HANA,
    SUM(CASE WHEN ean_hana IS NULL THEN 1 ELSE 0 END)                           AS CANT_SOLO_POST,
    SUM(CASE WHEN rest_hana IS NOT NULL AND rest_hana <> rest_post THEN 1 ELSE 0 END) AS CANT_DIFFS_RESTRINGIDO,
    MAX(ultima_carga_post)                                                       AS ULTIMA_CARGA_POST
    -- TOTAL_DIFFS y ESTADO: columnas calculadas en la Calculation View
FROM comparacion
GROUP BY tienda
```

---

## Vista 2: Detalle por tienda — `NCR_Monitor/DIF_DETALLE_TIENDA`

**Input parameters:**
- `$$WERKS_RUN$$` — código de tienda (ej: `'E802'`)
- `$$FECHA_REF$$` — fecha de referencia (DATE, opcional)

**Output:** Una fila por material con diferencia, para la tienda seleccionada.

| Columna | Tipo | Descripción |
|---------|------|-------------|
| `TIENDA` | NVARCHAR(10) | Código de tienda |
| `SKU` | NVARCHAR(50) | Código SAP del material |
| `EAN` | NVARCHAR(50) | Código de barras |
| `DESCRIPCION_HANA` | NVARCHAR(200) | `h."Descripcion"` de `Articulos_IMAGEN_POS` |
| `DESCRIPCION_POS` | NVARCHAR(200) | `p."DESCRIPCION"` de `POS_STAGING` |
| `PRECIO_HANA` | DECIMAL(18,2) | `h."Precio_POS"` de `Articulos_IMAGEN_POS` |
| `PRECIO_POS` | DECIMAL(18,2) | `p."PRECIO_POS"` de `POS_STAGING` |
| `RESTRINGIDO_HANA` | NVARCHAR(1) | `h."Restringido_Venta"` (PENDIENTE en vista HANA) |
| `RESTRINGIDO_POS` | NVARCHAR(1) | `p."RESTRINGIDO_VENTA"` de `POS_STAGING` |
| `TIPO_DIFERENCIA` | NVARCHAR(20) | 'PRECIO' / 'SOLO_HANA' / 'SOLO_POST' / 'RESTRINGIDO' |
| `FECHA_ULT_MOV` | TIMESTAMP | Fecha último movimiento en HANA (`Fecha_Ult_Act`) |
| `JOBIDN` | NVARCHAR(50) | Job ID del último envío (`JOBIDN`) |
| `ORIGEN_PRECIO` | NVARCHAR(50) | Origen del precio (`Origen_PRECIO_POS`) |

### Solo filas con diferencia
La vista debe filtrar: solo mostrar materiales donde al menos uno de estos sea verdadero:
- `ABS(precio_hana - precio_pos) > 0.01`
- `ean_pos IS NULL` (solo en HANA)
- `ean_hana IS NULL` (solo en POS)
- `restringido_hana <> restringido_pos`

---

## Campos de trazabilidad (de `Articulos_IMAGEN_POS`)

Estos campos ya existen en la vista HANA actual y son muy útiles para diagnóstico:
- `Fecha_Ult_Act` → cuándo se hizo el último envío
- `JOBIDN` → qué job lo generó
- `Origen_PRECIO_POS` → de dónde vino el precio

---

## Campo pendiente en vista HANA

Alejandra está agregando `Restringido_Venta` como columna calculada a la vista base.
Lógica: `LEFT("ADE_DATA_02", 1)` — toma el primer carácter de AddedData02.
- `'0'` = no restringido (puede vender)
- `'1'` = restringido (no puede vender)

Una vez disponible, conectar en las Calculation Views anteriores.

---

## Nombres de vista a usar en Python

Una vez creadas las vistas, actualizar en `src/hana_queries.py`:

```python
VISTA_RESUMEN_TIENDAS = '"_SYS_BIC"."Z_NCR_CO.NCR_Monitor/DIF_RESUMEN_TIENDAS"'
VISTA_DETALLE_TIENDA  = '"_SYS_BIC"."Z_NCR_CO.NCR_Monitor/DIF_DETALLE_TIENDA"'
```
