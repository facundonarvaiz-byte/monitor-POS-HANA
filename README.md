# 🔍 Monitor POS vs HANA — Materiales activos

Dashboard Streamlit para monitorear diferencias de materiales activos entre **SAP HANA**
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
HANA (imagen POS) ─────────────────────────────► POS_Comparacion / POS_vs_HANAxEAN_Ale (Calculation Views)
                                                           │
                                                    Streamlit Dashboard
```

- Las conexiones PostgreSQL se configuran por tienda en `stores.json` (producción) y `stores-test.json` (test), no en `.env`.
- El dashboard tiene un switch **Producción / Test** (default Producción): cambia HANA destino (`hl0-db` / `hd0-db`) y el JSON de tiendas. Las cargas a staging en Test se escriben **solo** en HANA test.
- La comparación de materiales la realiza la vista HANA `POS_Comparacion`; la comparación a nivel EAN usa la vista `POS_vs_HANAxEAN_Ale`.
- Python carga datos a `POS_STAGING` porque HANA XS Classic no puede conectarse
  a PostgreSQL externo.

## Requisitos

- Python 3.10+ (en el servidor de producción se usa Python 3.11)
- Acceso a SAP HANA (host, puerto, usuario y contraseña)
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
# Producción
HANA_HOST=hl0-db.example.corp
HANA_PORT=30015
HANA_USER=usuario
HANA_PASSWORD=contraseña

# Test (switch "Test" del dashboard)
HANA_TEST_HOST=hd0-db.cencosud.corp
HANA_TEST_PORT=30015
HANA_TEST_USER=usuario_test
HANA_TEST_PASSWORD=contraseña_test

# Autenticacion del dashboard (intranet)
GESTOR_PASS=gestor01
REVISOR_PASS=revisor01
```

> `HANA_SCHEMA` no se usa: las tablas y vistas están hardcodeadas en el schema `Z_NCR_CO`.

### `stores.json` — Conexiones PostgreSQL por tienda (producción)

Misma estructura en `stores-test.json` para las tiendas de test (ej. E011/E018).

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

1. **🌐 Ambiente** — Switch Producción / Test en la barra lateral. Al cambiar, recarga el resumen desde el HANA correspondiente y resetea la vista. En Test se muestra un banner de advertencia.
2. **Resumen** — Gráfico de barras y listado por tienda con el total de diferencias (escala fija 0–300 cuando el máximo no supera 300; si lo supera, autoescala con línea punteada en el umbral 300)
3. **Ver detalle →** — Grilla de diferencias para una tienda (hasta 20.000 filas en pantalla; el CSV exporta todo)
4. **🔀 POS vs HANA x EAN** — Resumen por tienda de EAN **Solo POS** (sin Solo HANA ni Match) con la misma escala 0–300 y línea de umbral, y detalle solo de EAN Solo POS, con export CSV
5. **🔄 Actualizar datos** — Recarga el resumen o el detalle según la vista activa
6. **⬆️ Cargar Postgres** (solo gestor) — Lee el PostgreSQL de la(s) tienda(s) y actualiza `POS_STAGING` en el ambiente activo
7. **Push cambios (tienda)** (solo gestor, con tienda seleccionada) — Encola los cambios hacia el POS, espera la confirmación asíncrona y recarga `POS_STAGING` desde PostgreSQL al terminar
8. **Correo diario** — Un cron de lunes a viernes a las 08:40 envía por XSJS el resumen principal (KPIs, gráfico y tabla por tienda) a los destinatarios de la vista HANA de `Dashboard_Mail`

## Correo diario (lunes a viernes 08:40)

`scripts/daily_email.py` consulta `get_resumen_tiendas("prod")`, genera un PNG con el
gráfico de "Materiales con diferencias por tienda" y envía un correo HTML a través del
endpoint XSJS de HANA `xsjs/send_monitor_email.xsjs` (usa `$.net.Mail`; el relay SMTP
lo configura HANA).

- **Remitente**: fijo `errores.ncr@easy.com.ar` (el XSJS rechaza otro).
- **Destinatarios**: se leen de la vista HANA compartida
  `Z_NCRCO.Log_views.Dashboard_Mail/Log_ReportMailer_Dashboard_DEST` (columna
  `DESTINATION`). Si no devuelve direcciones, el correo no se envía.
  `--to` permite una prueba puntual.
- **Envío**: `EMAIL_XSJS_URL` con Basic Auth usando `HANA_USER`/`HANA_PASSWORD`.
- **TLS**: `EMAIL_XSJS_VERIFY_TLS=0` si el certificado del XS es interno/self-signed.

Vista previa local (no envía):

```powershell
venv\Scripts\python scripts\daily_email.py --dry-run
# genera logs/email_preview.html y logs/grafico_resumen.png
```

Prueba de envío a un solo destinatario:

```powershell
venv\Scripts\python scripts\daily_email.py --to mi.correo@easy.com.ar
```

### Desplegar el XSJS en HANA

`xsjs/send_monitor_email.xsjs` **no se despliega con `deploy.ps1`**: se sube una vez al
paquete XS Classic de HANA prod (`hl0-db`) en `Z_NCRCO/Pos_staging/` con el XS IDE
(`http://hl0-db.cencosud.corp:8000/sap/hana/xs/ide/`). La URL queda
`https://hl0-db.cencosud.corp:4300/Z_NCRCO/Pos_staging/send_monitor_email.xsjs`
(también responde por HTTP en el puerto 8000). El paquete debe exigir Basic Auth
en su `.xsaccess` (no sobreescribir uno existente sin revisarlo):

