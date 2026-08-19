# Spec XSJS — Cache PostgreSQL → HANA
**Para:** Facundo Narvaiz  
**Objetivo:** Leer datos del PostgreSQL local de cada tienda y escribirlos en una tabla staging de HANA, para que el monitor Python solo necesite una conexión (a HANA).

---

## Tabla staging destino en HANA

Crear en schema `Z_NCR_CO`:

```sql
CREATE TABLE "Z_NCR_CO"."POS_STAGING" (
    "TIENDA"              NVARCHAR(10)   NOT NULL,  -- Código tienda ej: 'E802'
    "EAN"                 NVARCHAR(50)   NOT NULL,  -- article.article_number
    "SKU"                 NVARCHAR(50)   NOT NULL,  -- article.mainplunumber (Cod_SAP)
    "DESCRIPCION"         NVARCHAR(200),            -- article.designation (con fallback)
    "PRECIO_POS"          DECIMAL(18,2),            -- article.price (con fallback)
    "RESTRINGIDO_VENTA"   NVARCHAR(1),              -- ext_structure_code[17]: '0'=no rest / '1'=rest
    "FECHA_CARGA"         TIMESTAMP      NOT NULL,  -- timestamp de cuando se corrió el XSJS
    PRIMARY KEY ("TIENDA", "EAN")
);
```

---

## Query PostgreSQL a ejecutar por tienda

```sql
SELECT
    '<CODIGO_TIENDA>'                              AS tienda,
    a.article_number                               AS ean,
    a.mainplunumber                                AS sku,
    CASE
        WHEN a.designation <> '' THEN a.designation
        ELSE (SELECT designation FROM article
              WHERE article_number = a.mainplunumber
                AND designation <> '' LIMIT 1)
    END                                            AS descripcion,
    CASE
        WHEN a.price IS NOT NULL AND a.price <> 0 THEN a.price
        ELSE (SELECT price FROM article
              WHERE article_number = a.mainplunumber
                AND price <> 0 LIMIT 1)
    END                                            AS precio_pos,
    SUBSTRING(ae.ext_structure_code, 17, 1)        AS restringido_venta
FROM article a
LEFT JOIN article_extended ae ON a.article_number = ae.article_number
INNER JOIN department d       ON a.department_number = d.department_number;
```

Reemplazar `<CODIGO_TIENDA>` con el código de la tienda procesada (ej: `'E802'`).

---

## Servidores PostgreSQL por tienda (Colombia — 14 tiendas MDH)

| Tienda | Host | Puerto | DB | Usuario | PASSWORD |
|--------|------|--------|----|---------|----------|
| E802   | 10.128.35.1 | 5432 | webfront | mtxadmin | password|
| ...    | ...  | ...    | ...| ...     | ... |

> Completar con los datos de las 14 tiendas cuando estén disponibles.

---

## Lógica del XSJS

### Batch diario (3:00 am)
1. Para cada tienda en la lista:
   a. Conectar al PostgreSQL local de esa tienda
   b. Ejecutar la query anterior
   c. Hacer UPSERT en `POS_STAGING` (DELETE + INSERT, o MERGE)
   d. Registrar `FECHA_CARGA = NOW()`
2. Log de ejecución en tabla `POS_STAGING_LOG` (tienda, inicio, fin, cant_registros, estado)

### On-demand (endpoint HTTP)
- Endpoint: `GET /xsjs/update_tienda.xsjs?tienda=E802`
- Ejecuta el mismo proceso para una sola tienda
- Responde JSON: `{"tienda": "E802", "registros": 12450, "duracion_ms": 3200, "ok": true}`

---

## Tabla de log sugerida

```sql
CREATE TABLE "Z_NCR_CO"."POS_STAGING_LOG" (
    "ID"           INTEGER GENERATED ALWAYS AS IDENTITY,
    "TIENDA"       NVARCHAR(10),
    "INICIO"       TIMESTAMP,
    "FIN"          TIMESTAMP,
    "REGISTROS"    INTEGER,
    "ESTADO"       NVARCHAR(10),  -- 'OK' / 'ERROR'
    "MENSAJE"      NVARCHAR(500),
    PRIMARY KEY ("ID")
);
```

---

## Notas

- El campo `RESTRINGIDO_VENTA` en POST: `'0'` = no restringido (puede vender), `'1'` = restringido (no puede vender).
- El campo `SKU` (`mainplunumber`) es el código SAP — es la clave de join con HANA.
- El campo `EAN` (`article_number`) es el código de barras.
- Si `price = 0` o NULL, hacer fallback al precio del artículo principal (`mainplunumber`).
