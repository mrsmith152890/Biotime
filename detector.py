"""
Motor de detección de llegadas tarde para BioTime 8.0 Colombia.

Lógica:
1. Por cada empleado con turno asignado hoy
2. Busca su primera transacción de CHECK-IN del día
3. Compara con la hora de inicio del turno + período de gracia
4. Si llegó tarde → retorna LateArrival para notificación inmediata

Consideraciones Colombia:
- Zona horaria America/Bogota (UTC-5, sin cambio de hora)
- BioTime puede guardar los tiempos en UTC → se convierte antes de comparar
- Algunos turnos nocturnos cruzan medianoche
"""

import logging
from datetime import date, datetime, time, timedelta
from typing import Optional
import pytz

from .config import settings
from .models import BTEmployee, BTTransaction, BTEmployeeSchedule, LateArrival

logger = logging.getLogger(__name__)
BOGOTA_TZ = pytz.timezone("America/Bogota")


def to_bogota(dt: datetime) -> datetime:
    """Convierte datetime a hora Colombia."""
    if dt.tzinfo is None:
        # Asumimos que BioTime Colombia guarda en hora local Colombia
        # Si tu servidor BioTime está en UTC, cambia esto a:
        # return pytz.utc.localize(dt).astimezone(BOGOTA_TZ)
        return BOGOTA_TZ.localize(dt)
    return dt.astimezone(BOGOTA_TZ)


def minutes_late(scheduled_start: time, actual_checkin: datetime, grace: int) -> int:
    """
    Calcula minutos de retraso.
    Retorna 0 si llegó a tiempo (dentro del período de gracia).
    """
    bogota_checkin = to_bogota(actual_checkin)
    checkin_time = bogota_checkin.time()

    # Construir datetime para scheduled_start en la misma fecha
    checkin_date = bogota_checkin.date()
    scheduled_dt = BOGOTA_TZ.localize(
        datetime(checkin_date.year, checkin_date.month, checkin_date.day,
                 scheduled_start.hour, scheduled_start.minute)
    )
    deadline_dt = scheduled_dt + timedelta(minutes=grace)

    if bogota_checkin <= deadline_dt:
        return 0

    delta = bogota_checkin - scheduled_dt
    return int(delta.total_seconds() / 60)


class LateArrivalDetector:
    """Detecta llegadas tarde comparando marcajes vs turnos."""

    def __init__(self, grace_minutes: Optional[int] = None):
        self.grace = grace_minutes if grace_minutes is not None else settings.GRACE_PERIOD_MINUTES

    def detect_from_full_day(
        self,
        target_date: date,
        employees: list[BTEmployee],
        schedules: list[BTEmployeeSchedule],
        transactions: list[BTTransaction],
    ) -> list[LateArrival]:
        """
        Análisis completo del día.
        Usado para el reporte diario y para revisión manual.
        """
        emp_map = {e.emp_code: e for e in employees}
        schedule_map = {s.emp_code: s for s in schedules}

        # Agrupar transacciones por empleado, solo CHECK-IN
        checkins: dict[str, list[BTTransaction]] = {}
        for tx in transactions:
            if tx.is_checkin:
                checkins.setdefault(tx.emp_code, []).append(tx)

        late_arrivals = []
        date_str = target_date.strftime("%Y-%m-%d")

        for emp_code, sched in schedule_map.items():
            if sched.start_time_obj is None:
                logger.debug(f"Empleado {emp_code} sin hora de inicio en turno, omitiendo")
                continue

            emp = emp_map.get(emp_code)
            if not emp:
                logger.debug(f"Empleado {emp_code} en turno pero no en listado de personal")
                continue

            emp_checkins = sorted(checkins.get(emp_code, []), key=lambda t: t.punch_time)

            if not emp_checkins:
                # No marcó → no se notifica aquí (otro proceso maneja ausencias)
                continue

            first_checkin = emp_checkins[0]
            mins = minutes_late(sched.start_time_obj, first_checkin.punch_time, self.grace)

            if mins > 0:
                bogota_dt = to_bogota(first_checkin.punch_time)
                late_arrivals.append(LateArrival(
                    emp_code=emp_code,
                    full_name=emp.full_name,
                    department=emp.department,
                    mobile=emp.mobile,
                    email=emp.email,
                    scheduled_start=sched.start_time_obj,
                    actual_checkin=bogota_dt,
                    minutes_late=mins,
                    terminal=first_checkin.terminal_alias,
                    date=date_str,
                ))

        late_arrivals.sort(key=lambda x: x.minutes_late, reverse=True)
        logger.info(f"Detectadas {len(late_arrivals)} llegadas tarde para {date_str}")
        return late_arrivals

    def check_single_transaction(
        self,
        transaction: BTTransaction,
        employee: BTEmployee,
        schedule: BTEmployeeSchedule,
    ) -> Optional[LateArrival]:
        """
        Evalúa UN marcaje individual.
        Retorna LateArrival si es tarde, None si está a tiempo.
        Usado para notificación en tiempo real (polling).
        """
        if not transaction.is_checkin:
            return None

        if schedule.start_time_obj is None:
            return None

        mins = minutes_late(schedule.start_time_obj, transaction.punch_time, self.grace)
        if mins <= 0:
            return None

        bogota_dt = to_bogota(transaction.punch_time)
        return LateArrival(
            emp_code=employee.emp_code,
            full_name=employee.full_name,
            department=employee.department,
            mobile=employee.mobile,
            email=employee.email,
            scheduled_start=schedule.start_time_obj,
            actual_checkin=bogota_dt,
            minutes_late=mins,
            terminal=transaction.terminal_alias,
            date=bogota_dt.strftime("%Y-%m-%d"),
        )
