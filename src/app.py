"""
Monitor POS vs HANA — Dashboard de Comparacion de materiales activos.
Ejecutar con: streamlit run src/app.py
"""

import math
import os

import streamlit as st
import pandas as pd
import plotly.express as px

from config import logger, db
from hana_queries import (
    get_resumen_tiendas,
    get_detalle_tienda,
    get_logs_staging,
    get_cp_logs,
    get_resumen_ean,
    get_detalle_ean,
    JOIN_TYPES_EAN,
)
from post_queries import (
    populate_pos_staging,
    listar_tiendas_postgres,
    actualizar_pos_staging_por_sku,
)


# ============================================================
# CONFIGURACION DE PAGINA
# ============================================================

st.set_page_config(
    page_title="Monitor POS vs HANA — Materiales activos",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# ESTADO DE SESION
# ============================================================

defaults = {
    "ambiente": "prod",
    "tienda_seleccionada": None,
    "df_resumen": None,
    "df_detalle": None,
    "df_logs": None,
    "df_cp_logs": None,
    "ver_logs": False,
    "ver_cp_logs": False,
    "ver_ean": False,
    "df_resumen_ean": None,
    "df_detalle_ean": None,
    "ean_tienda": None,
    "ean_join_type": "LO",
    "autenticado": False,
    "rol": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ============================================================
# AUTENTICACION — roles: gestor (con acciones) / revisor (solo lectura)
# Las claves se leen del .env (GESTOR_PASS / REVISOR_PASS)
# ============================================================

_AUTH_ROLES = {"gestor": "GESTOR_PASS", "revisor": "REVISOR_PASS"}


def _autenticar(usuario: str, clave: str) -> bool:
    var = _AUTH_ROLES.get(usuario.strip().lower())
    if not var:
        return False
    return clave == os.getenv(var, "")


def _login_ui():
    st.markdown(
        "<div style='text-align:center; margin-top:8vh'>"
        "<h2>🔍 Monitor POS vs HANA</h2>"
        "<p style='color:#888'>Materiales activos — Ingresá para continuar</p>"
        "</div>",
        unsafe_allow_html=True,
    )
    with st.form("login"):
        usuario = st.text_input("Usuario", placeholder="gestor o revisor")
        clave = st.text_input("Contraseña", type="password")
        enviar = st.form_submit_button("Ingresar", type="primary", use_container_width=True)

    if enviar:
        if _autenticar(usuario, clave):
            st.session_state.autenticado = True
            st.session_state.rol = usuario.strip().lower()
            st.rerun()
        else:
            st.error("Usuario o contraseña incorrectos.")


if not st.session_state.autenticado:
    _login_ui()
    st.stop()


# ============================================================
# HELPERS
# ============================================================

def _semaforo(estado: str) -> str:
    return {
        "OK":      "🟢",
        "ALERTA":  "🟡",
        "CRITICO": "🔴",
        "ERROR":   "⚫",
    }.get(estado, "⚪")


# Máximo de filas que se muestran en la grilla de la vista por EAN;
# el CSV exporta el total (MA/AA pueden superar las 200.000 filas).
PREVIEW_EAN_FILAS = 20_000

_EAN_COL_LABELS = {
    "hana_tienda":      "Tienda HANA",
    "pos_ean":          "EAN POS",
    "pos_sku":          "SKU POS",
    "pos_descripcion":  "Desc. POS",
    "hana_ean":         "EAN HANA",
    "hana_sku":         "SKU HANA",
    "hana_ausente":     "Ausente HANA",
    "pos_ausente":      "Ausente POS",
    "match":            "Match",
    "hana_precio":      "Precio HANA",
    "pos_precio":       "Precio POS",
    "pos_tienda":       "Tienda POS",
    "hana_activo":      "Activo HANA",
    "hana_umv":         "UMV",
    "hana_ppal":        "EAN Principal",
    "pos_activo":       "Activo POS",
    "fecha_carga_pos":  "Últ. Carga POS",
    "hana_descripcion": "Desc. HANA",
    "show_ean":         "Mostrar EAN",
    "tipo":             "Tipo",
    "diff_precio":      "Diff. Precio",
}


_AMBIENTES_LABEL = {"Producción": "prod", "Test": "test"}


def _etiqueta_ambiente(ambiente: str) -> str:
    return "Test" if ambiente == "test" else "Producción"


@st.cache_data(ttl=300, show_spinner=False)
def _resumen_cacheados(ambiente: str) -> pd.DataFrame:
    """
    Resumen compartido entre sesiones (TTL 5 min) para no recalcularlo por usuario.
    La caché está separada por ambiente (prod/test no se pisan).
    """
    return get_resumen_tiendas(ambiente)


def _cargar_resumen(ambiente: str, forzar: bool = False):
    etiqueta = _etiqueta_ambiente(ambiente)
    with st.spinner(f"Cargando resumen de tiendas ({etiqueta})..."):
        try:
            if forzar:
                _resumen_cacheados.clear()
            st.session_state.df_resumen = _resumen_cacheados(ambiente)
        except Exception as ex:
            st.error(f"Error cargando resumen ({etiqueta}): {ex}")
            logger.exception("Error en get_resumen_tiendas")


def _cargar_detalle(tienda: str, ambiente: str):
    etiqueta = _etiqueta_ambiente(ambiente)
    with st.spinner(f"Cargando detalle de {tienda} ({etiqueta})..."):
        try:
            st.session_state.df_detalle = get_detalle_tienda(tienda, ambiente)
            st.session_state.tienda_seleccionada = tienda
            st.session_state.pagina_detalle = 1  # resetear paginación
            st.session_state.ver_logs = False
            st.session_state.ver_cp_logs = False
        except Exception as ex:
            st.error(f"Error cargando detalle de {tienda} ({etiqueta}): {ex}")
            logger.exception("Error en get_detalle_tienda")


def _cargar_logs(ambiente: str):
    etiqueta = _etiqueta_ambiente(ambiente)
    with st.spinner(f"Cargando logs de staging ({etiqueta})..."):
        try:
            st.session_state.df_logs = get_logs_staging(ambiente)
        except Exception as ex:
            st.error(f"Error cargando logs ({etiqueta}): {ex}")
            logger.exception("Error en get_logs_staging")


def _cargar_cp_logs(ambiente: str):
    etiqueta = _etiqueta_ambiente(ambiente)
    with st.spinner(f"Cargando logs CP_CVE ({etiqueta})..."):
        try:
            st.session_state.df_cp_logs = get_cp_logs(ambiente)
        except Exception as ex:
            st.error(f"Error cargando logs CP_CVE ({etiqueta}): {ex}")
            logger.exception("Error en get_cp_logs")


@st.cache_data(ttl=300, show_spinner=False)
def _ean_resumen_cacheados(ambiente: str) -> pd.DataFrame:
    """
    Resumen por EAN compartido entre sesiones (TTL 5 min).
    La caché está separada por ambiente (prod/test no se pisan).
    """
    return get_resumen_ean(ambiente)


def _cargar_resumen_ean(ambiente: str, forzar: bool = False):
    etiqueta = _etiqueta_ambiente(ambiente)
    with st.spinner(f"Cargando resumen por EAN ({etiqueta})..."):
        try:
            if forzar:
                _ean_resumen_cacheados.clear()
            st.session_state.df_resumen_ean = _ean_resumen_cacheados(ambiente)
        except Exception as ex:
            st.error(f"Error cargando resumen por EAN ({etiqueta}): {ex}")
            logger.exception("Error en get_resumen_ean")


def _cargar_detalle_ean(tienda: str, join_type: str, ambiente: str):
    etiqueta = _etiqueta_ambiente(ambiente)
    modo = JOIN_TYPES_EAN.get(join_type, join_type)
    with st.spinner(f"Cargando EANs de {tienda} — {modo} ({etiqueta})..."):
        try:
            st.session_state.df_detalle_ean = get_detalle_ean(tienda, join_type, ambiente)
            st.session_state.ean_tienda = tienda
            st.session_state.ean_join_type = join_type
        except Exception as ex:
            st.error(f"Error cargando EANs de {tienda} ({etiqueta}): {ex}")
            logger.exception("Error en get_detalle_ean")


def _resetear_vistas():
    """Limpia los datos cargados al cambiar de ambiente."""
    st.session_state.df_resumen = None
    st.session_state.df_detalle = None
    st.session_state.df_logs = None
    st.session_state.df_cp_logs = None
    st.session_state.tienda_seleccionada = None
    st.session_state.ver_logs = False
    st.session_state.ver_cp_logs = False
    st.session_state.ver_ean = False
    st.session_state.df_resumen_ean = None
    st.session_state.df_detalle_ean = None
    st.session_state.ean_tienda = None
    st.session_state.ean_join_type = "LO"


# ============================================================
# CARGA INICIAL (primera vez que abre la app)
# ============================================================

if st.session_state.df_resumen is None:
    _cargar_resumen(st.session_state.ambiente)


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.title("🔍 Monitor POS vs HANA")
st.sidebar.caption("Materiales activos · v2.2")
st.sidebar.caption(f"👤 Sesión: **{st.session_state.rol}**")

# -- Switch de ambiente: Producción (default) / Test -----------------
_sel_ambiente = st.sidebar.radio(
    "🌐 Ambiente",
    list(_AMBIENTES_LABEL.keys()),
    index=0,
    horizontal=True,
    key="selector_ambiente",
)
_ambiente_sel = _AMBIENTES_LABEL[_sel_ambiente]
if _ambiente_sel != st.session_state.ambiente:
    st.session_state.ambiente = _ambiente_sel
    _resetear_vistas()
    _cargar_resumen(_ambiente_sel, forzar=True)

if st.session_state.ambiente == "test":
    st.sidebar.warning("🧪 AMBIENTE TEST")
st.sidebar.caption(f"🗄️ HANA: `{db.host(st.session_state.ambiente)}`")

if st.sidebar.button("🚪 Cerrar sesión", use_container_width=True):
    st.session_state.autenticado = False
    st.session_state.rol = None
    st.rerun()
st.sidebar.markdown("---")

if st.session_state.ver_logs:
    if st.sidebar.button("← Volver al resumen", use_container_width=True):
        st.session_state.ver_logs = False
        st.session_state.tienda_seleccionada = None
        st.session_state.df_detalle = None
        st.session_state.df_logs = None
        st.rerun()
    st.sidebar.markdown("---")
elif st.session_state.ver_cp_logs:
    if st.sidebar.button("← Volver al resumen", use_container_width=True):
        st.session_state.ver_cp_logs = False
        st.session_state.tienda_seleccionada = None
        st.session_state.df_detalle = None
        st.session_state.df_cp_logs = None
        st.rerun()
    st.sidebar.markdown("---")
elif st.session_state.ver_ean:
    if st.sidebar.button("← Volver al resumen", use_container_width=True):
        if st.session_state.ean_tienda:
            # Del detalle EAN → resumen EAN
            st.session_state.ean_tienda = None
            st.session_state.df_detalle_ean = None
        else:
            # Del resumen EAN → resumen principal
            st.session_state.ver_ean = False
        st.rerun()
    st.sidebar.markdown("---")
elif st.session_state.tienda_seleccionada:
    if st.sidebar.button("← Volver al resumen", use_container_width=True):
        st.session_state.tienda_seleccionada = None
        st.session_state.df_detalle = None
        st.rerun()
    st.sidebar.markdown("---")

if st.session_state.ver_ean and st.session_state.ean_tienda:
    lbl_actualizar = f"🔄 Actualizar {st.session_state.ean_tienda}"
elif st.session_state.tienda_seleccionada:
    lbl_actualizar = f"🔄 Actualizar {st.session_state.tienda_seleccionada}"
else:
    lbl_actualizar = "🔄 Actualizar datos"
btn_actualizar = st.sidebar.button(lbl_actualizar, use_container_width=True, type="primary")

_sufijo_test = " TEST" if st.session_state.ambiente == "test" else ""
lbl_staging = (
    f"⬆️ Cargar Postgres ({st.session_state.tienda_seleccionada}){_sufijo_test}"
    if st.session_state.tienda_seleccionada
    else f"⬆️ Cargar Postgres (todas las tiendas){_sufijo_test}"
)
btn_staging = None
if st.session_state.rol == "gestor":
    btn_staging = st.sidebar.button(lbl_staging, use_container_width=True)
else:
    st.sidebar.caption("🔒 Solo lectura — la carga de staging la ejecuta un gestor.")

btn_logs = st.sidebar.button("📜 Ver logs de staging", use_container_width=True)
btn_cp_logs = st.sidebar.button("🧾 Ver logs CP_CVE", use_container_width=True)
btn_ean = st.sidebar.button("🔀 POS vs HANA x EAN", use_container_width=True)


# ============================================================
# LOGICA DE BOTONES
# ============================================================

ambiente_actual = st.session_state.ambiente
etiqueta_ambiente = _etiqueta_ambiente(ambiente_actual)

if btn_actualizar:
    if st.session_state.ver_logs:
        _cargar_logs(ambiente_actual)
    elif st.session_state.ver_cp_logs:
        _cargar_cp_logs(ambiente_actual)
    elif st.session_state.ver_ean:
        if st.session_state.ean_tienda:
            _cargar_detalle_ean(
                st.session_state.ean_tienda,
                st.session_state.ean_join_type,
                ambiente_actual,
            )
        else:
            _cargar_resumen_ean(ambiente_actual, forzar=True)
    elif st.session_state.tienda_seleccionada:
        _cargar_detalle(st.session_state.tienda_seleccionada, ambiente_actual)
    else:
        _cargar_resumen(ambiente_actual, forzar=True)

if btn_logs:
    st.session_state.ver_logs = True
    st.session_state.ver_cp_logs = False
    st.session_state.ver_ean = False
    _cargar_logs(ambiente_actual)

if btn_cp_logs:
    st.session_state.ver_cp_logs = True
    st.session_state.ver_logs = False
    st.session_state.ver_ean = False
    _cargar_cp_logs(ambiente_actual)

if btn_ean:
    st.session_state.ver_ean = True
    st.session_state.ver_logs = False
    st.session_state.ver_cp_logs = False
    st.session_state.tienda_seleccionada = None
    st.session_state.df_detalle = None
    st.session_state.ean_tienda = None
    st.session_state.df_detalle_ean = None
    if st.session_state.df_resumen_ean is None:
        _cargar_resumen_ean(ambiente_actual)

if btn_staging:
    if st.session_state.tienda_seleccionada:
        tiendas_pg = [st.session_state.tienda_seleccionada]
    else:
        tiendas_pg = listar_tiendas_postgres(ambiente_actual)

    if not tiendas_pg:
        st.sidebar.warning(f"⚠️ No hay tiendas configuradas para el ambiente {etiqueta_ambiente}.")
    else:
        resultados = []
        barra = st.progress(0, text=f"Iniciando carga en {etiqueta_ambiente}...")
        for i, t in enumerate(tiendas_pg):
            barra.progress(
                (i + 1) / len(tiendas_pg),
                text=f"Cargando {t}... ({i + 1}/{len(tiendas_pg)})",
            )
            resultados.append(populate_pos_staging(t, ambiente_actual))
        barra.empty()

        ok_count  = sum(1 for r in resultados if r["ok"])
        err_count = len(resultados) - ok_count
        total_reg = sum(r["registros"] for r in resultados)

        if err_count == 0:
            st.success(
                f"✅ {ok_count} tienda(s) cargadas en **{etiqueta_ambiente}** "
                f"— {total_reg:,} registros totales."
            )
        else:
            st.warning(
                f"⚠️ {etiqueta_ambiente}: {ok_count} OK, {err_count} con errores "
                f"— {total_reg:,} registros cargados."
            )

        with st.expander("Ver detalle del staging"):
            st.dataframe(
                pd.DataFrame(resultados)[["ambiente", "tienda", "registros", "duracion_ms", "ok"]],
                use_container_width=True,
                hide_index=True,
            )


# ============================================================
# CUERPO PRINCIPAL
# ============================================================

if ambiente_actual == "test":
    st.warning(
        f"🧪 **AMBIENTE TEST** — HANA `{db.host('test')}` · tiendas de `stores-test.json`. "
        "Las lecturas y cargas se hacen únicamente contra el ambiente de test."
    )

# -- VISTA LOGS CP_CVE ---------------------------------------

if st.session_state.ver_cp_logs:
    if st.session_state.df_cp_logs is None:
        _cargar_cp_logs(ambiente_actual)

    df_cp_logs = st.session_state.df_cp_logs

    st.subheader("🧾 Logs CP_CVE")

    if df_cp_logs is None or df_cp_logs.empty:
        st.info("No hay registros en el log CP_CVE.")
    else:
        cols_metricas = st.columns(3)
        with cols_metricas[0]:
            st.metric("Registros mostrados", len(df_cp_logs))
        niveles = df_cp_logs["level"].value_counts()
        for i, (nivel, conteo) in enumerate(niveles.head(2).items(), start=1):
            with cols_metricas[i]:
                st.metric(f"Level {nivel}", int(conteo))

        st.markdown("---")

        col_info, col_dl = st.columns([4, 1])
        with col_info:
            st.caption("Últimas 200 líneas — las más recientes primero. Usá el 🔍 de la grilla para filtrar.")
        with col_dl:
            csv_cp_logs = df_cp_logs.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Exportar CSV",
                data=csv_cp_logs,
                file_name="logs_cp_cve.csv",
                mime="text/csv",
                use_container_width=True,
            )

        st.dataframe(
            df_cp_logs.rename(columns={
                "job_id": "Job ID",
                "ts":     "TS",
                "level":  "Level",
                "message": "Mensaje",
            }),
            use_container_width=True,
            hide_index=True,
            height=620,
        )


# -- VISTA LOGS DE STAGING -----------------------------------

elif st.session_state.ver_logs:
    if st.session_state.df_logs is None:
        _cargar_logs(ambiente_actual)

    df_logs = st.session_state.df_logs

    st.subheader("📜 Log de cargas POS_STAGING")

    if df_logs is None or df_logs.empty:
        st.info("No hay registros en el log de staging.")
    else:
        ok_count  = int((df_logs["estado"] == "OK").sum())
        err_count = int((df_logs["estado"] == "ERROR").sum())

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Registros mostrados", len(df_logs))
        with col2:
            st.metric("Cargas OK ✅", ok_count)
        with col3:
            st.metric("Cargas con ERROR ❌", err_count)

        st.markdown("---")

        col_info, col_dl = st.columns([4, 1])
        with col_info:
            st.caption("Últimas 200 ejecuciones — las más recientes primero. Usá el 🔍 de la grilla para filtrar.")
        with col_dl:
            csv_logs = df_logs.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Exportar CSV",
                data=csv_logs,
                file_name="logs_staging.csv",
                mime="text/csv",
                use_container_width=True,
            )

        df_estado = df_logs.copy()
        df_estado["estado"] = df_estado["estado"].map(
            {"OK": "✅ OK", "ERROR": "❌ ERROR"}
        ).fillna(df_estado["estado"])

        st.dataframe(
            df_estado.rename(columns={
                "id":        "ID",
                "tienda":    "Tienda",
                "inicio":    "Inicio",
                "fin":       "Fin",
                "registros": "Registros",
                "estado":    "Estado",
                "mensaje":   "Mensaje",
            }),
            use_container_width=True,
            hide_index=True,
            height=620,
        )


