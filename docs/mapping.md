# Monitor POST vs HANA — Documentación Técnica

> **Versión:** 1.0  
> **Fecha:** 2026-07-17  
> **Propósito:** Documentar el mapeo de datos entre SAP HANA y PostgreSQL para el monitor de comparación de materiales.  
> **Contexto:** Proyecto Cencosud Colombia — integración SAP → POST (punto de venta).

---

## Índice

1. [Arquitectura general](#1-arquitectura-general)
2. [Conexiones a bases de datos](#2-conexiones-a-bases-de-datos)
3. [Mapeo de campos](#3-mapeo-de-campos)
4. [Lógica de negocio](#4-lógica-de-negocio)
5. [Flags especiales](#5-flags-especiales)
6. [Arquitectura del dashboard](#6-arquitectura-del-dashboard)
7. [Guía de uso](#7-guía-de-uso)
8. [Corrección de datos (Push)](#8-corrección-de-datos-push)
9. [Mantenimiento](#9-mantenimiento)
10. [Referencias](#10-referencias)

---

## 1. Arquitectura general

```
┌─────────────────────────────────────────────────────────────┐
│                         SAP HANA                            │
│  ┌──────────────────────────────────────────────────────┐   │
│  │           Base Imagen Post (Vista/Dashboard)          │   │
│  │  Contiene el estado actual de cada material según    │   │
│  │  SAP, incluyendo la última foto post-envío           │   │
│  │  + movimientos/pushes del día                        │   │
│  └──────────┬───────────────────────────────────────────┘   │
└─────────────┼───────────────────────────────────────────────┘
              │
              │  Extracción vía SQL (hdbcli)
              │  Tabla: PUBLIC.tu_tabla_hash_plu (EDITAR)
              ▼
┌─────────────────────────────────────────────────────────────┐
│                MONITOR (Streamlit Dashboard)                │
│                                                             │
│  1. Carga datos de HANA (imagen post)                       │
│  2. Carga datos de POST (hash PLU)                          │
│  3. Compara campo por campo                                 │
│  4. Muestra diferencias en dashboard                        │
│  5. Permite push de correcciones a POST                     │
└─────────────┬───────────────────────────────────────────────┘
              │
              │  Extracción vía SQL (psycopg2)
              │  Luego: UPDATE para correcciones
              ▼
┌─────────────────────────────────────────────────────────────┐
│                   PostgreSQL (POST)                         │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              Hash PLU                                 │   │
│  │  Datos que viajaron efectivamente al punto de venta  │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### Flujo de datos

1. **SAP → HANA:** Los materiales se crean/modifican en SAP y se replican en HANA
2. **HANA → POST:** Periódicamente se envía una "foto" de los materiales al POST (punto de venta) mediante un JSON
3. **POST → Tienda:** Los datos se usan en los POS de las tiendas
4. **Monitor:** Compara HANA (fuente de verdad) vs POST (lo que realmente viajó)

> **Problema detectado en la reunión:** Los datos en POST pueden quedar desactualizados si hay pushes durante el día que modifican materiales. La vista `Base Imagen Post` en HANA es la fuente de verdad, pero el POST puede tener una foto más vieja.

---

## 2. Conexiones a bases de datos

### 2.1 SAP HANA

| Propiedad | Valor |
|-----------|-------|
| Driver | `hdbcli` (via `sqlalchemy`) |
| URL de conexión | `hana+hdbcli://user:password@host:port/schema` |
| Host | Configurable en `.env` (`HANA_HOST`) |
| Puerto | Configurable en `.env` (`HANA_PORT`, default: `30015`) |
| Schema | Configurable en `.env` (`HANA_SCHEMA`) |

### 2.2 PostgreSQL (POST)

| Propiedad | Valor |
|-----------|-------|
| Driver | `psycopg2` (via `sqlalchemy`) |
| URL de conexión | `postgresql+psycopg2://user:password@host:port/db` |
| Host | Configurable en `.env` (`POSTGRES_HOST`) |
| Puerto | Configurable en `.env` (`POSTGRES_PORT`, default: `5432`) |
| Database | Configurable en `.env` (`POSTGRES_DB`) |

### 2.3 Configuración en código

Editar `src/config.py` o usar `.env`:

```ini
# HANA
HANA_HOST=host_hana
HANA_PORT=30015
HANA_USER=user_hana
HANA_PASSWORD=pass_hana
HANA_SCHEMA=tu_schema

# PostgreSQL
POSTGRES_HOST=host_postgres
POSTGRES_PORT=5432
POSTGRES_DB=post_db
POSTGRES_USER=user_postgres
POSTGRES_PASSWORD=pass_postgres
```

---

## 3. Mapeo de campos

> ⚠️ **ATENCIÓN:** Los nombres de vistas y columnas en HANA son **PENDIENTES DE CONFIRMAR**.  
> Hay que explorar las bases de datos para completar esta tabla.
>
> Archivos a editar: `src/hana_queries.py` y `src/post_queries.py`

### 3.1 Campos core (comparación inicial)

| # | Campo (nom. fantasía) | Vista HANA | Col. HANA | Tabla POST | Col. POST | Tipo | Transformación | Severidad |
|---|----------------------|-----------|-----------|-----------|-----------|------|---------------|-----------|
| 1 | **SKU** | `{VISTA_IMAGEN_POST}` | `MATNR` | `{TABLA_HASH_PLU}` | `sku` | `VARCHAR` | Directa | 🔴 ALTA |
| 2 | **EAN** | `{VISTA_IMAGEN_POST}` | `EAN11` | `{TABLA_HASH_PLU}` | `ean` | `VARCHAR` | Directa | 🔴 ALTA |
| 3 | **Precio** | `{VISTA_IMAGEN_POST}` | `PKVTR` | `{TABLA_HASH_PLU}` | `precio` | `DECIMAL` | Directa (tol: 0.01) | 🔴 ALTA |
| 4 | **Restringido Venta** | `{VISTA_IMAGEN_POST}` | `AddedData02` | `{TABLA_HASH_PLU}` | `restringido_venta` | `INT` | **Invertida** (ver §5) | 🔴 ALTA |
| 5 | **Medible / Pesable** | `{VISTA_IMAGEN_POST}` | `T006_ANDEC` | `{TABLA_HASH_PLU}` | `medible` | `INT` | **Invertida** (ver §5) | 🔴 ALTA |

### 3.2 Campos extendidos (futuro)

| # | Campo (nom. fantasía) | Vista HANA | Col. HANA | Tabla POST | Col. POST | Tipo | Transformación | Severidad |
|---|----------------------|-----------|-----------|-----------|-----------|------|---------------|-----------|
| 6 | Descripción corta | `{VISTA_IMAGEN_POST}` | `MAKTX` | `{TABLA_HASH_PLU}` | `descripcion` | `VARCHAR` | Directa | 🟡 MEDIA |
| 7 | Descripción larga | `{VISTA_IMAGEN_POST}` | `MATXT` | — | — | `VARCHAR` | Solo referencia | 🟢 BAJA |
| 8 | Impuestos bolsas | `{VISTA_IMAGEN_POST}` | `AddedData01` | `{TABLA_HASH_PLU}` | `impuestos_bolsas` | `INT` | Directa | 🟡 MEDIA |
| 9 | Sección (MVGR1) | `{VISTA_IMAGEN_POST}` | `MVGR1` | — | — | `VARCHAR` | Solo referencia | 🟢 BAJA |
| 10 | Sección (MVGR4) | `{VISTA_IMAGEN_POST}` | `MVGR4` | — | — | `VARCHAR` | Solo referencia | 🟢 BAJA |
| 11 | Fecha último envío | `{VISTA_MOVIMIENTOS}` | `FECHA_ENVIO` | `{TABLA_HASH_PLU}` | `fecha_ultimo_envio` | `DATE` | Directa | 🟢 BAJA |
| 12 | Costo | `{VISTA_IMAGEN_POST}` | `PSAB` | — | — | `DECIMAL` | Solo referencia | 🟡 MEDIA |

### 3.3 Clave de unión

La relación entre HANA y POST se establece mediante la combinación:

```
SKU (MATNR) + Tienda (WERKS)
```

> ⚠️ Verificar si en POST también existe el campo `tienda`. Si no, la comparación se hace solo por SKU.

---

## 4. Lógica de negocio

### 4.1 Materiales medibles vs pesables

Según la reunión, la condición de **material medible** se determina así:

| Condición | Es medible |
|-----------|-----------|
| EAN empieza con **24** | ✅ Sí |
| Otras condiciones (posición 21 y 22) | Ver lógica específica por país |

> **Historia:** Originalmente se usaba el flag `T006_ANDEC` (heredado de Argentina), pero luego resultó que no reflejaba correctamente la condición de medible. En Colombia, la regla pasó a ser "EAN empieza con 24". Sin embargo, el flag `T006_ANDEC` se mantiene en las vistas por compatibilidad.

### 4.2 Restringido venta

El campo `AddedData02` contiene el flag de restricción de venta.
Se setea en SR cuando:
- `MBKMVGR4 = 'FS'` → `AddedData02 = 1` (se puede vender)
- `MBKMVGR4 = '90'` (sección 90) → lógica específica

### 4.3 Impuestos de bolsas (AddedData01)

El campo `AddedData01` se usa para los impuestos de bolsas en países donde aplica (ej: Colombia, Argentina).

### 4.4 Diferencias por país

| País | Reglas específicas |
|------|-------------------|
| 🇦🇷 Argentina | `T006_ANDEC` usado como flag de medible (original) |
| 🇨🇴 Colombia | Medible = EAN empieza con 24. Lógica actualizada. |

> El monitor está enfocado en **Colombia** inicialmente.

---

## 5. Flags especiales

### 5.1 Flags invertidos

**⚠️ IMPORTANTE:** Algunos flags viajan con lógica **invertida** entre HANA y el JSON/POST.

#### Restringido venta (`AddedData02`)

| Valor en HANA | Significado |
|:---:|---|
| **1** | ✅ Se puede vender (no restringido) |
| **0** | ❌ No se puede vender (restringido) |
| *(vacío/nulo)* | ⚠️ Inconsistencia — debería investigarse |

> **Contraintuitivo:** `1` = positivo (se puede vender), no `1` = negativo.

#### Medible / Pesable (`T006_ANDEC`)

| Valor en HANA | Significado |
|:---:|---|
| **1** | ❌ No es medible |
| **0** | ✅ Sí es medible |
| *(vacío/nulo)* | ⚠️ Inconsistencia |

> **Contraintuitivo:** `1` = no medible. Al revés de lo esperado.

### 5.2 Cómo maneja el monitor los flags invertidos

En `comparator.py`, la función `_normalizar()` aplica la inversión automáticamente:

```python
def _normalizar(valor, invertido: bool = False):
    if invertido:
        return 1.0 - valor  # 1→0, 0→1
    return valor
```

Cada `CampoComparable` tiene el flag `invertido` configurado:

```python
CampoComparable(
    nombre="Restringido Venta",
    invertido=True,  # ← La comparación ya invierte automáticamente
)
```

Esto significa que cuando el dashboard muestra "Valor HANA" y "Valor POST", los valores **ya están normalizados** (invertidos si corresponde). El valor que se pushea a POST también se envía en el formato correcto.

---

## 6. Arquitectura del dashboard

### 6.1 Pestañas

| Pestaña | Función |
|---------|---------|
| **📊 Resumen** | KPIs (totales materiales, diferencias), gráficos de barras por campo y tienda |
| **📋 Detalle** | Tabla interactiva con todas las diferencias, filtrable por severidad/campo/tipo, exportable a CSV |
| **🔧 Corrección** | Selección de diferencias a corregir, push por lote a PostgreSQL |
| **ℹ️ Documentación** | Esta documentación resumida |

### 6.2 Flujo de comparación

```
get_imagen_post(HANA)    get_post_data(PostgreSQL)
        │                        │
        ▼                        ▼
    DataFrame HANA          DataFrame POST
        │                        │
        └────────┬───────────────┘
                 │
                 ▼
         comparar(df_hana, df_post)
                 │
                 ▼
         DataFrame Diferencias
                 │
        ┌────────┼────────┐
        ▼        ▼        ▼
     Resumen  Detalle  Corrección
```

### 6.3 Criterios de comparación

- **SKU, EAN:** Comparación exacta de strings
- **Precio:** Comparación numérica con tolerancia de 0.01 (1 centavo)
- **Flags invertidos:** Se normalizan antes de comparar (ver §5)
- **Materiales faltantes:** Se detectan tanto los que están solo en HANA como solo en POST

---

## 7. Guía de uso

### 7.1 Requisitos previos

- Python 3.10+ instalado
- Dependencias instaladas: `pip install -r requirements.txt`
- Archivo `.env` configurado con credenciales reales
- Nombres de vistas/tablas actualizados en `hana_queries.py` y `post_queries.py`

### 7.2 Ejecución

```bash
cd monitor-post-hana
streamlit run src/app.py
```

### 7.3 Pasos para comparar

1. **Probar conexiones** — Click en "Probar conexiones" en la barra lateral
   - Verificar que ambos indicadores (HANA y POST) estén en verde ✅
2. **Configurar filtros** (opcional):
   - **Tienda:** Código de tienda específica (ej: `1001`). Vacío = todas.
   - **Límite:** Para pruebas usar 100-1000 registros
3. **Cargar y comparar** — Click en el botón principal
4. **Explorar resultados:**
   - Pestaña **Resumen**: KPIs y gráficos
   - Pestaña **Detalle**: Tabla interactiva con filtros

### 7.4 Interpretación de resultados

- **Material solo en HANA** → No viajó al POST (nunca se envió o se eliminó del POST)
- **Material solo en POST** → Existe en el punto de venta pero no en SAP (posible creado localmente)
- **Valor distinto** → El dato en POST está desactualizado respecto a HANA
- **Nulo en HANA / Nulo en POST** → Dato faltante en una de las fuentes

### 7.5 Severidades

| Severidad | Significado | Acción recomendada |
|-----------|-------------|-------------------|
| 🔴 ALTA | Impacta directamente la venta (SKU, EAN, Precio, flags) | Corregir ASAP |
| 🟡 MEDIA | Datos administrativos (descripciones, impuestos) | Corregir en el día |
| 🟢 BAJA | Referencia (fechas, secciones) | Monitorear |

---

## 8. Corrección de datos (Push)

### 8.1 Modo actual: UPDATE directo en PostgreSQL

El push de correcciones se realiza mediante **UPDATE directo** en la tabla de PostgreSQL.

**Flujo:**
1. El usuario selecciona las diferencias a corregir (Pestaña Corrección)
2. Marca los campos y confirma
3. Click en "Aplicar correcciones"
4. Se ejecuta un UPDATE por cada combinación SKU+Tienda+campo
5. Se muestra el resultado (éxitos/errores)

**Precaución:** Siempre revisar los datos antes de aplicar. La interfaz pide confirmación explícita.

### 8.2 Modo futuro: API REST

Cuando esté disponible el endpoint de API, se puede activar el modo `PushAPI`:

```python
# En pusher.py
pusher = crear_pusher(modo="api")  # Cambiar de "directo" a "api"
```

### 8.3 Campos corregibles

| Campo | Columna en POST | Tipo |
|-------|----------------|------|
| SKU | `sku` | `VARCHAR` |
| EAN | `ean` | `VARCHAR` |
| Precio | `precio` | `DECIMAL` |
| Restringido Venta | `restringido_venta` | `INT` |
| Medible / Pesable | `medible` | `INT` |

> Los valores que se envían en el push ya están normalizados (flags invertidos correctamente).

---

## 9. Mantenimiento

### 9.1 Agregar un nuevo campo a comparar

Seguir estos pasos:

1. **`src/hana_queries.py`** — Agregar la columna en `get_imagen_post()`:
   ```python
   "MI_COLUMNA AS mi_campo",  # Descripción
   ```

2. **`src/post_queries.py`** — Agregar la columna en `get_post_data()`:
   ```python
   "mi_campo",
   ```

3. **`src/comparator.py`** — Agregar el `CampoComparable` en `CAMPOS_A_COMPARAR`:
   ```python
   CampoComparable(
       nombre="Mi Campo",
       col_hana="mi_campo",
       col_post="mi_campo",
       severidad=Severidad.MEDIA,
       tolerancia=None,
       invertido=False,
       descripcion="Descripción del campo",
   )
   ```

4. **`src/pusher.py`** — Agregar el mapeo en `campo_a_columna`:
   ```python
   "Mi Campo": "mi_campo",
   ```

5. **`docs/mapping.md`** — Actualizar la tabla de mapeo

### 9.2 Actualizar vistas en HANA

Si se agregan campos a las vistas en HANA:
1. Actualizar `get_imagen_post()` en `hana_queries.py`
2. Verificar que los nombres de columna coincidan
3. Probar con un límite bajo antes de ejecutar producción

### 9.3 Soporte para múltiples países

El monitor está configurado para Colombia. Para adaptarlo a otro país:
1. Revisar las reglas de flags específicas del país
2. Actualizar `CAMPOS_A_COMPARAR` si hay diferencias
3. Considerar agregar un selector de país en el dashboard

---

## 10. Referencias

### 10.1 Reunión

- **Fecha:** Grabación original (archivo: `Reunión en HANA - SAP - Python.vtt`)
- **Participantes:** Alejandra Dantur, Florencia Onostre, Facundo Narvaiz
- **Temas clave:**
  - Flags invertidos (restricted sale, weighable)
  - Necesidad de mapear JSON → GMREC → tablas HANA
  - Vistas del dashboard (`Base Imagen Post`, `Base Imagen Post no Trace`)
  - Monitor de comparación con 5 campos core
  - 1500+ diferencias por tienda detectadas

### 10.2 Archivos del proyecto

| Archivo | Propósito |
|---------|-----------|
| `src/app.py` | Dashboard Streamlit |
| `src/config.py` | Conexiones a bases de datos |
| `src/hana_queries.py` | Queries a HANA |
| `src/post_queries.py` | Queries a PostgreSQL |
| `src/comparator.py` | Lógica de comparación |
| `src/pusher.py` | Push de correcciones |
| `docs/mapping.md` | **Este documento** |

### 10.3 Tecnologías

- [Streamlit](https://streamlit.io) — Dashboard framework
- [SQLAlchemy](https://www.sqlalchemy.org) — ORM para bases de datos
- [hdbcli](https://pypi.org/project/hdbcli/) — Driver SAP HANA
- [psycopg2](https://pypi.org/project/psycopg2-binary/) — Driver PostgreSQL
- [Plotly](https://plotly.com/python/) — Gráficos interactivos
- [Pandas](https://pandas.pydata.org) — Manipulación de datos

---

> **Próximos pasos:**
> 1. ✅ Estructura del proyecto creada
> 2. ⬜ Conectar a HANA y explorar nombres reales de vistas
> 3. ⬜ Conectar a PostgreSQL y explorar tablas reales
> 4. ⬜ Actualizar placeholders en queries con nombres reales
> 5. ⬜ Probar comparación con datos reales
> 6. ⬜ Ajustar lógica según resultados de pruebas
