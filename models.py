"""
Modelos de datos para BioTime 8.0 Colombia.
La versión Colombia puede tener campos adicionales versus la versión internacional.
"""

from datetime import datetime, time
from typing import Optional
from pydantic import BaseModel, field_validator
import pytz

BOGOTA_TZ = pytz.timezone("America/Bogota")


# ── BioTime respuestas ────────────────────────────────────────────────────────

class BTEmployee(BaseModel):
    """Empleado en BioTime Colombia."""
    emp_code: str                           # Código único del empleado
    first_name: str
    last_name: str
    department: Optional[str] = None
    position: Optional[str] = None
    mobile: Optional[str] = None           # Celular para WhatsApp (ej: 3001234567)
    email: Optional[str] = None
    # Campo Colombia: en algunas instalaciones viene como "hire_date"
    hire_date: Optional[str] = None
    # Estado activo
    enable_att: bool = True

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def whatsapp_number(self) -> Optional[str]:
        """Formatea número colombiano para WhatsApp (57XXXXXXXXXX)."""
        if not self.mobile:
            return None
        num = self.mobile.strip().replace(" ", "").replace("-", "").replace("+", "")
        if num.startswith("57") and len(num) == 12:
            return num
        if num.startswith("3") and len(num) == 10:
            return f"57{num}"
        return None


class BTTransaction(BaseModel):
    """
    Transacción de marcaje en BioTime Colombia.
    Colombia usa punch_state:
      0 = Check-In
      1 = Check-Out
      2 = Break-Out
      3 = Break-In
      4 = Overtime-In
      5 = Overtime-Out
    """
    id: int
    emp_code: str
    punch_time: datetime                   # BioTime devuelve UTC, se convierte
    punch_state: str = "0"                 # "0" = entrada
    terminal_alias: Optional[str] = None
    area_alias: Optional[str] = None
    upload_time: Optional[datetime] = None

    @property
    def punch_time_bogota(self) -> datetime:
        """Convierte a hora Colombia."""
        if self.punch_time.tzinfo is None:
            return BOGOTA_TZ.localize(self.punch_time)
        return self.punch_time.astimezone(BOGOTA_TZ)

    @property
    def is_checkin(self) -> bool:
        return str(self.punch_state) in ("0", "check in", "Check In")


class BTScheduleDetail(BaseModel):
    """Detalle de turno (timetable) en BioTime Colombia."""
    id: int
    timetable_name: str
    # Hora de inicio del turno
    start_time: Optional[str] = None      # formato "HH:MM:SS" o "HH:MM"
    end_time: Optional[str] = None

    @property
    def start_time_obj(self) -> Optional[time]:
        if not self.start_time:
            return None
        try:
            parts = self.start_time.split(":")
            return time(int(parts[0]), int(parts[1]))
        except Exception:
            return None


class BTEmployeeSchedule(BaseModel):
    """Asignación de turno a empleado para una fecha."""
    emp_code: str
    schedule_date: str                     # "YYYY-MM-DD"
    timetable_alias: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None

    @property
    def start_time_obj(self) -> Optional[time]:
        if not self.start_time:
            return None
        try:
            parts = self.start_time.split(":")
            return time(int(parts[0]), int(parts[1]))
        except Exception:
            return None


# ── Modelos internos ──────────────────────────────────────────────────────────

class LateArrival(BaseModel):
    """Llegada tarde detectada."""
    emp_code: str
    full_name: str
    department: Optional[str]
    mobile: Optional[str]
    email: Optional[str]
    scheduled_start: time                  # hora de entrada según turno
    actual_checkin: datetime               # hora real de marcaje (Colombia)
    minutes_late: int
    terminal: Optional[str]
    date: str                              # "YYYY-MM-DD"

    # Control de notificación
    notified_whatsapp: bool = False
    notified_email: bool = False
    notification_error: Optional[str] = None


class NotificationResult(BaseModel):
    """Resultado del envío de notificación."""
    emp_code: str
    full_name: str
    channel: str                           # "whatsapp" | "email"
    success: bool
    message: Optional[str] = None
    timestamp: datetime


class DailySummary(BaseModel):
    """Resumen del día procesado."""
    date: str
    total_employees_with_schedule: int
    total_checked_in: int
    total_on_time: int
    total_late: int
    late_employees: list[LateArrival]
    generated_at: datetime


class MonitorStatus(BaseModel):
    """Estado actual del monitor."""
    running: bool
    last_poll: Optional[datetime] = None
    next_poll: Optional[datetime] = None
    processed_today: list[str] = []        # emp_codes ya notificados hoy
    errors: list[str] = []
