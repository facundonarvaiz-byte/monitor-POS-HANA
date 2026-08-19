"""
Monitor POST vs HANA — Dashboard de Comparacion.
Ejecutar con: streamlit run src/app.py
"""

import math

import streamlit as st
import pandas as pd
import plotly.express as px

from config import logger
from hana_queries import get_resumen_tiendas, get_detalle_tienda
from post_queries import populate_pos_staging, listar_tiendas_postgres


# ============================================================
# CONFIGURACION DE PAGINA
# ============================================================

st.set_page_config(
    page_title="Monitor POST vs HANA",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# ESTADO DE SESION
# ============================================================

defaults = {
    "tienda_seleccionada": None,
    "df_resumen": None,
    "df_detalle": None,
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


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


def _cargar_resumen():
    with st.spinner("Cargando resumen de tiendas..."):
        try:
            st.session_state.df_resumen = get_resumen_tiendas()
        except Exception as ex:
            st.error(f"Error cargando resumen: {ex}")
            logger.exception("Error en get_resumen_tiendas")


def _cargar_detalle(tienda: str):
    with st.spinner(f"Cargando detalle de {tienda}..."):
        try:
            st.session_state.df_detalle = get_detalle_tienda(tienda)
            st.session_state.tienda_seleccionada = tienda
            st.session_state.pagina_detalle = 1  # resetear paginación
        except Exception as ex:
            st.error(f"Error cargando detalle de {tienda}: {ex}")
            logger.exception("Error en get_detalle_tienda")


# ============================================================
# CARGA INICIAL (primera vez que abre la app)
# ============================================================

if st.session_state.df_resumen is None:
    _cargar_resumen()


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.title("🔍 Monitor POST vs HANA")
st.sidebar.caption("v2.0")

if st.session_state.tienda_seleccionada:
    if st.sidebar.button("← Volver al resumen", use_container_width=True):
        st.session_state.tienda_seleccionada = None
        st.session_state.df_detalle = None
        st.rerun()
    st.sidebar.markdown("---")

lbl_actualizar = (
    f"🔄 Actualizar {st.session_state.tienda_seleccionada}"
    if st.session_state.tienda_seleccionada
    else "🔄 Actualizar datos"
)
btn_actualizar = st.sidebar.button(lbl_actualizar, use_container_width=True, type="primary")

lbl_staging = (
    f"⬆️ Cargar Postgres ({st.session_state.tienda_seleccionada})"
    if st.session_state.tienda_seleccionada
    else "⬆️ Cargar Postgres (todas las tiendas)"
)
btn_staging = st.sidebar.button(lbl_staging, use_container_width=True)


# ============================================================
# LOGICA DE BOTONES
# ============================================================

if btn_actualizar:
    if st.session_state.tienda_seleccionada:
        _cargar_detalle(st.session_state.tienda_seleccionada)
    else:
        _cargar_resumen()

if btn_staging:
    if st.session_state.tienda_seleccionada:
        tiendas_pg = [st.session_state.tienda_seleccionada]
    else:
        tiendas_pg = listar_tiendas_postgres()

    if not tiendas_pg:
        st.sidebar.warning("⚠️ No hay tiendas configuradas en stores.json.")
    else:
        resultados = []
        barra = st.progress(0, text="Iniciando carga...")
        for i, t in enumerate(tiendas_pg):
            barra.progress(
                (i + 1) / len(tiendas_pg),
                text=f"Cargando {t}... ({i + 1}/{len(tiendas_pg)})",
            )
            resultados.append(populate_pos_staging(t))
        barra.empty()

        ok_count  = sum(1 for r in resultados if r["ok"])
        err_count = len(resultados) - ok_count
        total_reg = sum(r["registros"] for r in resultados)

        if err_count == 0:
            st.success(f"✅ {ok_count} tienda(s) cargadas — {total_reg:,} registros totales.")
        else:
            st.warning(f"⚠️ {ok_count} OK, {err_count} con errores — {total_reg:,} registros cargados.")

        with st.expander("Ver detalle del staging"):
            st.dataframe(
                pd.DataFrame(resultados)[["tienda", "registros", "duracion_ms", "ok"]],
                use_container_width=True,
                hide_index=True,
            )


# ============================================================
# CUERPO PRINCIPAL
# ============================================================

# -- VISTA DETALLE -------------------------------------------

if st.session_state.tienda_seleccionada and st.session_state.df_detalle is not None:
    tienda = st.session_state.tienda_seleccionada
    df_det = st.session_state.df_detalle

    st.subheader(f"📋 Tienda **{tienda}** — Detalle de diferencias")

    if df_det.empty:
        st.success("✅ No hay diferencias para esta tienda.")
    else:
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total productos", f"{len(df_det):,}")
        if "tipo_diferencia" in df_det.columns:
            tipos = df_det["tipo_diferencia"]
            with col2:
                st.metric("Diffs precio 💰", int((tipos == "PRECIO").sum()))
            with col3:
                st.metric("Diffs restringido 🚫", int((tipos == "RESTRINGIDO").sum()))
            with col4:
                st.metric("Solo en HANA", int((tipos == "SOLO_HANA").sum()))

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


# -- VISTA RESUMEN -------------------------------------------

elif st.session_state.df_resumen is not None:
    df_res = st.session_state.df_resumen

    st.subheader("📊 Resumen de diferencias por tienda")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Tiendas monitoreadas", len(df_res))
    with col2:
        criticas = int((df_res.get("estado", pd.Series()) == "CRITICO").sum())
        st.metric("Tiendas CRITICAS 🔴", criticas)
    with col3:
        total_diffs = int(df_res.get("total_diffs", pd.Series(dtype=int)).sum())
        st.metric("Diferencias totales", f"{total_diffs:,}")

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
            title="Diferencias totales por tienda",
            labels={"tienda": "Tienda", "total_diffs": "Total diferencias"},
        )
        fig.update_layout(xaxis_title="Tienda", yaxis_title="Diferencias")
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("---")

    for _, row in df_res.iterrows():
        tienda_cod = row.get("tienda", "-")
        estado     = row.get("estado", "")
        total      = int(row.get("total_diffs", 0))
        diffs_prec  = int(row.get("cant_diffs_precio", 0))
        diffs_restr = int(row.get("cant_diffs_restringido", 0))
        solo_hana   = int(row.get("cant_solo_hana", 0))

        col_info, col_btn = st.columns([5, 1])
        with col_info:
            st.markdown(
                f"{_semaforo(estado)} **{tienda_cod}** &nbsp;|&nbsp; "
                f"Total: **{total:,}** &nbsp;|&nbsp; "
                f"Precio: {diffs_prec:,} &nbsp;|&nbsp; "
                f"Restringido: {diffs_restr:,} &nbsp;|&nbsp; "
                f"Solo HANA: {solo_hana:,}"
            )
        with col_btn:
            if st.button("Ver detalle →", key=f"det_{tienda_cod}", use_container_width=True):
                _cargar_detalle(tienda_cod)
                st.rerun()


# -- ESTADO INICIAL (error en carga) -------------------------

else:
    st.info("No se pudieron cargar los datos. Usa **🔄 Actualizar datos** para reintentar.")
