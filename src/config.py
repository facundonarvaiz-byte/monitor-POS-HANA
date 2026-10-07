"""
Configuración de conexiones a bases de datos.

HANA (SAP) — fuente de verdad (Base Imagen Post)
PostgreSQL por tienda — conexiones via StoreConnectionManager (stores.json)

Dos ambientes:
  - prod: HANA HANA_HOST + stores.json
  - test: HANA HANA_TEST_HOST + stores-test.json

El ambiente se pasa de forma explícita en cada consulta (get_hana(ambiente),
get_store_manager(ambiente)); nunca se asume por estado global.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# Cargar variables de entorno desde .env en la raíz del proyecto
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

# ============================================================
# Configuración de logging
# ============================================================
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("monitor")


# ============================================================
# Ambientes
# ============================================================

AMBIENTES_VALIDOS = ("prod", "test")

AMBIENTES_LABELS = {"prod": "Producción", "test": "Test"}


def validar_ambiente(ambiente: str) -> str:
    """
    Valida el código de ambiente. Evita typos silenciosos (ej. 'test ' o
    'produccion') que podrían resolver engines equivocados.
    """
    if ambiente not in AMBIENTES_VALIDOS:
        raise ValueError(
            f"Ambiente inválido: {ambiente!r}. Válidos: {AMBIENTES_VALIDOS}"
        )
    return ambiente


# ============================================================
# Estructuras de configuración
# ============================================================

@dataclass
class HANAConfig:
    """Configuración de conexión a SAP HANA para un ambiente."""
    host: str
    port: str
    user: str
    password: str
    schema: str

    @classmethod
    def desde_ambiente(cls, ambiente: str) -> "HANAConfig":
        validar_ambiente(ambiente)
        if ambiente == "test":
            return cls(
                host=os.getenv("HANA_TEST_HOST", "hd0-db.cencosud.corp"),
                port=os.getenv("HANA_TEST_PORT", "30015"),
                user=os.getenv("HANA_TEST_USER", ""),
                password=os.getenv("HANA_TEST_PASSWORD", ""),
                schema=os.getenv("HANA_SCHEMA", ""),
            )
        return cls(
            host=os.getenv("HANA_HOST", "localhost"),
            port=os.getenv("HANA_PORT", "30015"),
            user=os.getenv("HANA_USER", ""),
            password=os.getenv("HANA_PASSWORD", ""),
            schema=os.getenv("HANA_SCHEMA", ""),
        )

    @property
    def connection_url(self) -> str:
        return (
            f"hana+hdbcli://{quote_plus(self.user)}:{quote_plus(self.password)}"
            f"@{self.host}:{self.port}/"
        )


# ============================================================
# Fábrica de conexiones
# ============================================================

class DatabaseConnection:
    """Manejador de conexiones a HANA, una por ambiente."""

    def __init__(self):
        self._hana_engines: dict[str, Engine] = {}

    # ---- HANA ----

    def get_hana(self, ambiente: str = "prod") -> Engine:
        """Devuelve (o crea) el engine HANA del ambiente indicado."""
        ambiente = validar_ambiente(ambiente)
        if ambiente not in self._hana_engines:
            cfg = HANAConfig.desde_ambiente(ambiente)
            if not cfg.user or not cfg.password:
                vars_creds = (
                    "HANA_TEST_USER/HANA_TEST_PASSWORD"
                    if ambiente == "test"
                    else "HANA_USER/HANA_PASSWORD"
                )
                raise ValueError(
                    f"Faltan credenciales para el ambiente {ambiente}: "
                    f"configurá {vars_creds} en .env."
                )
            logger.info(
                "Conectando a HANA [%s] en %s:%s ...",
                ambiente,
                cfg.host,
                cfg.port,
            )
            self._hana_engines[ambiente] = create_engine(
                cfg.connection_url,
                connect_args={"autocommit": True},
                pool_pre_ping=True,
            )
        return self._hana_engines[ambiente]

    def host(self, ambiente: str = "prod") -> str:
        """Host HANA del ambiente (para mostrar en la UI)."""
        return HANAConfig.desde_ambiente(ambiente).host


# Instancia global (singleton)
db = DatabaseConnection()


# ============================================================
# Gestor de conexiones multi-tienda (PostgreSQL)
# ============================================================

_BASE_DIR = Path(__file__).resolve().parent.parent
_STORES_JSON = _BASE_DIR / "stores.json"
_STORES_TEST_JSON = _BASE_DIR / "stores-test.json"

_RUTA_STORES_POR_AMBIENTE = {
    "prod": _STORES_JSON,
    "test": _STORES_TEST_JSON,
}

# Tabla HANA de tiendas habilitadas — la lista de tiendas activas sale de acá.
# stores.json / stores-test.json contienen las tiendas con credenciales; solo
# se procesan las que existen en Z_NCR_WERKS_SEL (no hace falta editar el JSON
# para habilitar/deshabilitar tiendas).
_TABLA_WERKS_SEL = '"Z_NCR_CO"."Z_NCR_WERKS_SEL"'


class StoreConnectionManager:
    """
    Gestiona conexiones PostgreSQL independientes por tienda para UN ambiente.

    prod → stores.json
    test → stores-test.json

    Cada tienda tiene su propio servidor PostgreSQL local.
    Las credenciales se leen del JSON correspondiente al ambiente.

    Para agregar una tienda nueva, añadir una entrada en el JSON:
    {
      "E805": {
        "host": "10.x.x.x",
        "port": 5432,
        "db": "webfront",
        "user": "...",
        "password": "...",
        "description": "Tienda E805"
      }
    }
    """

    def __init__(self, ambiente: str) -> None:
        self._ambiente = validar_ambiente(ambiente)
        self._ruta_json = _RUTA_STORES_POR_AMBIENTE[self._ambiente]
        self._engines: dict[str, Engine] = {}
        self._configs: dict = self._load_configs()

    def _load_configs(self) -> dict:
        if not self._ruta_json.exists():
            logger.warning(
                "%s no encontrado en %s — sin tiendas configuradas.",
                self._ruta_json.name,
                self._ruta_json,
            )
            return {}
        try:
            with open(self._ruta_json, encoding="utf-8") as f:
                data = json.load(f)
            # Ignorar claves que empiezan con "_" (comentarios)
            return {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception as e:
            logger.error("Error leyendo %s: %s", self._ruta_json.name, e)
            return {}

    def get_engine(self, store_code: str) -> Engine:
        """Devuelve (o crea) el engine PostgreSQL para una tienda."""
        if store_code not in self._engines:
            if store_code not in self._configs:
                raise ValueError(
                    f"Tienda '{store_code}' no encontrada en {self._ruta_json.name} "
                    f"(ambiente {self._ambiente}). "
                    f"Tiendas disponibles: {self.list_stores()}"
                )
            cfg = self._configs[store_code]
            url = (
                f"postgresql+psycopg2://{cfg['user']}:{cfg['password']}"
                f"@{cfg['host']}:{cfg['port']}/{cfg['db']}"
            )
            self._engines[store_code] = create_engine(url, pool_pre_ping=True)
            logger.info(
                "Engine PostgreSQL creado [%s] para tienda %s (%s:%s)",
                self._ambiente,
                store_code,
                cfg["host"],
                cfg["port"],
            )
        return self._engines[store_code]

    def _fallback_test(self) -> list[str]:
        """En test, si Z_NCR_WERKS_SEL no aporta tiendas, usar el JSON completo."""
        if self._ambiente != "test":
            return []
        activas = sorted(self._configs)
        logger.warning(
            "HANA test sin tiendas habilitadas en %s; "
            "usando todas las de %s: %s",
            _TABLA_WERKS_SEL,
            self._ruta_json.name,
            activas,
        )
        return activas

    def list_stores(self) -> list[str]:
        """
        Lista las tiendas activas del ambiente: las habilitadas en la tabla
        HANA Z_NCR_WERKS_SEL del MISMO ambiente que además tengan credenciales
        en el JSON correspondiente.

        En test, si la tabla no existe o queda vacía, se usan todas las
        tiendas de stores-test.json (fallback).
        """
        try:
            with db.get_hana(self._ambiente).connect() as conn:
                werks = conn.execute(
                    text(f'SELECT "WERKS" FROM {_TABLA_WERKS_SEL}')
                ).scalars().all()
        except Exception as e:
            logger.error(
                "Error leyendo %s en HANA [%s]: %s",
                _TABLA_WERKS_SEL,
                self._ambiente,
                e,
            )
            return self._fallback_test()

        habilitadas = {str(w).strip() for w in werks}
        activas = sorted(h for h in habilitadas if h in self._configs)

        excluidas = sorted(set(self._configs) - habilitadas)
        if excluidas:
            logger.info(
                "Tiendas en %s no habilitadas en %s [%s] (se omiten): %s",
                self._ruta_json.name,
                _TABLA_WERKS_SEL,
                self._ambiente,
                excluidas,
            )
        no_config = sorted(habilitadas - set(self._configs))
        if no_config:
            logger.warning(
                "Tiendas habilitadas en %s [%s] sin credenciales en %s (se omiten): %s",
                _TABLA_WERKS_SEL,
                self._ambiente,
                self._ruta_json.name,
                no_config,
            )

        if not activas:
            return self._fallback_test()

        return activas


# Gestores de tiendas por ambiente (lazy)
_managers: dict[str, StoreConnectionManager] = {}


def get_store_manager(ambiente: str = "prod") -> StoreConnectionManager:
    """Devuelve (o crea) el gestor de tiendas del ambiente indicado."""
    ambiente = validar_ambiente(ambiente)
    if ambiente not in _managers:
        _managers[ambiente] = StoreConnectionManager(ambiente)
    return _managers[ambiente]
