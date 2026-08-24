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

- Python 3.10+ (en el servidor de producción se usa Python 3.11)
- Acceso a SAP HANA (host, puerto, usuario, contraseña, schema)
- PostgreSQL accesible por tienda (credenciales en `stores.json`)

## Instalación (desarrollo)

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
# Editar .env con los datos de conexión a HANA y las claves del dashboard

# 5. Configurar tiendas
# Editar stores.json con host/puerto/credenciales de cada PostgreSQL
```

## Configuración

### `.env` — Credenciales HANA + claves del dashboard

```ini
HANA_HOST=hl0-db.example.corp
HANA_PORT=30015
HANA_USER=usuario
HANA_PASSWORD=contraseña
HANA_SCHEMA=Z_NCR_CO

# Autenticacion del dashboard (intranet)
GESTOR_PASS=gestor01
REVISOR_PASS=revisor01
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

Al abrir el dashboard hay que iniciar sesión con uno de los dos roles:

| Usuario | Permisos |
|---|---|
| `gestor` | Todo: ver, exportar, actualizar y **Cargar Postgres** (staging) |
| `revisor` | Solo lectura: ve, filtra, exporta CSV; sin botón de staging |

Dentro de la app:

1. **Resumen** — Gráfico de barras + tabla por tienda con total de diferencias
2. **Ver detalle →** — Grilla completa de diferencias para una tienda
3. **🔄 Actualizar datos** — Recarga el resumen o el detalle según la vista activa
4. **⬆️ Cargar Postgres** (solo gestor) — Lee el PostgreSQL de la(s) tienda(s) y actualiza `POS_STAGING`

## Despliegue (servidor intranet g100603aws079 / 172.23.18.102)

### Acceso

- Dashboard en producción: `http://g100603aws079/monitor`
- Servidor: Apache reverse proxy → Streamlit `127.0.0.1:8501` (websockets incluidos)
- Servicio systemd: `monitor-post-hana` (log de deploy: `/var/log/monitor-post-hana-deploy.log`)
- Carga diaria automática: cron a las **05:00** (`daily_staging.py`, log en `logs/daily_staging.log`)

### Cómo publicar un cambio

```powershell
scripts\deploy.ps1
```

Hace: commit pendiente (si hay) → `git push origin master` (GitHub) → `git push deploy master`
(server intranet). El hook `post-receive` del server hace checkout + `pip install` + restart del
servicio automáticamente. Alternativa manual: `git push origin master; git push deploy master`.

### Secrets (no viajan por git)

En el server, en `/opt/pyapps/monitor-post-hana/`, con permisos `root:root 600`:

- `.env` — HANA + `GESTOR_PASS`/`REVISOR_PASS` (las claves reales del dashboard)
- `stores.json` — conexiones PostgreSQL por tienda

Para cambiar las claves del dashboard: editar `GESTOR_PASS`/`REVISOR_PASS` en el `.env` del
server y `systemctl restart monitor-post-hana`.

### Agregar una tienda en producción

1. Editar `stores.json` **en el server** (root): `/opt/pyapps/monitor-post-hana/stores.json`
2. (Opcional) subir también a tu `stores.json` local para desarrollo
3. La tienda aparece sola en el resumen y en "Cargar Postgres"

## Estructura del proyecto

```
monitor-post-hana/
├── AGENTS.md                  # Guía de operación para agentes (despliegue, server)
├── docs/
│   ├── mapping.md             # Mapeo de campos HANA ↔ POST (stale, ver AGENTS.md)
│   ├── spec_hana_views.md     # Especificación de vistas HANA (stale)
│   └── spec_xsjs.md           # Especificación del staging
├── pos_staging/
│   ├── POS_STAGING.hdbtable   # Definición tabla HANA
│   └── POS_STAGING_LOG.hdbtable
├── scripts/
│   ├── daily_staging.py       # Carga POS_STAGING (todas las tiendas)
│   └── deploy.ps1             # Deploy: GitHub + servidor intranet
├── src/
│   ├── app.py                 # Dashboard Streamlit (login gestor/revisor)
│   ├── config.py              # Conexión HANA + gestor de tiendas
│   ├── hana_queries.py        # Consultas HANA (resumen, detalle)
│   └── post_queries.py        # Carga PostgreSQL → POS_STAGING
├── stores.json                # Conexiones PostgreSQL por tienda (gitignored)
├── .env                       # Credenciales HANA + claves dashboard (gitignored)
├── .env.example               # Template de configuración
└── requirements.txt           # Dependencias Python
```

## Agregar una tienda nueva (desarrollo)

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

## Notas

- `src/pusher.py` es **WIP** (push por API REST) y no está conectado al dashboard.
- `docs/mapping.md` y `docs/spec_hana_views.md` están desactualizados; ver `AGENTS.md`.