# -- VISTA POS vs HANA POR EAN --------------------------------

elif st.session_state.ver_ean:
    if st.session_state.df_resumen_ean is None and st.session_state.ean_tienda is None:
        _cargar_resumen_ean(ambiente_actual)

    df_res_ean = st.session_state.df_resumen_ean

    # ── Detalle por tienda ─────────────────────────────────
    if st.session_state.ean_tienda and st.session_state.df_detalle_ean is not None:
        tienda_ean = st.session_state.ean_tienda
        df_ean = st.session_state.df_detalle_ean

        st.subheader(f"🔀 Tienda **{tienda_ean}** — POS vs HANA por EAN")

        _opciones_ean = list(JOIN_TYPES_EAN.keys())
        _idx_ean = (
            _opciones_ean.index(st.session_state.ean_join_type)
            if st.session_state.ean_join_type in _opciones_ean
            else 0
        )
        jt_sel = st.radio(
            "Modalidad",
            _opciones_ean,
            index=_idx_ean,
            horizontal=True,
            format_func=lambda k: f"{JOIN_TYPES_EAN[k]} ({k})",
        )
        if jt_sel != st.session_state.ean_join_type:
            # Sin st.rerun(): si la vista HANA falla, se evita reintentar en loop.
            _cargar_detalle_ean(tienda_ean, jt_sel, ambiente_actual)
            df_ean = st.session_state.df_detalle_ean

        if df_ean.empty:
            st.success("✅ No hay filas para esta modalidad y tienda.")
        else:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Filas", f"{len(df_ean):,}")
            with col2:
                st.metric("Solo POS", f"{int((df_ean['tipo'] == 'SOLO_POS').sum()):,}")
            with col3:
                st.metric("Solo HANA", f"{int((df_ean['tipo'] == 'SOLO_HANA').sum()):,}")
            with col4:
                st.metric("Match", f"{int((df_ean['tipo'] == 'MATCH').sum()):,}")

            st.markdown("---")

            # ── Filtro tipo/precio + descarga ──────────────────
            col_filtro, col_chk, col_info, col_dl = st.columns([3, 2, 3, 1])

            with col_filtro:
                tipos_disp = sorted(df_ean["tipo"].dropna().unique())
                tipos_sel = st.multiselect("Tipo", tipos_disp, default=tipos_disp)
                df_vista = df_ean[df_ean["tipo"].isin(tipos_sel)]

            with col_chk:
                if df_ean["diff_precio"].any():
                    if st.checkbox("Solo diff. de precio"):
                        df_vista = df_vista[df_vista["diff_precio"]]

            csv_ean = df_vista.to_csv(index=False).encode("utf-8")

            with col_info:
                caption = f"{len(df_vista):,} filas"
                if len(df_vista) > PREVIEW_EAN_FILAS:
                    caption += (
                        f" — se muestran las primeras {PREVIEW_EAN_FILAS:,} "
                        f"en pantalla (el CSV incluye todas)"
                    )
                st.caption(caption + " — usá el 🔍 de la grilla para buscar y las cabeceras para ordenar")

            with col_dl:
                st.download_button(
                    "📥 Exportar CSV",
                    data=csv_ean,
                    file_name=f"ean_{tienda_ean}_{st.session_state.ean_join_type}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

            st.dataframe(
                df_vista.rename(columns=_EAN_COL_LABELS).head(PREVIEW_EAN_FILAS),
                use_container_width=True,
                hide_index=True,
                height=620,
            )

    # ── Resumen por tienda ─────────────────────────────────
    else:
        st.subheader("🔀 POS vs HANA por EAN — resumen por tienda")

        if df_res_ean is None or df_res_ean.empty:
            st.info("No se pudo cargar el resumen por EAN. Usá **🔄 Actualizar datos** para reintentar.")
        else:
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Tiendas monitoreadas", len(df_res_ean))
            with col2:
                st.metric("EANs solo POS", f"{int(df_res_ean['solo_pos'].sum()):,}")
            with col3:
                st.metric("EANs solo HANA", f"{int(df_res_ean['solo_hana'].sum()):,}")
            with col4:
                st.metric("EANs match", f"{int(df_res_ean['coincidencias'].sum()):,}")

            errores_ean = (
                int((df_res_ean["estado"] == "ERROR").sum())
                if "estado" in df_res_ean.columns
                else 0
            )
            if errores_ean:
                st.warning(
                    f"⚠️ {errores_ean} tienda(s) sin datos: la vista HANA por EAN "
                    "se regenera por ventanas. Reintentá con **🔄 Actualizar datos**."
                )

            st.markdown("---")

            df_plot = df_res_ean.melt(
                id_vars="tienda",
                value_vars=["solo_pos", "solo_hana", "coincidencias"],
                var_name="grupo",
                value_name="ean",
            )
            df_plot["grupo"] = df_plot["grupo"].map({
                "solo_pos":      "Solo POS",
                "solo_hana":     "Solo HANA",
                "coincidencias": "Match",
            })
            fig_ean = px.bar(
                df_plot,
                x="tienda",
                y="ean",
                color="grupo",
                barmode="group",
                color_discrete_map={
                    "Solo POS":  "#f59e0b",
                    "Solo HANA": "#3b82f6",
                    "Match":     "#10b981",
                },
                title="EANs por tienda",
                labels={"tienda": "Tienda", "ean": "EANs", "grupo": ""},
            )
            fig_ean.update_layout(xaxis_title="Tienda", yaxis_title="EANs", legend_title="")
            st.plotly_chart(fig_ean, use_container_width=True)

            st.markdown("---")

            for _, row in df_res_ean.iterrows():
                tienda_cod = row.get("tienda", "-")
                estado_ean = row.get("estado", "OK")

                col_info, col_btn = st.columns([5, 1])
                with col_info:
                    st.markdown(
                        f"{_semaforo(estado_ean)} **{tienda_cod}** &nbsp;|&nbsp; "
                        f"Solo POS: {int(row.get('solo_pos', 0)):,} &nbsp;|&nbsp; "
                        f"Solo HANA: {int(row.get('solo_hana', 0)):,} &nbsp;|&nbsp; "
                        f"Match: {int(row.get('coincidencias', 0)):,}"
                    )
                with col_btn:
                    if st.button(
                        "Ver detalle →",
                        key=f"ean_det_{tienda_cod}",
                        use_container_width=True,
                    ):
                        _cargar_detalle_ean(tienda_cod, "LO", ambiente_actual)
                        st.rerun()


# -- VISTA DETALLE -------------------------------------------

elif st.session_state.tienda_seleccionada and st.session_state.df_detalle is not None:
    tienda = st.session_state.tienda_seleccionada
    df_det = st.session_state.df_detalle

    st.subheader(f"📋 Tienda **{tienda}** — Diferencias de materiales activos")

    if df_det.empty:
        st.success("✅ No hay diferencias para esta tienda.")
    else:
        col1, col2, col3 = st.columns(3)
        if {"sku", "diff_precio", "diff_restringido", "ean", "ean_pos"} <= set(df_det.columns):
            skus = df_det["sku"]
            with col1:
                st.metric("Diffs precio 💰", int(skus[df_det["diff_precio"]].nunique()))
            with col2:
                st.metric("Diffs restringido 🚫", int(skus[df_det["diff_restringido"]].nunique()))
            with col3:
                st.metric("Solo en HANA", int(skus[df_det["ean_pos"].isna()].nunique()))

        st.markdown("---")

        _col_labels = {
            "tienda":           "Tienda",
            "sku":              "SKU",
            "ean":              "EAN",
            "ean_pos":          "EAN POS",
            "descripcion_hana": "Desc. HANA",
            "descripcion_pos":  "Desc. POS",
            "precio_hana":      "Precio HANA",
            "precio_pos":       "Precio POS",
            "restringido_pos":  "Restringido POS",
            "restringido_hana": "Restringido HANA",
            "surtido_hana":     "Surtido HANA",
            "fecha_ult_mov":    "Últ. Actualización",
            "jobidn":           "Job ID",
            "origen_precio":    "Origen Precio",
            "precio_ant":       "Precio Anterior",
            "fecha_carga_pos":  "Últ. Mod. POS",
            "diff_precio":      "Diff. Precio",
            "diff_restringido": "Diff. Restringido",
            "not_exist_pos":    "Solo en HANA",
            "tipo_diferencia":  "Tipo",
        }

        # ── Filtro tipo + descarga ─────────────────────────────
        col_filtro, col_info, col_dl = st.columns([4, 3, 1])

        with col_filtro:
            if "tipo_diferencia" in df_det.columns:
                tipos_disp = sorted(df_det["tipo_diferencia"].dropna().unique())
                default_sel = ["PRECIO"] if "PRECIO" in tipos_disp else tipos_disp
                tipos_sel = st.multiselect("Tipo de diferencia", tipos_disp, default=default_sel)
                df_vista = df_det[df_det["tipo_diferencia"].isin(tipos_sel)]
            else:
                df_vista = df_det

        csv = df_vista.to_csv(index=False).encode("utf-8")

        with col_info:
            st.caption(f"{len(df_vista):,} filas — usá el 🔍 de la grilla para buscar y las cabeceras para ordenar")

        with col_dl:
            st.download_button(
                "📥 Exportar CSV",
                data=csv,
                file_name=f"diffs_{tienda}.csv",
                mime="text/csv",
                use_container_width=True,
            )

        st.dataframe(
            df_vista.rename(columns=_col_labels),
            use_container_width=True,
            hide_index=True,
            height=620,
        )

        # ── Actualización por producto (solo gestor) ─────────
        st.markdown("---")

        if st.session_state.rol == "gestor":
            corregibles = df_det[
                df_det["tipo_diferencia"].isin(["PRECIO", "RESTRINGIDO"])
            ]
            with st.expander("⬆️ Actualizar un producto (por SKU)", expanded=False):
                if corregibles.empty:
                    st.caption("No hay diferencias de PRECIO/RESTRINGIDO corregibles en esta tienda.")
                else:
                    opciones = (
                        corregibles[["sku", "descripcion_hana", "tipo_diferencia"]]
                        .dropna(subset=["sku"])
                        .drop_duplicates("sku")
                        .sort_values("sku")
                        .reset_index(drop=True)
                    )
                    skus = opciones["sku"].astype(str).tolist()
                    etiquetas = []
                    for _, r in opciones.iterrows():
                        desc = str(r.get("descripcion_hana") or "")[:45]
                        etiquetas.append(f"{r['sku']} — {desc} ({r['tipo_diferencia']})")

                    sku_sel = st.selectbox(
                        "Producto a actualizar",
                        skus,
                        format_func=lambda s: etiquetas[skus.index(s)],
                    )
                    if st.button(
                        f"⬆️ Actualizar registro {sku_sel}",
                        type="primary",
                        use_container_width=True,
                    ):
                        with st.spinner(f"Actualizando SKU {sku_sel} desde el POS de {tienda} ({etiqueta_ambiente})..."):
                            res = actualizar_pos_staging_por_sku(tienda, sku_sel, ambiente_actual)
                        if res["ok"]:
                            st.success(
                                f"✅ SKU {sku_sel} actualizado en **{etiqueta_ambiente}** "
                                f"— {res['registros']} registro(s) en staging."
                            )
                            _cargar_detalle(tienda, ambiente_actual)
                            st.rerun()
                        else:
                            st.error(f"❌ Error actualizando SKU {sku_sel}: {res.get('error')}")
        else:
            st.caption("🔒 La actualización por producto la ejecuta un gestor.")


# -- VISTA RESUMEN -------------------------------------------

elif st.session_state.df_resumen is not None:
    df_res = st.session_state.df_resumen

    st.subheader("📊 Resumen de materiales activos — diferencias por tienda")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Tiendas monitoreadas", len(df_res))
    with col2:
        criticas = int((df_res.get("estado", pd.Series()) == "CRITICO").sum())
        st.metric("Tiendas CRITICAS 🔴", criticas)
    with col3:
        total_diffs = int(df_res.get("total_diffs", pd.Series(dtype=int)).sum())
        st.metric("Materiales con diferencias", f"{total_diffs:,}")

    st.markdown("---")

    if "total_diffs" in df_res.columns:
        fig = px.bar(
            df_res.sort_values("total_diffs", ascending=False),
            x="tienda",
            y="total_diffs",
            color="estado",
            color_discrete_map={
                "OK":      "#10b981",
                "ALERTA":  "#f59e0b",
                "CRITICO": "#ef4444",
                "ERROR":   "#6b7280",
            },
            title="Materiales con diferencias por tienda",
            labels={"tienda": "Tienda", "total_diffs": "Materiales con diferencias"},
        )
        fig.update_layout(xaxis_title="Tienda", yaxis_title="Materiales")
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    for _, row in df_res.iterrows():
        tienda_cod = row.get("tienda", "-")
        estado     = row.get("estado", "")
        diffs_prec  = int(row.get("cant_diffs_precio", 0))
        diffs_restr = int(row.get("cant_diffs_restringido", 0))
        solo_hana   = int(row.get("cant_solo_hana", 0))

        col_info, col_btn = st.columns([5, 1])
        with col_info:
            st.markdown(
                f"{_semaforo(estado)} **{tienda_cod}** &nbsp;|&nbsp; "
                f"Precio: {diffs_prec:,} &nbsp;|&nbsp; "
                f"Restringido: {diffs_restr:,} &nbsp;|&nbsp; "
                f"Solo HANA: {solo_hana:,}"
            )
        with col_btn:
            if st.button("Ver detalle →", key=f"det_{tienda_cod}", use_container_width=True):
                _cargar_detalle(tienda_cod, ambiente_actual)
                st.rerun()


# -- ESTADO INICIAL (error en carga) -------------------------

else:
    st.info("No se pudieron cargar los datos. Usa **🔄 Actualizar datos** para reintentar.")
