"""
Configuración central - BioTime 8.0 Colombia
Carga variables desde .env o variables de entorno del sistema.
"""

import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── BioTime Colombia ──────────────────────────────────────────────────────
    BIOTIME_BASE_URL: str = "http://localhost:8000"       # URL del servidor BioTime
    BIOTIME_USERNAME: str = "admin"
    BIOTIME_PASSWORD: str = "admin"
    BIOTIME_TIMEOUT: int = 30                             # segundos
    BIOTIME_PAGE_SIZE: int = 500                          # registros por página
    BIOTIME_VERIFY_SSL: bool = False                      # SSL self-signed frecuente en Colombia

    # ── Turno / Tolerancia ────────────────────────────────────────────────────
    # Minutos de gracia antes de marcar como tarde
    GRACE_PERIOD_MINUTES: int = 5
    # Zona horaria Colombia
    TIMEZONE: str = "America/Bogota"

    # ── Monitor ───────────────────────────────────────────────────────────────
    # Cada cuántos segundos se revisa BioTime (60 = cada minuto)
    POLL_INTERVAL_SECONDS: int = 60
    # Hora de inicio del monitoreo diario  (HH:MM)
    MONITOR_START_TIME: str = "05:30"
    # Hora de fin del monitoreo diario
    MONITOR_END_TIME: str = "12:00"

    # ── WhatsApp (Evolution API) ──────────────────────────────────────────────
    EVOLUTION_API_URL: Optional[str] = None              # https://evolution.tuservidor.com
    EVOLUTION_API_KEY: Optional[str] = None
    EVOLUTION_INSTANCE: Optional[str] = None             # nombre de la instancia
    WHATSAPP_ENABLED: bool = False

    # ── Email (SMTP) ──────────────────────────────────────────────────────────
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM: Optional[str] = None
    SMTP_TLS: bool = True
    EMAIL_ENABLED: bool = False

    # ── Notificación ─────────────────────────────────────────────────────────
    # Correos/teléfonos de administradores que reciben el resumen diario
    ADMIN_EMAILS: str = ""          # "rrhh@empresa.com,gerencia@empresa.com"
    ADMIN_PHONES: str = ""          # "573001234567,573007654321"

    # ── API interna ───────────────────────────────────────────────────────────
    API_KEY: str = "cambiar_esta_clave_secreta"
    DEBUG: bool = False

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip() for e in self.ADMIN_EMAILS.split(",") if e.strip()]

    @property
    def admin_phone_list(self) -> list[str]:
        return [p.strip() for p in self.ADMIN_PHONES.split(",") if p.strip()]


settings = Settings()
