"""
daily_email.py - Correo diario con el resumen de diferencias por tienda.

Ejecutar manualmente:
    venv/Scripts/python scripts/daily_email.py --dry-run
    venv/Scripts/python scripts/daily_email.py --to mi.correo@easy.com.ar

En el servidor lo ejecuta el cron de lunes a viernes a las 08:40 (hora local),
después de la carga diaria de POS_STAGING de las 08:10.

Los destinatarios se leen de la vista HANA compartida
`Z_NCRCO.Log_views.Dashboard_Mail/Log_ReportMailer_Dashboard_DEST`
(columna DESTINATION). Si la vista no devuelve direcciones, el correo no
se envía. --to permite una prueba puntual.

El correo se envía a través de un endpoint XSJS de HANA que usa $.net.Mail
(ver xsjs/send_monitor_email.xsjs). La configuración se lee del .env:
    EMAIL_XSJS_URL, EMAIL_XSJS_VERIFY_TLS
La autenticación del XSJS usa HANA_USER/HANA_PASSWORD y el remitente es el
mismo que valida el XSJS (errores.ncr@easy.com.ar).
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import math
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from html import escape
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

# Asegurar que src/ esté en el path (mismo patrón que daily_staging.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config import logger  # noqa: E402
from hana_queries import (  # noqa: E402
    get_destinatarios_email,
    get_logs_staging,
    get_resumen_tiendas,
)

# La carga diaria y el correo son SIEMPRE producción.
AMBIENTE = "prod"

# Mismo umbral que usa la app en el gráfico de resumen.
UMBRAL_DIFS = 300

# Remitente fijo: el XSJS solo permite esta dirección.
REMITENTE = "errores.ncr@easy.com.ar"

DASHBOARD_URL = "http://g100603aws079/monitor"

EMAIL_TIMEOUT = 120

_CID_GRAFICO = "monitor_resumen"
_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

_COLORES_ESTADO = {
    "OK": "#10b981",
    "ALERTA": "#f59e0b",
    "CRITICO": "#ef4444",
    "ERROR": "#6b7280",
}

_TEXTO_ESTADO = {
    "OK": "#ffffff",
    "ALERTA": "#111827",
    "CRITICO": "#ffffff",
    "ERROR": "#ffffff",
}

_ETIQUETAS_ESTADO = {
    "OK": "OK",
    "ALERTA": "ALERTA",
    "CRITICO": "CRÍTICO",
    "ERROR": "ERROR",
}


# ============================================================
# UTILIDADES
# ============================================================

def _parse_lista(valor: str) -> list[str]:
    """Separa direcciones por coma o punto y coma y descarta vacías."""
    if not valor:
        return []
    partes = valor.replace(";", ",").split(",")
    return [p.strip() for p in partes if p.strip()]


def _bool_env(nombre: str, default: bool) -> bool:
    valor = os.getenv(nombre, "").strip().lower()
    if not valor:
        return default
    return valor in ("1", "true", "si", "sí", "yes", "s")


def _fmt_num(valor) -> str:
    """Formatea un conteo con separador de miles; '—' si no es numérico."""
    try:
        if pd.isna(valor):
            return "—"
        return f"{int(float(valor)):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_fecha_carga(valor) -> str:
    """Normaliza la última carga POS a dd/mm HH:MM."""
    if valor is None or valor == "":
        return "—"
    try:
        if pd.isna(valor):
            return "—"
        fecha = pd.to_datetime(valor, errors="coerce")
        if pd.isna(fecha):
            return "—"
        return fecha.strftime("%d/%m %H:%M")
    except Exception:
        return "—"


def _leer_config_envio() -> dict:
    """
    Lee la configuración mínima del envío.

    Los destinatarios salen de la vista HANA (ver
    `_resolver_destinatarios`); el remitente es fijo y la autenticación del
    XSJS usa las mismas credenciales HANA del monitor.
    """
    return {
        "url": os.getenv("EMAIL_XSJS_URL", "").strip(),
        "usuario": os.getenv("HANA_USER", "").strip(),
        "clave": os.getenv("HANA_PASSWORD", ""),
        "verificar_tls": _bool_env("EMAIL_XSJS_VERIFY_TLS", True),
        "timeout": EMAIL_TIMEOUT,
    }


def _validar_config(cfg: dict) -> bool:
    """Valida la configuración mínima antes de consultar HANA."""
    if not cfg["to"]:
        logger.error(
            "No hay destinatarios: la vista HANA no devolvió direcciones "
            "(o usá --to para una prueba). No se envía el correo."
        )
        return False
    if not cfg["url"]:
        logger.error("Falta EMAIL_XSJS_URL en .env")
        return False
    if cfg["url"].lower().startswith("https") and not cfg["verificar_tls"]:
        logger.warning(
            "EMAIL_XSJS_VERIFY_TLS=0: no se validará el certificado TLS del XSJS."
        )
    return True


def _resolver_destinatarios(override: str | None) -> list[str]:
    """
    Resuelve los destinatarios del correo.

    Prioridad:
      1. --to (prueba puntual)
      2. Vista HANA compartida (`Dashboard_Mail/DESTINATION`)

    Si la vista falla o no devuelve direcciones, devuelve [] y el correo
    no se envía.
    """
    if override:
        destinatarios = _parse_lista(override)
        logger.info(
            "Correo diario: %d destinatario(s) desde --to",
            len(destinatarios),
        )
        return destinatarios

    try:
        destinatarios = get_destinatarios_email(AMBIENTE)
    except Exception:
        logger.exception(
            "Correo diario: no se pudo leer la vista de destinatarios; "
            "no se envía el correo"
        )
        return []

    if destinatarios:
        logger.info(
            "Correo diario: %d destinatario(s) desde la vista HANA",
            len(destinatarios),
        )
        return destinatarios

    logger.error(
        "Correo diario: la vista HANA de destinatarios no devolvió "
        "direcciones; no se envía el correo"
    )
    return []


def _kpis(df: pd.DataFrame) -> dict:
    estados = df["estado"].astype(str) if "estado" in df.columns else pd.Series(dtype=str)
    total_diffs = (
        int(df["total_diffs"].fillna(0).sum())
        if "total_diffs" in df.columns
        else 0
    )
    return {
        "total_tiendas": len(df),
        "ok": int((estados == "OK").sum()),
        "alerta": int((estados == "ALERTA").sum()),
        "critico": int((estados == "CRITICO").sum()),
        "error": int((estados == "ERROR").sum()),
        "total_diffs": total_diffs,
    }


def _estado_global(df: pd.DataFrame) -> str:
    if df.empty:
        return "ERROR"
    estados = set(df["estado"].astype(str)) if "estado" in df.columns else set()
    if "CRITICO" in estados:
        return "CRITICO"
    if "ALERTA" in estados:
        return "ALERTA"
    if "ERROR" in estados:
        return "ERROR"
    return "OK"


def _asunto(df: pd.DataFrame, estado_global: str) -> str:
    fecha = datetime.now().strftime("%d/%m/%Y")
    etiqueta = _ETIQUETAS_ESTADO.get(estado_global, estado_global)
    errores = int((df["estado"] == "ERROR").sum()) if "estado" in df.columns else 0
    sufijo = f" ({errores} sin datos)" if errores else ""
    return (
        f"[{etiqueta}] Monitor POS vs HANA — Diferencias por tienda — "
        f"{fecha}{sufijo}"
    )


def _info_staging() -> dict | None:
    """
    Lee POS_STAGING_LOG para mostrar la última carga y las tiendas del día.

    `POS_STAGING_LOG` tiene una fila por carga completa de tienda y también por
    actualización de un SKU; por eso se informa la última fila y la cantidad de
    tiendas distintas cargadas hoy, no la cantidad de filas.

    No es crítico: si falla, el correo se envía igual (sin la línea de staging).
    """
    try:
        df = get_logs_staging(AMBIENTE, limite=1000)
    except Exception:
        logger.exception("Correo diario: no se pudo leer POS_STAGING_LOG")
        return None

    if df is None or df.empty:
        return None

    df = df.copy()
    df["_inicio"] = pd.to_datetime(df["inicio"], errors="coerce")
    df["_fin"] = pd.to_datetime(df["fin"], errors="coerce")
    df["_cuando"] = df["_fin"].fillna(df["_inicio"])

    hoy = datetime.now().date()
    df_hoy = df[df["_inicio"].dt.date == hoy].copy()

    if df_hoy.empty:
        df_valido = df.dropna(subset=["_cuando"])
        if df_valido.empty:
            return None
        fila = df_valido.loc[df_valido["_cuando"].idxmax()]
        return {
            "ultima": fila["_cuando"],
            "ultima_tienda": str(fila.get("tienda", "-")),
            "es_hoy": False,
            "tiendas_hoy": 0,
            "error_hoy": 0,
        }

    df_hoy = df_hoy.dropna(subset=["_cuando"])
    if df_hoy.empty:
        return None

    fila = df_hoy.loc[df_hoy["_cuando"].idxmax()]
    return {
        "ultima": fila["_cuando"],
        "ultima_tienda": str(fila.get("tienda", "-")),
        "es_hoy": True,
        "tiendas_hoy": int(df_hoy["tienda"].nunique()),
        "error_hoy": int(
            df_hoy.loc[df_hoy["estado"] == "ERROR", "tienda"].nunique()
        ),
    }


def _texto_staging(info: dict | None) -> str:
    """Arma la línea de frescura del staging para el correo."""
    if not info:
        return ""
    ultima = info.get("ultima")
    if ultima is None or pd.isna(ultima):
        return ""

    fecha = ultima.strftime("%d/%m/%Y %H:%M")
    tienda = info.get("ultima_tienda", "-")
    if not info.get("es_hoy"):
        return (
            f"Sin cargas de staging hoy "
            f"(última: {tienda} — {fecha})."
        )

    tiendas = int(info.get("tiendas_hoy", 0))
    errores = int(info.get("error_hoy", 0))
    texto = (
        f"Última carga de staging: {tienda} — {fecha}. "
        f"Tiendas cargadas hoy: {tiendas}"
    )
    if errores:
        texto += f" ({errores} con ERROR)"
    return texto + "."


# ============================================================
# GRÁFICO PNG (Pillow, incluido con Streamlit)
# ============================================================

def _cargar_fuente(tamano: int, negrita: bool = False) -> ImageFont.ImageFont:
    rutas = [
        r"C:\Windows\Fonts\arialbd.ttf" if negrita else r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeuib.ttf" if negrita else r"C:\Windows\Fonts\segoeui.ttf",
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if negrita
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
        (
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"
            if negrita
            else "/usr/share/fonts/dejavu/DejaVuSans.ttf"
        ),
    ]
    for ruta in rutas:
        try:
            return ImageFont.truetype(ruta, tamano)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=tamano)
    except TypeError:
        return ImageFont.load_default()


def _bbox(draw: ImageDraw.ImageDraw, texto: str, fuente) -> tuple[int, int, int, int]:
    try:
        return draw.textbbox((0, 0), texto, font=fuente)
    except AttributeError:
        # Compatibilidad con Pillow < 8.
        ancho, alto = draw.textsize(texto, font=fuente)
        return (0, 0, ancho, alto)


def _texto_centrado(draw, x, y, texto, fuente, color):
    bbox = _bbox(draw, texto, fuente)
    ancho = bbox[2] - bbox[0]
    alto = bbox[3] - bbox[1]
    draw.text(
        (x - ancho / 2 - bbox[0], y - alto / 2 - bbox[1]),
        texto,
        font=fuente,
        fill=color,
    )


def _texto_derecha(draw, x, y, texto, fuente, color):
    bbox = _bbox(draw, texto, fuente)
    ancho = bbox[2] - bbox[0]
    alto = bbox[3] - bbox[1]
    draw.text(
        (x - ancho - bbox[0], y - alto / 2 - bbox[1]),
        texto,
        font=fuente,
        fill=color,
    )


def _texto_izquierda(draw, x, y, texto, fuente, color):
    bbox = _bbox(draw, texto, fuente)
    alto = bbox[3] - bbox[1]
    draw.text(
        (x - bbox[0], y - alto / 2 - bbox[1]),
        texto,
        font=fuente,
        fill=color,
    )


def _ticks_y(max_diffs: int) -> tuple[float, list[int]]:
    """Calcula el máximo del eje Y y ticks legibles."""
    if max_diffs <= UMBRAL_DIFS:
        y_max = UMBRAL_DIFS * 1.10
        return y_max, list(range(0, UMBRAL_DIFS + 1, 50))

    y_max = max_diffs * 1.15
    bruto = y_max / 6
    magnitud = 10 ** int(math.floor(math.log10(bruto))) if bruto > 0 else 1
    paso = magnitud
    for factor in (1, 2, 2.5, 5, 10):
        candidato = factor * magnitud
        if candidato * 6 >= y_max:
            paso = candidato
            break
    ticks = [0]
    valor = paso
    while valor <= y_max:
        ticks.append(int(valor))
        valor += paso
    return y_max, ticks


def _generar_grafico(df: pd.DataFrame, ruta: Path) -> bytes:
    """Genera el gráfico de barras por tienda y devuelve los bytes PNG."""
    ancho = 1100
    alto = 620
    margen_izq = 85
    margen_der = 45
    margen_sup = 130
    margen_inf = 110

    fondo = "#ffffff"
    color_texto = "#111827"
    color_subtitulo = "#6b7280"
    color_grilla = "#e5e7eb"
    color_eje = "#9ca3af"

    imagen = Image.new("RGB", (ancho, alto), fondo)
    draw = ImageDraw.Draw(imagen)

    fuente_titulo = _cargar_fuente(20, negrita=True)
    fuente_subtitulo = _cargar_fuente(12)
    fuente_eje = _cargar_fuente(11)
    fuente_valor = _cargar_fuente(11, negrita=True)
    fuente_tienda = _cargar_fuente(11 if len(df) <= 20 else 9, negrita=True)
    fuente_leyenda = _cargar_fuente(11)

    fecha = datetime.now().strftime("%d/%m/%Y")
    _texto_centrado(
        draw,
        ancho / 2,
        28,
        "Materiales con diferencias por tienda",
        fuente_titulo,
        color_texto,
    )
    _texto_centrado(
        draw,
        ancho / 2,
        56,
        f"SKUs distintos con diferencias de precio, restringido o solo HANA — {fecha}",
        fuente_subtitulo,
        color_subtitulo,
    )

    # Leyenda de estados.
    x_leyenda = margen_izq
    y_leyenda = 88
    for estado in ("OK", "ALERTA", "CRITICO", "ERROR"):
        color = _COLORES_ESTADO[estado]
        draw.rectangle(
            [x_leyenda, y_leyenda - 5, x_leyenda + 10, y_leyenda + 5],
            fill=color,
        )
        etiqueta = _ETIQUETAS_ESTADO[estado]
        _texto_izquierda(
            draw,
            x_leyenda + 15,
            y_leyenda,
            etiqueta,
            fuente_leyenda,
            color_texto,
        )
        bbox = _bbox(draw, etiqueta, fuente_leyenda)
        x_leyenda += 15 + (bbox[2] - bbox[0]) + 22

    area_ancho = ancho - margen_izq - margen_der
    area_alto = alto - margen_sup - margen_inf
    y_base = alto - margen_inf

    max_diffs = (
        int(df["total_diffs"].fillna(0).max()) if not df.empty else 0
    )
    y_max, ticks = _ticks_y(max_diffs)

    # Grilla y etiquetas del eje Y.
    for tick in ticks:
        y = y_base - (tick / y_max) * area_alto
        draw.line(
            [(margen_izq, y), (ancho - margen_der, y)],
            fill=color_grilla,
            width=1,
        )
        _texto_derecha(
            draw,
            margen_izq - 10,
            y,
            f"{tick:,}",
            fuente_eje,
            color_subtitulo,
        )

    draw.line(
        [(margen_izq, y_base), (ancho - margen_der, y_base)],
        fill=color_eje,
        width=1,
    )

    # Línea de umbral.
    if UMBRAL_DIFS <= y_max:
        y_umbral = y_base - (UMBRAL_DIFS / y_max) * area_alto
        x_ini = margen_izq
        while x_ini < ancho - margen_der:
            x_fin = min(x_ini + 7, ancho - margen_der)
            draw.line(
                [(x_ini, y_umbral), (x_fin, y_umbral)],
                fill="#6b7280",
                width=2,
            )
            x_ini += 12
        _texto_derecha(
            draw,
            ancho - margen_der - 4,
            y_umbral - 12,
            f"Umbral {UMBRAL_DIFS}",
            fuente_eje,
            "#4b5563",
        )

    # Barras.
    cantidad = max(1, len(df))
    espacio = area_ancho / cantidad
    ancho_barra = max(8.0, min(58.0, espacio * 0.58))

    for indice, (_, fila) in enumerate(df.iterrows()):
        valor = int(fila.get("total_diffs", 0) or 0)
        estado = str(fila.get("estado", ""))
        color = _COLORES_ESTADO.get(estado, "#6b7280")

        x_centro = margen_izq + espacio * (indice + 0.5)
        altura = (min(valor, y_max) / y_max) * area_alto
        x0 = x_centro - ancho_barra / 2
        x1 = x_centro + ancho_barra / 2
        y0 = y_base - altura

        if valor > 0:
            draw.rectangle([x0, y0, x1, y_base], fill=color)
            _texto_centrado(
                draw,
                x_centro,
                y0 - 12,
                f"{valor:,}",
                fuente_valor,
                color_texto,
            )
        elif estado == "ERROR":
            # Marca mínima para tiendas sin datos.
            draw.rectangle([x0, y_base - 3, x1, y_base], fill=color)

        tienda = str(fila.get("tienda", "-"))
        _texto_centrado(
            draw,
            x_centro,
            y_base + 20,
            tienda,
            fuente_tienda,
            color_texto,
        )

    imagen.save(ruta, format="PNG", optimize=True)
    return ruta.read_bytes()


# ============================================================
# HTML / TEXTO
# ============================================================

def _tarjeta_kpi(titulo: str, valor: str, color: str) -> str:
    return (
        '<div style="border:1px solid #e5e7eb;border-radius:6px;padding:10px;'
        'text-align:center;background-color:#ffffff;">'
        f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase;'
        f'letter-spacing:0.5px;">{escape(str(titulo))}</div>'
        f'<div style="font-size:24px;font-weight:bold;color:{color};">'
        f'{escape(str(valor))}</div>'
        "</div>"
    )


def _tabla_kpis(k: dict) -> str:
    filas = [
        [
            _tarjeta_kpi("Tiendas", k["total_tiendas"], "#111827"),
            _tarjeta_kpi("OK", k["ok"], "#059669"),
            _tarjeta_kpi("ALERTA", k["alerta"], "#d97706"),
        ],
        [
            _tarjeta_kpi("CRÍTICO", k["critico"], "#dc2626"),
            _tarjeta_kpi("Sin datos", k["error"], "#6b7280"),
            _tarjeta_kpi("Total diferencias", f"{k['total_diffs']:,}", "#111827"),
        ],
    ]
    html_filas = []
    for fila in filas:
        celdas = []
        for tarjeta in fila:
            celdas.append(
                f'<td style="padding:4px;width:33.33%;">{tarjeta}</td>'
            )
        html_filas.append("<tr>" + "".join(celdas) + "</tr>")
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;margin:16px 0 4px 0;">'
        + "".join(html_filas)
        + "</table>"
    )


def _tabla_tiendas(df: pd.DataFrame) -> str:
    encabezados = [
        ("Tienda", "left"),
        ("Estado", "center"),
        ("Precio", "right"),
        ("Restringido", "right"),
        ("Solo HANA", "right"),
        ("Total", "right"),
        ("Últ. mod. POS", "right"),
    ]
    cabecera = "".join(
        f'<th style="padding:8px;background-color:#111827;color:#ffffff;'
        f'text-align:{alineacion};font-size:12px;font-weight:bold;'
        f'white-space:nowrap;">{escape(titulo)}</th>'
        for titulo, alineacion in encabezados
    )

    filas = []
    for indice, (_, fila) in enumerate(df.iterrows()):
        estado_raw = str(fila.get("estado", ""))
        estado = _ETIQUETAS_ESTADO.get(estado_raw, estado_raw)
        color_estado = _COLORES_ESTADO.get(estado_raw, "#6b7280")
        color_texto_estado = _TEXTO_ESTADO.get(estado_raw, "#ffffff")
        fondo = "#ffffff" if indice % 2 == 0 else "#f9fafb"

        if estado_raw == "ERROR":
            valores = ["—", "—", "—", "—", "—"]
        else:
            valores = [
                _fmt_num(fila.get("cant_diffs_precio")),
                _fmt_num(fila.get("cant_diffs_restringido")),
                _fmt_num(fila.get("cant_solo_hana")),
                _fmt_num(fila.get("total_diffs")),
                _fmt_fecha_carga(fila.get("ultima_carga_pos")),
            ]

        celdas = [
            f'<td style="padding:7px 8px;border-bottom:1px solid #e5e7eb;'
            f'background-color:{fondo};font-weight:bold;color:#111827;'
            f'font-size:12px;white-space:nowrap;">'
            f'{escape(str(fila.get("tienda", "-")))}</td>',
            f'<td style="padding:7px 8px;border-bottom:1px solid #e5e7eb;'
            f'background-color:{color_estado};color:{color_texto_estado};'
            f'text-align:center;font-weight:bold;font-size:11px;'
            f'white-space:nowrap;">{escape(estado)}</td>',
            f'<td style="padding:7px 8px;border-bottom:1px solid #e5e7eb;'
            f'background-color:{fondo};text-align:right;font-size:12px;'
            f'color:#111827;">{valores[0]}</td>',
            f'<td style="padding:7px 8px;border-bottom:1px solid #e5e7eb;'
            f'background-color:{fondo};text-align:right;font-size:12px;'
            f'color:#111827;">{valores[1]}</td>',
            f'<td style="padding:7px 8px;border-bottom:1px solid #e5e7eb;'
            f'background-color:{fondo};text-align:right;font-size:12px;'
            f'color:#111827;">{valores[2]}</td>',
            f'<td style="padding:7px 8px;border-bottom:1px solid #e5e7eb;'
            f'background-color:{fondo};text-align:right;font-size:12px;'
            f'color:#111827;font-weight:bold;">{valores[3]}</td>',
            f'<td style="padding:7px 8px;border-bottom:1px solid #e5e7eb;'
            f'background-color:{fondo};text-align:right;font-size:12px;'
            f'color:#6b7280;white-space:nowrap;">{valores[4]}</td>',
        ]
        filas.append("<tr>" + "".join(celdas) + "</tr>")

    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;margin-top:12px;">'
        f"<thead><tr>{cabecera}</tr></thead>"
        f"<tbody>{''.join(filas)}</tbody>"
        "</table>"
    )


def _generar_html(
    df: pd.DataFrame,
    incluir_grafico: bool = True,
    info_staging: dict | None = None,
) -> str:
    k = _kpis(df)
    fecha_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
    fecha_corta = datetime.now().strftime("%d/%m/%Y")

    linea_staging = _texto_staging(info_staging)
    if linea_staging:
        staging_html = (
            '<p style="font-size:12px;color:#6b7280;margin:0 0 12px 0;">'
            f"{escape(linea_staging)}</p>"
        )
    else:
        staging_html = ""

    if k["error"]:
        aviso = (
            '<div style="background-color:#fef3c7;border-left:4px solid #f59e0b;'
            'padding:10px 14px;margin:0 0 16px 0;font-size:13px;color:#92400e;">'
            f'<strong>Atención:</strong> {k["error"]} tienda(s) no pudieron '
            "consultarse. Los totales pueden estar incompletos.</div>"
        )
    else:
        aviso = ""

    if incluir_grafico:
        grafico = (
            '<div style="margin:22px 0 10px 0;">'
            f'<img src="cid:{_CID_GRAFICO}" '
            'alt="Materiales con diferencias por tienda" width="1000" '
            'style="width:100%;max-width:1000px;height:auto;'
            'border:1px solid #e5e7eb;border-radius:6px;'
            'background-color:#ffffff;">'
            "</div>"
        )
    else:
        grafico = (
            '<p style="font-size:13px;color:#b45309;margin:18px 0;">'
            "No se pudo generar el gráfico para este envío.</p>"
        )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Monitor POS vs HANA — Materiales activos</title>
</head>
<body style="margin:0;padding:0;background-color:#f3f4f6;">
<div style="font-family:Arial,Helvetica,sans-serif;max-width:1000px;margin:0 auto;padding:20px;color:#111827;">
  <div style="background-color:#111827;color:#ffffff;padding:18px 22px;border-radius:8px 8px 0 0;">
    <div style="font-size:20px;font-weight:bold;">Monitor POS vs HANA — Materiales activos</div>
    <div style="font-size:13px;color:#d1d5db;margin-top:4px;">Diferencias de tiendas en materiales activos · {fecha_corta}</div>
  </div>
  <div style="background-color:#ffffff;padding:20px 22px;border:1px solid #e5e7eb;border-top:none;">
    {aviso}
    {staging_html}
    {_tabla_kpis(k)}
    {grafico}
    {_tabla_tiendas(df)}
    <p style="margin:18px 0 0 0;font-size:13px;">
      <a href="{escape(DASHBOARD_URL)}" style="color:#2563eb;text-decoration:none;font-weight:bold;">Abrir dashboard del monitor</a>
    </p>
    <p style="margin:14px 0 0 0;font-size:11px;color:#6b7280;line-height:1.5;">
      Umbrales: OK &le; 50 · ALERTA &le; 300 · CRÍTICO &gt; 300.
      Total = SKUs distintos con al menos una diferencia (precio, restringido o solo HANA).<br>
      Datos: POS_STAGING (PostgreSQL → HANA) del ambiente Producción.
      Generado automáticamente el {fecha_hora}.
    </p>
  </div>
</div>
</body>
</html>"""


