# AGENTS.md

Streamlit dashboard (Cencosud Colombia) that detects material differences between SAP HANA (source of truth) and per-store PostgreSQL (POS). All code, comments, logs, and UI strings are in **Spanish** — keep new code in Spanish.

## Commands (Windows, venv already installed)

```powershell
streamlit run src/app.py                       # dashboard local (run from repo root)
venv\Scripts\python scripts\daily_staging.py   # carga POS_STAGING, todas las tiendas (local)
scripts\deploy.ps1                             # deploy: commit pendiente + push GitHub + servidor
```

No tests, linter, formatter, CI, or pyproject exist. `requirements.txt` is the only manifest. The repo is a git repo: remotes `origin` (GitHub: `https://github.com/facundonarvaiz-byte/monitor-POS-HANA`) y `deploy` (servidor intranet).

## Despliegue (servidor intranet g100603aws079 / 172.23.18.102)

### Flujo normal (cada cambio)

1. `scripts\deploy.ps1` (o manualmente `git push origin master` + `git push deploy master`).
2. El hook `post-receive` del repo bare del server (`/opt/git/monitor-post-hana.git`) corre `/opt/git/monitor-post-hana-deploy.sh`:
   `git checkout -f master` → `pip install -r requirements.txt` → `systemctl restart monitor-post-hana`.
3. Verificar: `http://g100603aws079/monitor` y log `/var/log/monitor-post-hana-deploy.log`.

### Componentes

- Dashboard: `http://g100603aws079/monitor` — Apache (`/etc/apache2/conf.d/monitor.conf`) hace reverse proxy a Streamlit `127.0.0.1:8501` con `--server.baseUrlPath monitor`. La regla `ProxyPass /monitor/_stcore/stream ws://...` es obligatoria para los websockets.
- Servicio systemd `monitor-post-hana` (User=root, Restart=always). ExecStart usa `/opt/pyapps/monitor-post-hana/venv/bin/streamlit run src/app.py --server.port 8501 --server.address 127.0.0.1 --server.headless true --server.baseUrlPath monitor`.
- Carga diaria: `/etc/cron.d/monitor-post-hana` a las **05:00** → `venv/bin/python scripts/daily_staging.py` (root, log en `logs/daily_staging.log`). La corrida completa tarda ~6-8 min (E812 es la más lenta).
- Secrets en `/opt/pyapps/monitor-post-hana/`: `.env` (HANA + GESTOR_PASS/REVISOR_PASS) y `stores.json`, ambos `root:root 600`, gitignored y **no viajan por git**: se suben a mano con pscp.
- Login del dashboard: usuarios `gestor` / `revisor` con claves en `.env` del server (`.env` local usa valores de desarrollo `gestor01`/`revisor01`). `revisor` no ve el botón "Cargar Postgres".

### SSH al server (patrones para agentes)

- Usuario **`ec0326`** (password provista por el equipo). Los usuarios de AD (ej. `ama5813`) **no tienen sudo**; `ec0326` sí via NOPASSWD.
- Root: `sudo -n su - -c "<comando>"`. El wrapper es **restrictivo**: rechaza `;`, `&&`, `sh <script>`, pipes; acepta comandos simples, `sh -c "..."` (con `>`/`<`) y **scripts ejecutables**. La salida del wrapper se traga — loguear a archivos y luego `cat`.
- El shell de login de `ec0326` es tcsh: **no anidar quoting** (`bash -c "...\"...\""` se rompe). Patrón probado: subir script con pscp → `chmod 755` → ejecutar. Para root, el script se auto-eleva:
  ```bash
  #!/bin/bash
  if [ "$(id -u)" != "0" ]; then sudo -n su - -c "$0"; exit $?; fi
  ... # resto del script como root, con salida a un archivo en /tmp
  ```
- Clave SSH de la máquina de desarrollo en `/home/CENCOSUD/ec0326/.ssh/authorized_keys` (push de git sin password).
- El venv del server usa **`/usr/bin/python3.11`** (el Python 3.13 de `/usr/local` no tiene `sqlite3`, que `pandas.read_sql` necesita). No cambiar a 3.13.

### No tocar

