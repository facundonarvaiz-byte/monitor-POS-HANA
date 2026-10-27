"""
Monitor POS vs HANA — Dashboard de Comparacion de materiales activos.
Ejecutar con: streamlit run src/app.py
"""

import math
import os
from datetime import datetime

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
    contar_materiales_push,
    push_from_monitor,
    estado_push,
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
    "push_estado": None,
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
# el CSV exporta el total (puede superar las 200.000 filas).
PREVIEW_EAN_FILAS = 20_000

# Umbral de referencia del gráfico de diferencias.
_UMBRAL_DIFS = 300

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


def _cargar_detalle_ean(tienda: str, ambiente: str):
    etiqueta = _etiqueta_ambiente(ambiente)
    with st.spinner(f"Cargando EANs Solo POS de {tienda} ({etiqueta})..."):
        try:
            st.session_state.df_detalle_ean = get_detalle_ean(tienda, "LO", ambiente)
            st.session_state.ean_tienda = tienda
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


# ============================================================
# PUSH DE CAMBIOS AL POS
# ============================================================

_PUSH_ESPERA_MAX_MIN = 90


def _push_elapsed_min(estado: dict) -> float:
    inicio = estado.get("inicio")
    if not inicio:
        return 0.0
    try:
        delta = datetime.now() - datetime.fromisoformat(inicio)
        return max(0.0, delta.total_seconds() / 60)
    except Exception:
        return 0.0


def _push_activo() -> dict | None:
    estado = st.session_state.get("push_estado")
    if estado and estado.get("estado") in ("pendiente", "actualizando", "demorado"):
        return estado
    return None


def _actualizar_post_push(estado: dict) -> bool:
    """Recarga POS_STAGING desde PostgreSQL y refresca la vista actual."""
    tienda = estado["tienda"]
    ambiente = estado["ambiente"]
    etiqueta = _etiqueta_ambiente(ambiente)
    try:
        with st.spinner(
            f"POS procesó el push. Actualizando POS_STAGING de {tienda} "
            f"desde PostgreSQL ({etiqueta})..."
        ):
            res = populate_pos_staging(tienda, ambiente)
    except Exception as ex:
        res = {"ok": False, "error": str(ex)}

    if not res.get("ok"):
        estado["estado"] = "error_staging"
        estado["mensaje"] = (
            f"El POS procesó el push, pero falló la actualización desde "
            f"PostgreSQL: {res.get('error')}"
        )
        st.session_state.push_estado = estado
        logger.error(
            "Push [%s] tienda=%s: error actualizando staging: %s",
            ambiente,
            tienda,
            res.get("error"),
        )
        return False

    estado["staging_registros"] = int(res.get("registros", 0))
    estado["demora_min"] = _push_elapsed_min(estado)
    estado["estado"] = "completado"
    estado["mensaje"] = ""
    st.session_state.push_estado = estado

    _resumen_cacheados.clear()
    _ean_resumen_cacheados.clear()

    if ambiente == st.session_state.ambiente:
        if st.session_state.ver_ean and st.session_state.ean_tienda == tienda:
            _cargar_detalle_ean(tienda, ambiente)
        elif st.session_state.tienda_seleccionada == tienda:
            _cargar_detalle(tienda, ambiente)
        st.session_state.df_resumen = None
        st.session_state.df_resumen_ean = None

    logger.info(
        "Push [%s] tienda=%s completado: staging=%d registros, demora=%.1f min",
        ambiente,
        tienda,
        estado["staging_registros"],
        estado["demora_min"],
    )
    return True


