"""
Monitor POST vs HANA — Dashboard de Comparacion.
Ejecutar con: streamlit run src/app.py
"""

import math
import os

import streamlit as st
import pandas as pd
import plotly.express as px

from config import logger
from hana_queries import (
    get_resumen_tiendas,
    get_detalle_tienda,
    get_logs_staging,
    es_subarticulo,
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
    "df_logs": None,
    "ver_logs": False,
    "ver_subarticulos": False,
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
        "<h2>🔍 Monitor POST vs HANA</h2>"
        "<p style='color:#888'>Ingresá para continuar</p>"
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
            st.session_state.ver_subarticulos = False
            st.session_state.ver_logs = False
        except Exception as ex:
            st.error(f"Error cargando detalle de {tienda}: {ex}")
            logger.exception("Error en get_detalle_tienda")


def _cargar_logs():
    with st.spinner("Cargando logs de staging..."):
        try:
            st.session_state.df_logs = get_logs_staging()
        except Exception as ex:
            st.error(f"Error cargando logs: {ex}")
            logger.exception("Error en get_logs_staging")


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
st.sidebar.caption(f"👤 Sesión: **{st.session_state.rol}**")
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
elif st.session_state.tienda_seleccionada:
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
btn_staging = None
if st.session_state.rol == "gestor":
    btn_staging = st.sidebar.button(lbl_staging, use_container_width=True)
else:
    st.sidebar.caption("🔒 Solo lectura — la carga de staging la ejecuta un gestor.")

btn_logs = st.sidebar.button("📜 Ver logs de staging", use_container_width=True)


# ============================================================
# LOGICA DE BOTONES
# ============================================================

if btn_actualizar:
    if st.session_state.ver_logs:
        _cargar_logs()
    elif st.session_state.tienda_seleccionada:
        _cargar_detalle(st.session_state.tienda_seleccionada)
    else:
        _cargar_resumen()

if btn_logs:
    st.session_state.ver_logs = True
    _cargar_logs()

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

# -- VISTA LOGS DE STAGING -----------------------------------

if st.session_state.ver_logs:
    if st.session_state.df_logs is None:
        _cargar_logs()

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


# -- VISTA DETALLE -------------------------------------------

elif st.session_state.tienda_seleccionada and st.session_state.df_detalle is not None:
    tienda = st.session_state.tienda_seleccionada
    df_det = st.session_state.df_detalle

    # Subarticulos EAN (restringido_pos NULL que existen en POS) van a una vista aparte
    es_sub = es_subarticulo(df_det)
    df_principales = df_det[~es_sub]
    df_subarticulos = df_det[es_sub]

    st.subheader(f"📋 Tienda **{tienda}** — Detalle de diferencias")

    if df_det.empty:
        st.success("✅ No hay diferencias para esta tienda.")
    else:
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Total productos", f"{len(df_principales):,}")
        if "tipo_diferencia" in df_principales.columns:
            tipos = df_principales["tipo_diferencia"]
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
            if "tipo_diferencia" in df_principales.columns:
                tipos_disp = sorted(df_principales["tipo_diferencia"].dropna().unique())
                default_sel = ["PRECIO"] if "PRECIO" in tipos_disp else tipos_disp
                tipos_sel = st.multiselect("Tipo de diferencia", tipos_disp, default=default_sel)
                df_vista = df_principales[df_principales["tipo_diferencia"].isin(tipos_sel)]
            else:
                df_vista = df_principales

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
            corregibles = df_principales[
                df_principales["tipo_diferencia"].isin(["PRECIO", "RESTRINGIDO"])
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
                        with st.spinner(f"Actualizando SKU {sku_sel} desde el POS de {tienda}..."):
                            res = actualizar_pos_staging_por_sku(tienda, sku_sel)
                        if res["ok"]:
                            st.success(
                                f"✅ SKU {sku_sel} actualizado — {res['registros']} registro(s) en staging."
                            )
                            _cargar_detalle(tienda)
                            st.rerun()
                        else:
                            st.error(f"❌ Error actualizando SKU {sku_sel}: {res.get('error')}")
        else:
            st.caption("🔒 La actualización por producto la ejecuta un gestor.")

        # ── Subarticulos EANS ─────────────────────────────────
        st.markdown("---")

        if df_subarticulos.empty:
            st.caption("🔗 No hay subarticulos EANS (restringido POS NULL) para esta tienda.")
        else:
            lbl_sub = (
                "✖ Ocultar subarticulos EANS"
                if st.session_state.ver_subarticulos
                else f"🔗 Vista con todos los subarticulos EANS ({len(df_subarticulos):,})"
            )
            if st.button(lbl_sub, use_container_width=True):
                st.session_state.ver_subarticulos = not st.session_state.ver_subarticulos
                st.rerun()

            if st.session_state.ver_subarticulos:
                st.subheader("🧩 Subarticulos EANS (restringido POS NULL)")
                col_sub_info, col_sub_dl = st.columns([4, 1])
                with col_sub_info:
                    st.caption(
                        f"{len(df_subarticulos):,} filas — usá el 🔍 de la grilla para buscar y las cabeceras para ordenar"
                    )
                with col_sub_dl:
                    csv_sub = df_subarticulos.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        "📥 Exportar CSV",
                        data=csv_sub,
                        file_name=f"subarticulos_{tienda}.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )
                st.dataframe(
                    df_subarticulos.rename(columns=_col_labels),
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