def _generar_texto(
    df: pd.DataFrame,
    info_staging: dict | None = None,
) -> str:
    k = _kpis(df)
    fecha_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
    linea_staging = _texto_staging(info_staging)
    lineas = [
        "Monitor POS vs HANA — Materiales activos",
        f"Diferencias de tiendas en materiales activos — {fecha_hora}",
        "",
    ]
    if linea_staging:
        lineas.append(linea_staging)
    lineas.extend(
        [
            (
                f"Tiendas: {k['total_tiendas']} | OK: {k['ok']} | "
                f"ALERTA: {k['alerta']} | CRÍTICO: {k['critico']} | "
                f"Sin datos: {k['error']}"
            ),
            f"Total diferencias: {k['total_diffs']:,}",
            "",
            "TIENDA | ESTADO | PRECIO | RESTRINGIDO | SOLO HANA | TOTAL | ULT. MOD. POS",
        ]
    )

    for _, fila in df.iterrows():
        estado_raw = str(fila.get("estado", ""))
        estado = _ETIQUETAS_ESTADO.get(estado_raw, estado_raw)
        if estado_raw == "ERROR":
            valores = ["—", "—", "—", "—", "—"]
        else:
            valores = [
                _fmt_num(fila.get("cant_diffs_precio")),
                _fmt_num(fila.get("cant_diffs_restringido")),
                _fmt_num(fila.get("cant_solo_hana")),
                _fmt_num(fila.get("total_diffs")),
                _fmt_fecha_carga(fila.get("ultima_carga_pos")),
            ]
        lineas.append(
            " | ".join([str(fila.get("tienda", "-")), estado] + valores)
        )

    lineas.extend(
        [
            "",
            f"Abrir dashboard: {DASHBOARD_URL}",
            "Umbrales: OK <= 50 | ALERTA <= 300 | CRITICO > 300.",
            "Total = SKUs distintos con al menos una diferencia.",
            "Generado automáticamente por el Monitor POS vs HANA.",
        ]
    )
    return "\n".join(lineas)