```json
{
  "exposed": true,
  "authentication": [{"method": "Basic"}],
  "prevent_xsrf": true
}
```

La URL final se configura en `EMAIL_XSJS_URL`; el usuario `HANA_USER` debe tener
permiso sobre el paquete XS.

El cron del server (dentro de la ventana 08:00–19:01 de la VM) ejecuta:

```cron
40 8 * * 1-5 root /opt/pyapps/monitor-post-hana/venv/bin/python /opt/pyapps/monitor-post-hana/scripts/daily_email.py >> /opt/pyapps/monitor-post-hana/logs/daily_email.log 2>&1
```

El correo incluye la línea "Última carga de staging" (desde `POS_STAGING_LOG`) y la columna
"Últ. mod. POS" por tienda, para detectar si salió antes de que terminara la carga diaria
de las 08:10. Los fines de semana no se envía porque la VM está apagada.

## Despliegue (servidor intranet g100603aws079 / 172.23.18.102)

### Acceso

- Dashboard en producción: `http://g100603aws079/monitor`
- Servidor: Apache reverse proxy → Streamlit `127.0.0.1:8501` (websockets incluidos)
- Servicio systemd: `monitor-post-hana` (log de deploy: `/var/log/monitor-post-hana-deploy.log`)
- Carga diaria automática: cron a las **08:10** hora local (11:10 UTC), **de lunes a viernes** (la VM no se enciende los fines de semana), con `daily_staging.py` y log en `logs/daily_staging.log`
- Correo diario: cron a las **08:40** hora local, **de lunes a viernes**, con `daily_email.py` y log en `logs/daily_email.log`

### Cómo publicar un cambio

```powershell
scripts\deploy.ps1
```

Hace: commit pendiente (si hay) → `git push origin master` (GitHub) → `git push deploy master`
(server intranet). El hook `post-receive` del server hace checkout + `pip install` + restart del
servicio automáticamente. Alternativa manual: `git push origin master; git push deploy master`.

### Secrets (no viajan por git)

En el server, en `/opt/pyapps/monitor-post-hana/`, con permisos `root:root 600`:

- `.env` — HANA prod + `HANA_TEST_*` + `GESTOR_PASS`/`REVISOR_PASS` (las claves reales del dashboard) + `EMAIL_XSJS_URL`/`EMAIL_XSJS_VERIFY_TLS` (correo diario)
- `stores.json` — conexiones PostgreSQL por tienda (producción)
- `stores-test.json` — conexiones PostgreSQL de las tiendas de test

Para cambiar las claves del dashboard, o habilitar el switch Test en el server, editar el `.env` /
subir `stores-test.json` y `systemctl restart monitor-post-hana`. Los cambios de `EMAIL_XSJS_URL` /
`EMAIL_XSJS_VERIFY_TLS` los toma el cron en la siguiente ejecución (no requieren reiniciar el servicio).

### Agregar una tienda en producción

1. Editar `stores.json` **en el server** (root): `/opt/pyapps/monitor-post-hana/stores.json`
2. (Opcional) subir también a tu `stores.json` local para desarrollo
3. La tienda aparece sola en el resumen y en "Cargar Postgres"

## Estructura del proyecto

```
monitor-post-hana/
├── AGENTS.md                  # Guía de operación para agentes (despliegue, server)
├── docs/
│   ├── mapping.md             # Mapeo de campos HANA ↔ POS (stale, ver AGENTS.md)
│   ├── spec_hana_views.md     # Especificación de vistas HANA (stale)
│   └── spec_xsjs.md           # Especificación del staging
├── pos_staging/
│   ├── POS_STAGING.hdbtable   # Definición tabla HANA
│   └── POS_STAGING_LOG.hdbtable
├── scripts/
│   ├── daily_staging.py       # Carga POS_STAGING (todas las tiendas)
│   ├── daily_email.py         # Correo diario (resumen + gráfico vía XSJS)
│   └── deploy.ps1             # Deploy: GitHub + servidor intranet
├── xsjs/
│   └── send_monitor_email.xsjs  # Endpoint XSJS que envía el correo con $.net.Mail
├── src/
│   ├── app.py                 # Dashboard Streamlit (login gestor/revisor)
│   ├── config.py              # Conexión HANA + gestor de tiendas
│   ├── hana_queries.py        # Consultas HANA (resumen, detalle)
│   ├── post_queries.py        # Carga PostgreSQL → POS_STAGING
│   └── pusher.py              # Push al POS (SP + seguimiento asíncrono)
├── stores.json                # Conexiones PostgreSQL por tienda (gitignored)
├── stores-test.json           # Conexiones PostgreSQL tiendas de test (gitignored)
├── .env                       # Credenciales HANA (prod/test) + claves dashboard (gitignored)
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

Para tiendas de test, agregar la entrada en `stores-test.json` (y habilitarla en
`Z_NCR_WERKS_SEL` de HANA test; si esa tabla no se puede leer, se usan todas las del JSON).

## Notas

- El **push al POS es asíncrono**: el SP `PUSH_FROM_MONITOR` solo encola los materiales; el POS los procesa después. La demora observada es de **~30–50 minutos**. El dashboard consulta el estado cada 30 s y, cuando el POS confirma, recarga `POS_STAGING` desde PostgreSQL automáticamente.
- La vista por EAN (`POS_vs_HANAxEAN_Ale`) se regenera por ventanas en HANA. Si una tienda aparece como `ERROR` en el resumen, reintentar con **🔄 Actualizar datos**.
- `docs/mapping.md` y `docs/spec_hana_views.md` están desactualizados; ver `AGENTS.md`.