def _iniciar_push(tienda: str, ambiente: str):
    if _push_activo():
        st.error("Ya hay un push en curso. Esperá a que termine antes de iniciar otro.")
        return
    inicio_dt = datetime.now()
    estado_base = {
        "tienda": tienda,
        "ambiente": ambiente,
        "inicio": inicio_dt.isoformat(timespec="seconds"),
        "inicio_fecha": inicio_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "esperados": 0,
        "encolados": 0,
        "run_key": None,
        "run_floor": None,
        "sp_ms": None,
        "ultimo_chequeo": None,
        "pendientes": None,
        "procesados": None,
        "procesado_ts": None,
        "demora_min": None,
        "staging_registros": None,
        "mensaje": "",
    }

    try:
        with st.spinner(f"Consultando materiales a pushear de {tienda}..."):
            esperados = contar_materiales_push(tienda, ambiente)
    except Exception as ex:
        estado_base["estado"] = "error"
        estado_base["mensaje"] = f"No se pudo consultar materiales a pushear: {ex}"
        st.session_state.push_estado = estado_base
        logger.exception("Error contando materiales de push")
        return

    if esperados == 0:
        estado_base["estado"] = "sin_materiales"
        estado_base["mensaje"] = "No hay materiales activos para pushear."
        st.session_state.push_estado = estado_base
        return

    with st.spinner(f"Enviando push de {tienda} al POS..."):
        res = push_from_monitor(tienda, ambiente)

    if not res.get("ok"):
        estado_base["estado"] = "error"
        estado_base["mensaje"] = f"Error ejecutando el push: {res.get('error')}"
        st.session_state.push_estado = estado_base
        return

    estado_base.update({
        "estado": "pendiente",
        "esperados": esperados,
        "encolados": int(res.get("encolados", 0)),
        "run_key": res.get("run_key"),
        "run_floor": res.get("run_floor"),
        "sp_ms": res.get("sp_ms"),
    })
    st.session_state.push_estado = estado_base
    logger.info(
        "Push iniciado [%s] tienda=%s esperados=%d encolados=%d run=%s",
        ambiente,
        tienda,
        esperados,
        estado_base["encolados"],
        estado_base["run_key"],
    )


def _chequear_push(estado: dict, manual: bool = False) -> bool:
    """Consulta el estado del push y, si terminó, actualiza el staging."""
    if estado.get("estado") not in ("pendiente", "demorado"):
        return False

    try:
        res = estado_push(
            estado["tienda"],
            estado.get("run_key"),
            estado.get("run_floor"),
            estado["ambiente"],
        )
    except Exception as ex:
        estado["ultimo_chequeo"] = datetime.now().strftime("%H:%M:%S")
        estado["mensaje"] = f"Error consultando el estado del push: {ex}"
        st.session_state.push_estado = estado
        logger.exception("Error consultando estado_push")
        return False

    estado["ultimo_chequeo"] = datetime.now().strftime("%H:%M:%S")
    estado["pendientes"] = res.get("pendientes")
    estado["procesados"] = res.get("procesados")
    estado["procesado_ts"] = res.get("procesado_ts")
    estado["mensaje"] = ""

    if res.get("completado"):
        estado["estado"] = "actualizando"
        st.session_state.push_estado = estado
        return _actualizar_post_push(estado)

    if _push_elapsed_min(estado) > _PUSH_ESPERA_MAX_MIN:
        estado["estado"] = "demorado"
        estado["mensaje"] = (
            f"El POS todavía no confirmó el push después de "
            f"{_PUSH_ESPERA_MAX_MIN} min."
        )
    st.session_state.push_estado = estado
    return False


def _dibujar_estado_push(estado: dict):
    est = estado.get("estado")
    tienda = estado.get("tienda", "-")
    ambiente = _etiqueta_ambiente(estado.get("ambiente", "prod"))

    if est == "sin_materiales":
        st.info(f"No hay materiales activos para pushear en {tienda}.")
        return

    if est == "error":
        st.error(f"Push {tienda}: {estado.get('mensaje', 'Error desconocido')}")
        return

    if est == "error_staging":
        st.error(f"Push {tienda}: {estado.get('mensaje', 'Error desconocido')}")
        if st.button(
            "Reintentar actualización",
            key=f"push_retry_stg_{tienda}",
            use_container_width=True,
        ):
            if _actualizar_post_push(estado):
                st.rerun(scope="app")
        return

    if est == "actualizando":
        st.info(f"POS procesó el push de {tienda}. Actualizando staging...")
        return

    if est == "completado":
        demora = estado.get("demora_min") or 0
        registros = estado.get("staging_registros") or 0
        st.success(f"Push {tienda} aplicado (demora {demora:.0f} min).")
        st.caption(f"Ambiente: {ambiente}")
        st.caption(
            f"POS_STAGING actualizado desde PostgreSQL: {registros:,} registros."
        )
        return

    st.markdown(f"**Push {tienda}**")
    st.caption(f"Ambiente: {ambiente}")
    st.caption(
        f"Encolado: {estado.get('inicio_fecha', '-')} — "
        f"{int(estado.get('esperados', 0)):,} materiales"
    )
    if est == "demorado":
        st.warning(
            f"Sigue pendiente después de {_push_elapsed_min(estado):.0f} min. "
            "El POS puede demorar; podés verificar manualmente."
        )
    else:
        st.caption("Esperando procesamiento del POS... (demora típica 30–50 min)")

    if estado.get("ultimo_chequeo"):
        st.caption(f"Último chequeo: {estado['ultimo_chequeo']}")
    if estado.get("pendientes") is not None:
        st.caption(
            f"Pendientes: {int(estado.get('pendientes') or 0)} — "
            f"procesados: {int(estado.get('procesados') or 0)}"
        )
    if estado.get("mensaje"):
        st.warning(estado["mensaje"])

    if st.button(
        "Verificar ahora",
        key=f"push_check_{tienda}",
        use_container_width=True,
    ):
        _chequear_push(estado, manual=True)
        st.rerun(scope="app")

    if est == "demorado":
        if st.button(
            "Descartar estado",
            key=f"push_discard_{tienda}",
            use_container_width=True,
        ):
            st.session_state.push_estado = None
            st.rerun(scope="app")