Unidades/flask-proxy/tomcat, apache existente, crontab de root, `/srv/www/htdocs`, otros `/opt/*`, `/etc/sudoers*`. Solo se agregan archivos nuevos (`/opt/pyapps/monitor-post-hana`, `/opt/git/*`, `monitor-post-hana.service`, `monitor.conf`, `/etc/cron.d/monitor-post-hana`).

## Config

- `.env` — HANA credentials + `GESTOR_PASS`/`REVISOR_PASS` (claves del dashboard). Loaded by `src/config.py` from the project root; no env vars needed to run scripts.
- `stores.json` (root, gitignored) — per-store PostgreSQL connections keyed by store code (e.g. `E802`). Keys starting with `_` are treated as comments.
- `.env.example` also has `POSTGRES_*` vars — these are **unused**; per-store PG creds come only from `stores.json`. `HANA_SCHEMA` is also unused: HANA object names are hardcoded.

## Architecture

- `src/config.py` — global singletons `db` (HANA engine) and `store_manager` (per-store PG engines). Import these; don't create engines directly.
- `src/hana_queries.py` — the comparison is done by the HANA Calculation View `"_SYS_BIC"."Z_NCRCO.Pos_staging/POS_Comparacion"`, called per store with `('PLACEHOLDER' = ('$$WERKS_RUN$$', '<store>'))`. `get_resumen_tiendas()` calls `get_detalle_tienda()` per store (N queries).
- `src/post_queries.py` — `populate_pos_staging(tienda)`: reads PG tables `article`/`article_extended`/`department`, then DELETE-by-store + batched INSERT (500 rows) into `"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING"` (the HANA XSJS trigger equivalent). Logs each run to `POS_STAGING_LOG` with `ID = MAX(ID)+1` (XS Classic has no IDENTITY); log failures are non-fatal.
- `src/app.py` — dashboard + login con roles. `gestor` (todas las acciones) / `revisor` (oculta el botón de staging). Claves leídas de `.env` via `_AUTH_ROLES = {"gestor": "GESTOR_PASS", "revisor": "REVISOR_PASS"}`.
- `pos_staging/*.hdbtable` — HANA table definitions; keep in sync with `post_queries.py` inserts.

## Gotchas

- `docs/mapping.md` y `docs/spec_hana_views.md` son **stale**: describen un `comparator.py` / `get_imagen_post()` / `DIF_RESUMEN_TIENDAS` que ya no existe. El código ejecutable es `src/*`.
- `src/pusher.py` es **WIP y no está conectado a `app.py`**: referencia `db.postgres` (no existe — solo `db.hana` + `store_manager`) y una tabla placeholder `public.tu_tabla_hash_plu`. No asumir que funciona.
- `get_detalle_tienda()` valida códigos de tienda con `^[A-Za-z0-9]+$` antes de interpolar en la vista HANA — mantener ese guard en cualquier query nueva que reciba un código de tienda.
- Los identificadores HANA están hardcodeados con schema `Z_NCR_CO` (ej. `"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING"`); cambiar `HANA_SCHEMA` en `.env` no tiene efecto.
- La app consulta HANA/PG en vivo en cada arranque y rerun; no hay capa de mock/fixtures. El primer load del resumen tarda ~2 min (una query por tienda).
- `get_resumen_tiendas()` clasifica estado: 0 diffs → `OK`, <50 → `ALERTA`, ≥50 → `CRITICO`.
- **Los passwords de HANA vencen**: si el dashboard muestra `Error cargando ... (414) 'user is forced to change password: alter password required for user AMA5813'`, la contraseña de `HANA_USER` en `.env` venció. Hay que cambiarla (DBeaver/Studio o el admin de HANA) y actualizarla en `.env` del server + local + `systemctl restart monitor-post-hana`. Cuidado: hay varios sistemas HANA (`hl0-db` vs `hd0-db`); la app usa `hl0-db.cencosud.corp:30015`.
- `.playwright-mcp/` (artefactos de pruebas de navegador) está gitignored — no commitear.
- La carga de staging escribe `DELETE + INSERT` en `POS_STAGING` por tienda; correrla actualiza los datos del dashboard. Es la operación diaria de las 05:00.
