# AGENTS.md

Streamlit dashboard (Cencosud Colombia) that detects material differences between SAP HANA (source of truth) and per-store PostgreSQL (POS). All code, comments, logs, and UI strings are in **Spanish** — keep new code in Spanish.

## Commands (Windows, venv already installed)

```powershell
streamlit run src/app.py                       # dashboard (run from repo root)
venv\Scripts\python scripts\daily_staging.py   # scheduled POS_STAGING load, all stores
```

No tests, linter, formatter, CI, or pyproject exist. `requirements.txt` is the only manifest. Repo is not a git repo (`.gitignore` present but no `.git`).

## Config

- `.env` — HANA credentials only (`HANA_HOST/PORT/USER/PASSWORD/SCHEMA`). Loaded by `src/config.py` from the project root; no env vars needed to run scripts.
- `stores.json` (root, gitignored) — per-store PostgreSQL connections keyed by store code (e.g. `E802`). Keys starting with `_` are treated as comments.
- `.env.example` also has `POSTGRES_*` vars — these are **unused**; per-store PG creds come only from `stores.json`. `HANA_SCHEMA` is also unused: HANA object names are hardcoded.

## Architecture

- `src/config.py` — global singletons `db` (HANA engine) and `store_manager` (per-store PG engines). Import these; don't create engines directly.
- `src/hana_queries.py` — the comparison is done by the HANA Calculation View `"_SYS_BIC"."Z_NCRCO.Pos_staging/POS_Comparacion"`, called per store with `('PLACEHOLDER' = ('$$WERKS_RUN$$', '<store>'))`. `get_resumen_tiendas()` calls `get_detalle_tienda()` per store (N queries).
- `src/post_queries.py` — `populate_pos_staging(tienda)`: reads PG tables `article`/`article_extended`/`department`, then DELETE-by-store + batched INSERT (500 rows) into `"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING"` (the HANA XSJS trigger equivalent). Logs each run to `POS_STAGING_LOG` with `ID = MAX(ID)+1` (XS Classic has no IDENTITY); log failures are non-fatal.
- `pos_staging/*.hdbtable` — HANA table definitions; keep in sync with `post_queries.py` inserts.

## Gotchas

- `docs/mapping.md` and `docs/spec_hana_views.md` are **stale**: they describe a `comparator.py` / `get_imagen_post()` / `DIF_RESUMEN_TIENDAS` design that no longer exists. The executable source is `src/*`.
- `src/pusher.py` is **WIP and not wired into `app.py`**: it references `db.postgres` (doesn't exist — there's only `db.hana` + `store_manager`) and a placeholder table `public.tu_tabla_hash_plu`. Don't assume it works.
- `get_detalle_tienda()` validates store codes with `^[A-Za-z0-9]+$` before interpolating into the HANA view call — keep that guard on any new query taking a store code.
- All HANA identifiers are hardcoded with schema `Z_NCR_CO` (e.g. `"Z_NCR_CO"."Z_NCRCO.Pos_staging::POS_STAGING"`); changing `HANA_SCHEMA` in `.env` has no effect on them.
- App queries live HANA/PG on every startup and rerun; no mock/fixture layer exists.