@st.fragment(run_every=30)
def _panel_push_poll():
    estado = st.session_state.get("push_estado")
    if not estado or estado.get("estado") not in ("pendiente", "actualizando"):
        return

    if estado.get("estado") == "pendiente":
        _chequear_push(estado, manual=False)
        estado = st.session_state.get("push_estado")

    if not estado or estado.get("estado") not in ("pendiente", "actualizando"):
        st.rerun(scope="app")

    _dibujar_estado_push(estado)


def _render_push_ui():
    estado = st.session_state.get("push_estado")
    if not estado:
        return
    with st.sidebar:
        st.markdown("---")
        if estado.get("estado") in ("pendiente", "actualizando"):
            _panel_push_poll()
        else:
            _dibujar_estado_push(estado)


@st.dialog("Confirmar push a POS")
def _dialogo_confirmar_push(tienda: str, ambiente: str):
    st.markdown(
        f"Se enviarán al POS los cambios pendientes de la tienda **{tienda}**."
    )
    st.warning(
        "El POS puede tardar entre 30 y 50 minutos en reflejar los cambios. "
        "La app consultará el estado y actualizará los datos automáticamente."
    )
    col1, col2 = st.columns(2)
    with col1:
        if st.button(
            "Confirmar push",
            type="primary",
            use_container_width=True,
            key="push_confirm_ok",
        ):
            _iniciar_push(tienda, ambiente)
            st.rerun()
    with col2:
        if st.button(
            "Cancelar",
            use_container_width=True,
            key="push_confirm_cancel",
        ):
            st.rerun()



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
    st.session_state.push_estado = None
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
tienda_staging = (
    st.session_state.ean_tienda
    if st.session_state.ver_ean
    else st.session_state.tienda_seleccionada
)
lbl_staging = (
    f"⬆️ Cargar Postgres ({tienda_staging}){_sufijo_test}"
    if tienda_staging
    else f"⬆️ Cargar Postgres (todas las tiendas){_sufijo_test}"
)
btn_staging = None
if st.session_state.rol == "gestor":
    btn_staging = st.sidebar.button(lbl_staging, use_container_width=True)
else:
    st.sidebar.caption("🔒 Solo lectura — la carga de staging la ejecuta un gestor.")

btn_push = None
_push_en_curso = _push_activo()
if st.session_state.rol == "gestor" and tienda_staging:
    btn_push = st.sidebar.button(
        f"Push cambios ({tienda_staging})",
        use_container_width=True,
        disabled=_push_en_curso is not None,
    )
    if _push_en_curso and _push_en_curso.get("tienda") != tienda_staging:
        st.sidebar.caption(f"Push en curso: {_push_en_curso['tienda']}")

btn_logs = st.sidebar.button("📜 Ver logs de staging", use_container_width=True)
btn_cp_logs = st.sidebar.button("🧾 Ver logs CP_CVE", use_container_width=True)
btn_ean = st.sidebar.button("🔀 POS vs HANA x EAN", use_container_width=True)

_render_push_ui()


# ============================================================
# LOGICA DE BOTONES
# ============================================================

ambiente_actual = st.session_state.ambiente
etiqueta_ambiente = _etiqueta_ambiente(ambiente_actual)

if btn_push:
    _dialogo_confirmar_push(tienda_staging, ambiente_actual)

