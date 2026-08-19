"""
Configuración de conexiones a bases de datos.

HANA (SAP) — fuente de verdad (Base Imagen Post)
PostgreSQL por tienda — conexiones via StoreConnectionManager (stores.json)
"""

import json
import os
from dataclasses import dataclass, field
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
# Estructuras de configuración
# ============================================================

@dataclass
class HANAConfig:
    """Configuración de conexión a SAP HANA."""
    host: str = field(default_factory=lambda: os.getenv("HANA_HOST", "localhost"))
    port: str = field(default_factory=lambda: os.getenv("HANA_PORT", "30015"))
    user: str = field(default_factory=lambda: os.getenv("HANA_USER", ""))
    password: str = field(default_factory=lambda: os.getenv("HANA_PASSWORD", ""))
    schema: str = field(default_factory=lambda: os.getenv("HANA_SCHEMA", ""))

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
    """Manejador de conexión a HANA."""

    def __init__(self):
        self._hana_engine: Engine | None = None
        self._hana_config = HANAConfig()

    # ---- HANA ----

    @property
    def hana(self) -> Engine:
        if self._hana_engine is None:
            logger.info(
                "Conectando a HANA en %s:%s ...",
                self._hana_config.host,
                self._hana_config.port,
            )
            self._hana_engine = create_engine(
                self._hana_config.connection_url,
                connect_args={"autocommit": True},
                pool_pre_ping=True,
            )
        return self._hana_engine


# Instancia global (singleton)
db = DatabaseConnection()


# ============================================================
# Gestor de conexiones multi-tienda (PostgreSQL)
# ============================================================

_STORES_JSON = Path(__file__).resolve().parent.parent / "stores.json"


class StoreConnectionManager:
    """
    Gestiona conexiones PostgreSQL independientes por tienda.

    Cada tienda tiene su propio servidor PostgreSQL local.
    Las credenciales se leen de stores.json en la raíz del proyecto.

    Para agregar una tienda nueva, añadir una entrada en stores.json:
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

    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}
        self._configs: dict = self._load_configs()

    def _load_configs(self) -> dict:
        if not _STORES_JSON.exists():
            logger.warning("stores.json no encontrado en %s — sin tiendas configuradas.", _STORES_JSON)
            return {}
        try:
            with open(_STORES_JSON, encoding="utf-8") as f:
                data = json.load(f)
            # Ignorar claves que empiezan con "_" (comentarios)
            return {k: v for k, v in data.items() if not k.startswith("_")}
        except Exception as e:
            logger.error("Error leyendo stores.json: %s", e)
            return {}

    def get_engine(self, store_code: str) -> Engine:
        """Devuelve (o crea) el engine PostgreSQL para una tienda."""
        if store_code not in self._engines:
            if store_code not in self._configs:
                raise ValueError(
                    f"Tienda '{store_code}' no encontrada en stores.json. "
                    f"Tiendas disponibles: {self.list_stores()}"
                )
            cfg = self._configs[store_code]
            url = (
                f"postgresql+psycopg2://{cfg['user']}:{cfg['password']}"
                f"@{cfg['host']}:{cfg['port']}/{cfg['db']}"
            )
            self._engines[store_code] = create_engine(url, pool_pre_ping=True)
            logger.info("Engine PostgreSQL creado para tienda %s (%s:%s)", store_code, cfg["host"], cfg["port"])
        return self._engines[store_code]

    def list_stores(self) -> list[str]:
        """Lista los códigos de tienda configurados en stores.json."""
        return sorted(self._configs.keys())


# Instancia global del gestor de tiendas
store_manager = StoreConnectionManager()
