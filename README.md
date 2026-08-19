# 🔍 Monitor POST vs HANA

Dashboard Streamlit para monitorear diferencias de materiales entre **SAP HANA**
(fuente de verdad) y el **sistema POS** (PostgreSQL por tienda).

## Stack

| Capa | Tecnología |
|---|---|
| Dashboard | [Streamlit](https://streamlit.io) |
| HANA | `hdbcli` + `SQLAlchemy` |
| PostgreSQL (por tienda) | `psycopg2` + `SQLAlchemy` |
| Visualización | Plotly |

## Arquitectura

```
PostgreSQL (tienda)  ──► populate_pos_staging() ──► HANA POS_STAGING
                                                           │
HANA (imagen post) ─────────────────────────────► POS_Comparacion (Calculation View)
                                                           │
                                                    Streamlit Dashboard
```

- Las conexiones PostgreSQL se configuran por tienda en `stores.json` (no en `.env`).
- La comparación la realiza la vista HANA `POS_Comparacion` directamente.
- Python carga datos a `POS_STAGING` porque HANA XS Classic no puede conectarse
  a PostgreSQL externo.

## Requisitos

- Python 3.10+
- Acceso a SAP HANA (host, puerto, usuario, contraseña, schema)
- PostgreSQL accesible por tienda (credenciales en `stores.json`)

## Instalación

```bash
# 1. Clonar el proyecto
cd monitor-post-hana

# 2. Crear entorno virtual
python -m venv venv
venv\Scripts\activate    # Windows
source venv/bin/activate  # Linux/Mac

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar variables de entorno
cp .env.example .env
# Editar .env con los datos de conexión a HANA

# 5. Configurar tiendas
# Editar stores.json con host/puerto/credenciales de cada PostgreSQL
```

## Configuración

### `.env` — Solo credenciales HANA

```ini
HANA_HOST=hl0-db.example.corp
HANA_PORT=30015
HANA_USER=usuario
HANA_PASSWORD=contraseña
HANA_SCHEMA=Z_NCR_CO
```

### `stores.json` — Conexiones PostgreSQL por tienda

```json
{
  "E802": {
    "host": "10.x.x.x",
    "port": 5432,
    "db": "webfront",
    "user": "usuario",
    "password": "contraseña"
  }
}
```

## Uso

```bash
streamlit run src/app.py
```

La app carga automáticamente el resumen al iniciar:

1. **Resumen** — Gráfico de barras + tabla por tienda con total de diferencias
2. **Ver detalle →** — Grilla completa de diferencias para una tienda
3. **🔄 Actualizar datos** — Recarga el resumen o el detalle según la vista activa
4. **⬆️ Cargar Postgres** — Lee el PostgreSQL de la(s) tienda(s) y actualiza `POS_STAGING`

## Estructura del proyecto

```
monitor-post-hana/
├── docs/
│   ├── mapping.md              # Mapeo de campos HANA ↔ POST
│   ├── spec_hana_views.md      # Especificación de vistas HANA
│   └── spec_xsjs.md            # Especificación del staging
├── pos_staging/
│   ├── POS_STAGING.hdbtable    # Definición tabla HANA
│   └── POS_STAGING_LOG.hdbtable
├── src/
│   ├── app.py                  # Dashboard Streamlit
│   ├── config.py               # Conexión HANA + gestor de tiendas
│   ├── hana_queries.py         # Consultas HANA (resumen, detalle)
│   └── post_queries.py         # Carga PostgreSQL → POS_STAGING
├── stores.json                 # Conexiones PostgreSQL por tienda
├── .env                        # Credenciales HANA (no commitear)
├── .env.example                # Template de configuración
└── requirements.txt            # Dependencias Python
```

## Agregar una tienda nueva

Agregar una entrada en `stores.json`:

```json
{
  "E805": {
    "host": "10.x.x.x",
    "port": 5432,
    "db": "webfront",
    "user": "usuario",
    "password": "contraseña"
  }
}
```

La tienda aparece automáticamente en el resumen y el botón "Cargar Postgres".


### Modo de push

Por defecto se usa UPDATE directo en PostgreSQL.
Para migrar a API REST, editar `pusher.py` y crear la estrategia `PushAPI`.