if btn_actualizar:
    if st.session_state.ver_logs:
        _cargar_logs(ambiente_actual)
    elif st.session_state.ver_cp_logs:
        _cargar_cp_logs(ambiente_actual)
    elif st.session_state.ver_ean:
        if st.session_state.ean_tienda:
            _cargar_detalle_ean(st.session_state.ean_tienda, ambiente_actual)
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
    if tienda_staging:
        tiendas_pg = [tienda_staging]
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

        if df_ean.empty:
            st.success("✅ No hay EANs Solo POS para esta tienda.")
        else:
            st.metric("EANs Solo POS", f"{len(df_ean):,}")

            st.markdown("---")

            # ── Filtro tipo (si aplica) + descarga ─────────────
            tipos_disp = sorted(df_ean["tipo"].dropna().unique())
            if len(tipos_disp) > 1:
                col_filtro, col_info, col_dl = st.columns([3, 4, 1])
                with col_filtro:
                    tipos_sel = st.multiselect("Tipo", tipos_disp, default=tipos_disp)
                    df_vista = df_ean[df_ean["tipo"].isin(tipos_sel)]
            else:
                col_info, col_dl = st.columns([7, 1])
                df_vista = df_ean

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
                    file_name=f"ean_{tienda_ean}.csv",
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

            fig_ean = px.bar(
                df_res_ean.sort_values("solo_pos", ascending=False),
                x="tienda",
                y="solo_pos",
                title="EANs Solo POS por tienda",
                labels={"tienda": "Tienda", "solo_pos": "EANs Solo POS"},
                color_discrete_sequence=["#f59e0b"],
            )
            fig_ean.update_layout(
                xaxis_title="Tienda",
                yaxis_title="EANs Solo POS",
                showlegend=False,
            )
            st.plotly_chart(fig_ean, use_container_width=True)

            st.markdown("---")

            for _, row in df_res_ean.iterrows():
                tienda_cod = row.get("tienda", "-")
                estado_ean = row.get("estado", "OK")

                col_info, col_btn = st.columns([5, 1])
                with col_info:
                    st.markdown(
                        f"{_semaforo(estado_ean)} **{tienda_cod}** &nbsp;|&nbsp; "
                        f"Solo POS: {int(row.get('solo_pos', 0)):,}"
                    )
                with col_btn:
                    if st.button(
                        "Ver detalle →",
                        key=f"ean_det_{tienda_cod}",
                        use_container_width=True,
                    ):
                        _cargar_detalle_ean(tienda_cod, ambiente_actual)
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

    if not df_res.empty and "total_diffs" in df_res.columns:
        df_plot = df_res.sort_values("total_diffs", ascending=False).copy()
        max_diffs = int(df_plot["total_diffs"].max())
        supera_umbral = max_diffs > _UMBRAL_DIFS
        df_plot["etiqueta"] = df_plot["total_diffs"].apply(
            lambda v: "" if v == 0 else f"{int(v):,}"
        )

        fig = px.bar(
            df_plot,
            x="tienda",
            y="total_diffs",
            color="estado",
            custom_data=["total_diffs"],
            text="etiqueta",
            color_discrete_map={
                "OK":      "#10b981",
                "ALERTA":  "#f59e0b",
                "CRITICO": "#ef4444",
                "ERROR":   "#6b7280",
            },
            title="Materiales con diferencias por tienda",
            labels={"tienda": "Tienda", "total_diffs": "Materiales con diferencias"},
        )
        fig.update_traces(
            textposition="outside",
            cliponaxis=False,
            hovertemplate=(
                "%{x}<br>Materiales con diferencias: "
                "%{customdata[0]:,}<extra></extra>"
            ),
        )
        fig.update_layout(xaxis_title="Tienda", yaxis_title="Materiales")

        if supera_umbral:
            fig.add_hline(
                y=_UMBRAL_DIFS,
                line_dash="dash",
                line_color="#6b7280",
                annotation_text=f"Umbral {_UMBRAL_DIFS}",
                annotation_position="top right",
            )
            fig.update_yaxes(range=[0, max_diffs * 1.1])
            st.caption(
                f"Escala automática (máximo {max_diffs:,}). "
                f"Línea punteada = umbral {_UMBRAL_DIFS}."
            )
        else:
            fig.update_yaxes(range=[0, _UMBRAL_DIFS])
            st.caption(f"Escala fija 0–{_UMBRAL_DIFS}.")

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