def _html_error(mensaje: str, fecha_hora: str) -> str:
    mensaje_html = escape(str(mensaje))
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:20px;background-color:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827;">
  <div style="max-width:800px;margin:0 auto;background-color:#ffffff;border:1px solid #e5e7eb;border-radius:8px;padding:20px;">
    <h2 style="color:#b91c1c;margin-top:0;">Monitor POS vs HANA — no se pudo generar el resumen</h2>
    <p style="font-size:13px;color:#374151;">{fecha_hora}</p>
    <div style="background-color:#fef2f2;border-left:4px solid #ef4444;padding:10px 14px;font-size:13px;color:#991b1b;white-space:pre-wrap;">{mensaje_html}</div>
    <p style="font-size:12px;color:#6b7280;margin-top:18px;">Revisá la conexión a HANA y el log del script en el servidor.</p>
  </div>
</body>
</html>"""


def _texto_error(mensaje: str, fecha_hora: str) -> str:
    return (
        "Monitor POS vs HANA — no se pudo generar el resumen\n"
        f"{fecha_hora}\n\n"
        f"{mensaje}\n\n"
        "Revisá la conexión a HANA y el log del script en el servidor.\n"
    )


# ============================================================
# ENVÍO VÍA XSJS
# ============================================================

def _crear_opener(url: str, cookie_jar, verificar_tls: bool):
    # Sin ProxyHandler: el XSJS es un servicio interno, no debe pasar por proxy.
    handlers = [
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(cookie_jar),
    ]
    if url.lower().startswith("https") and not verificar_tls:
        contexto = ssl.create_default_context()
        contexto.check_hostname = False
        contexto.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=contexto))
    return urllib.request.build_opener(*handlers)


def _leer_error_http(error: urllib.error.HTTPError) -> str:
    try:
        cuerpo = error.read().decode("utf-8", errors="replace")
        return cuerpo or str(error.reason)
    except Exception:
        return str(error.reason)


def _xsjs_enviar(cfg: dict, payload: dict) -> dict:
    """Llama al XSJS: primero obtiene el token CSRF y luego envía el POST."""
    url = cfg["url"]
    cookie_jar = http.cookiejar.CookieJar()
    opener = _crear_opener(url, cookie_jar, cfg["verificar_tls"])
    headers_base = {
        "Accept": "application/json",
        "User-Agent": "MonitorPOS-HANA/1.0",
    }
    if cfg["usuario"] or cfg["clave"]:
        token = base64.b64encode(
            f"{cfg['usuario']}:{cfg['clave']}".encode("utf-8")
        ).decode("ascii")
        headers_base["Authorization"] = f"Basic {token}"

    csrf = None
    headers_get = dict(headers_base)
    headers_get["x-csrf-token"] = "Fetch"
    request_get = urllib.request.Request(url, headers=headers_get, method="GET")
    try:
        with opener.open(request_get, timeout=cfg["timeout"]) as respuesta:
            csrf = (
                respuesta.headers.get("x-csrf-token")
                or respuesta.headers.get("X-CSRF-Token")
            )
            respuesta.read()
    except urllib.error.HTTPError as error:
        cuerpo = _leer_error_http(error)
        if error.code in (401, 403):
            raise RuntimeError(
                f"XSJS rechazó la autenticación ({error.code}): {cuerpo}"
            ) from error
        if error.code == 405:
            logger.info("XSJS no permite GET para CSRF; se intenta POST directo.")
        else:
            raise RuntimeError(
                f"Error consultando CSRF del XSJS ({error.code}): {cuerpo}"
            ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"No se pudo conectar al XSJS {url}: {error.reason}"
        ) from error

    headers_post = dict(headers_base)
    headers_post["Content-Type"] = "application/json; charset=utf-8"
    if csrf:
        headers_post["x-csrf-token"] = csrf

    datos = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_post = urllib.request.Request(
        url, data=datos, headers=headers_post, method="POST"
    )
    try:
        with opener.open(request_post, timeout=cfg["timeout"]) as respuesta:
            cuerpo = respuesta.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        cuerpo = _leer_error_http(error)
        raise RuntimeError(
            f"XSJS devolvió HTTP {error.code}: {cuerpo}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"No se pudo enviar el correo al XSJS: {error.reason}"
        ) from error

    try:
        resultado = json.loads(cuerpo) if cuerpo else {}
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Respuesta no JSON del XSJS: {cuerpo[:500]}"
        ) from error

    if not resultado.get("ok"):
        raise RuntimeError(
            f"El XSJS no confirmó el envío: {cuerpo[:500]}"
        )
    return resultado


def _armar_payload(
    asunto: str,
    html: str,
    texto: str,
    cfg: dict,
    png_bytes: bytes | None,
) -> dict:
    imagenes = []
    if png_bytes:
        imagenes.append(
            {
                "contentId": _CID_GRAFICO,
                "contentType": "image/png",
                "fileName": "resumen_tiendas.png",
                "dataBase64": base64.b64encode(png_bytes).decode("ascii"),
            }
        )
    return {
        "subject": asunto,
        "sender": REMITENTE,
        "to": cfg["to"],
        "html": html,
        "text": texto,
        "inlineImages": imagenes,
    }


def _enviar_o_guardar(
    asunto: str,
    html: str,
    texto: str,
    png_bytes: bytes | None,
    cfg: dict,
    dry_run: bool,
) -> None:
    if dry_run:
        html_preview = html.replace(f"cid:{_CID_GRAFICO}", "grafico_resumen.png")
        ruta_html = _LOGS_DIR / "email_preview.html"
        ruta_html.write_text(html_preview, encoding="utf-8")
        if png_bytes:
            (_LOGS_DIR / "grafico_resumen.png").write_bytes(png_bytes)
        logger.info("Dry-run: preview generado en %s", ruta_html)
        return

    payload = _armar_payload(asunto, html, texto, cfg, png_bytes)
    resultado = _xsjs_enviar(cfg, payload)
    logger.info(
        "Correo enviado vía XSJS: messageId=%s finalReply=%s",
        resultado.get("messageId", "-"),
        resultado.get("finalReply", "-"),
    )


def _enviar_error(mensaje: str, cfg: dict, dry_run: bool) -> int:
    fecha_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
    asunto = (
        "[ERROR] Monitor POS vs HANA — no se pudo generar el resumen — "
        f"{datetime.now().strftime('%d/%m/%Y')}"
    )
    try:
        _enviar_o_guardar(
            asunto,
            _html_error(mensaje, fecha_hora),
            _texto_error(mensaje, fecha_hora),
            None,
            cfg,
            dry_run,
        )
        return 0
    except Exception:
        logger.exception("No se pudo enviar el correo de error")
        return 1


# ============================================================
# MAIN
# ============================================================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Envía el correo diario del Monitor POS vs HANA."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Genera el HTML y el PNG en logs/ sin enviar el correo.",
    )
    parser.add_argument(
        "--to",
        default=None,
        help="Destinatarios de prueba separados por coma (omite la vista HANA).",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)

    cfg = _leer_config_envio()
    cfg["to"] = _resolver_destinatarios(args.to)

    if not args.dry_run and not _validar_config(cfg):
        return 1

    logger.info("Correo diario [%s]: consultando resumen de tiendas...", AMBIENTE)
    try:
        df = get_resumen_tiendas(AMBIENTE)
    except Exception as ex:
        logger.exception("Correo diario: error consultando el resumen de tiendas")
        return _enviar_error(f"No se pudo consultar HANA: {ex}", cfg, args.dry_run)

    if df is None or df.empty:
        mensaje = "El resumen de tiendas vino vacío (¿no hay tiendas configuradas?)."
        logger.error("Correo diario: %s", mensaje)
        return _enviar_error(mensaje, cfg, args.dry_run)

    estado_global = _estado_global(df)
    asunto = _asunto(df, estado_global)
    logger.info(
        "Correo diario: %d tiendas, estado global=%s, asunto=%s",
        len(df),
        estado_global,
        asunto,
    )

    info_staging = _info_staging()
    linea_staging = _texto_staging(info_staging)
    if linea_staging:
        logger.info("Correo diario: %s", linea_staging)

    png_bytes = None
    incluir_grafico = True
    try:
        ruta_png = _LOGS_DIR / "grafico_resumen.png"
        png_bytes = _generar_grafico(df, ruta_png)
        logger.info("Correo diario: gráfico generado en %s", ruta_png)
    except Exception:
        logger.exception(
            "Correo diario: no se pudo generar el gráfico; se envía sin imagen"
        )
        incluir_grafico = False
        png_bytes = None

    html = _generar_html(
        df,
        incluir_grafico=incluir_grafico,
        info_staging=info_staging,
    )
    texto = _generar_texto(df, info_staging=info_staging)

    try:
        _enviar_o_guardar(asunto, html, texto, png_bytes, cfg, args.dry_run)
    except Exception:
        logger.exception("Correo diario: no se pudo enviar el correo")
        return 1

    logger.info("Correo diario: finalizado correctamente.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
