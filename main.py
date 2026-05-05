"""
BioTime Colombia - API de Llegadas Tarde
FastAPI application principal

Endpoints:
  GET  /health              → Estado del servicio
  GET  /monitor/status      → Estado del monitor en tiempo real
  POST /monitor/start       → Inicia el monitor
  POST /monitor/stop        → Detiene el monitor
  GET  /report/today        → Reporte del día actual
  GET  /report/date/{date}  → Reporte de una fecha específica
  POST /check/now           → Dispara revisión inmediata manual
  GET  /notifications/log   → Log de notificaciones enviadas
  GET  /employees           → Lista de empleados con turno hoy
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional

import pytz
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

from .biotime_client import BioTimeColombiaClient
from .config import settings
from .detector import LateArrivalDetector
from .models import DailySummary, LateArrival, MonitorStatus
from .monitor import monitor

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/biotime_colombia.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
BOGOTA_TZ = pytz.timezone("America/Bogota")


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicia el monitor al arrancar el servidor."""
    logger.info("=== BioTime Colombia API arrancando ===")
    os.makedirs("logs", exist_ok=True)
    await monitor.start()
    yield
    logger.info("=== BioTime Colombia API cerrando ===")
    await monitor.stop()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="BioTime Colombia - Control de Asistencia",
    description=(
        "API para detección y notificación en tiempo real de llegadas tarde. "
        "Versión Colombia (America/Bogota). Desarrollado por MG Technology."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Seguridad ─────────────────────────────────────────────────────────────────

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: Optional[str] = Security(api_key_header)):
    if api_key != settings.API_KEY:
        raise HTTPException(status_code=401, detail="API Key inválida o no proporcionada")
    return api_key


# ── Modelos de respuesta ──────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    timestamp: str
    timezone: str
    version: str


class CheckNowResponse(BaseModel):
    message: str
    processed: int
    late_detected: int
    notifications_sent: int
    late_employees: list[LateArrival]


class EmployeeSummary(BaseModel):
    emp_code: str
    full_name: str
    department: Optional[str]
    scheduled_start: Optional[str]
    has_checkin_today: bool
    checkin_time: Optional[str]
    status: str    # "a_tiempo" | "tarde" | "sin_marcaje" | "sin_turno"


# ── Endpoints públicos ────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["Sistema"])
async def health():
    now = datetime.now(BOGOTA_TZ)
    return HealthResponse(
        status="ok",
        timestamp=now.isoformat(),
        timezone="America/Bogota",
        version="1.0.0",
    )


# ── Monitor ───────────────────────────────────────────────────────────────────

@app.get("/monitor/status", response_model=MonitorStatus, tags=["Monitor"])
async def get_monitor_status(_: str = Depends(verify_api_key)):
    """Estado actual del monitor: si está corriendo, cuándo fue el último poll, etc."""
    return monitor.get_status()


@app.post("/monitor/start", tags=["Monitor"])
async def start_monitor(_: str = Depends(verify_api_key)):
    """Inicia el monitor de tiempo real si no está corriendo."""
    if monitor._running:
        return {"message": "Monitor ya está corriendo"}
    await monitor.start()
    return {"message": "Monitor iniciado"}


@app.post("/monitor/stop", tags=["Monitor"])
async def stop_monitor(_: str = Depends(verify_api_key)):
    """Detiene el monitor."""
    await monitor.stop()
    return {"message": "Monitor detenido"}


# ── Reporte ───────────────────────────────────────────────────────────────────

@app.get("/report/today", response_model=DailySummary, tags=["Reportes"])
async def report_today(_: str = Depends(verify_api_key)):
    """Genera el reporte de llegadas tarde del día actual."""
    return await _generate_report(date.today())


@app.get("/report/date/{report_date}", response_model=DailySummary, tags=["Reportes"])
async def report_by_date(report_date: str, _: str = Depends(verify_api_key)):
    """
    Genera el reporte de una fecha específica.
    Formato fecha: YYYY-MM-DD
    """
    try:
        target = date.fromisoformat(report_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD")
    return await _generate_report(target)


async def _generate_report(target_date: date) -> DailySummary:
    """Genera reporte completo para una fecha."""
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(
        None, _generate_report_sync, target_date
    )


def _generate_report_sync(target_date: date) -> DailySummary:
    with BioTimeColombiaClient() as client:
        employees = client.get_employees(active_only=True)
        schedules = client.get_employee_schedules(target_date)
        transactions = client.get_transactions(target_date)

    detector = LateArrivalDetector()
    late_list = detector.detect_from_full_day(target_date, employees, schedules, transactions)

    # Empleados con turno
    schedule_codes = {s.emp_code for s in schedules}
    # Empleados que marcaron entrada
    checkin_codes = {
        tx.emp_code for tx in transactions
        if tx.is_checkin
    }

    return DailySummary(
        date=target_date.strftime("%Y-%m-%d"),
        total_employees_with_schedule=len(schedule_codes),
        total_checked_in=len(checkin_codes & schedule_codes),
        total_on_time=len(schedule_codes & checkin_codes) - len(late_list),
        total_late=len(late_list),
        late_employees=late_list,
        generated_at=datetime.now(BOGOTA_TZ),
    )


# ── Revisión manual inmediata ─────────────────────────────────────────────────

@app.post("/check/now", response_model=CheckNowResponse, tags=["Operaciones"])
async def check_now(_: str = Depends(verify_api_key)):
    """
    Fuerza una revisión inmediata de BioTime.
    Detecta y notifica llegadas tarde en este momento.
    Útil para pruebas o cuando el monitor está detenido.
    """
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(
        None, _check_now_sync
    )


def _check_now_sync() -> CheckNowResponse:
    from .notifier import NotificationService

    target_date = date.today()
    with BioTimeColombiaClient() as client:
        employees = client.get_employees(active_only=True)
        schedules = client.get_employee_schedules(target_date)
        transactions = client.get_transactions(target_date)

    detector = LateArrivalDetector()
    late_list = detector.detect_from_full_day(target_date, employees, schedules, transactions)

    notifier = NotificationService()
    notifications_sent = 0

    for late in late_list:
        # Solo notificar si no fue notificado previamente hoy
        if late.emp_code not in monitor._notified_today:
            results = notifier.notify_late_arrival(late)
            notifications_sent += len([r for r in results if r.success])
            monitor._notified_today.add(late.emp_code)
            monitor.results_log.extend(results)

    return CheckNowResponse(
        message=f"Revisión completada para {target_date}",
        processed=len(schedules),
        late_detected=len(late_list),
        notifications_sent=notifications_sent,
        late_employees=late_list,
    )


# ── Log de notificaciones ─────────────────────────────────────────────────────

@app.get("/notifications/log", tags=["Reportes"])
async def get_notifications_log(
    limit: int = 50,
    _: str = Depends(verify_api_key),
):
    """Log de las últimas notificaciones enviadas."""
    log = monitor.results_log[-limit:]
    return {
        "total": len(monitor.results_log),
        "returned": len(log),
        "entries": [r.model_dump() for r in reversed(log)],
    }


# ── Empleados ─────────────────────────────────────────────────────────────────

@app.get("/employees/today", tags=["Empleados"])
async def employees_today(_: str = Depends(verify_api_key)):
    """
    Lista todos los empleados con turno hoy y su estado de marcaje.
    """
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(
        None, _employees_today_sync
    )


def _employees_today_sync():
    target_date = date.today()
    detector = LateArrivalDetector()

    with BioTimeColombiaClient() as client:
        employees = client.get_employees(active_only=True)
        schedules = client.get_employee_schedules(target_date)
        transactions = client.get_transactions(target_date)

    emp_map = {e.emp_code: e for e in employees}
    sched_map = {s.emp_code: s for s in schedules}

    # Primer check-in por empleado
    first_checkin: dict = {}
    for tx in sorted(transactions, key=lambda t: t.punch_time):
        if tx.is_checkin and tx.emp_code not in first_checkin:
            first_checkin[tx.emp_code] = tx

    result = []
    for emp_code, sched in sched_map.items():
        emp = emp_map.get(emp_code)
        if not emp:
            continue

        tx = first_checkin.get(emp_code)
        if not tx:
            status = "sin_marcaje"
            checkin_str = None
        else:
            from .detector import to_bogota
            bogota_dt = to_bogota(tx.punch_time)
            checkin_str = bogota_dt.strftime("%H:%M:%S")
            if sched.start_time_obj:
                late = detector.check_single_transaction(tx, emp, sched)
                status = "tarde" if late else "a_tiempo"
            else:
                status = "a_tiempo"

        result.append(EmployeeSummary(
            emp_code=emp_code,
            full_name=emp.full_name,
            department=emp.department,
            scheduled_start=sched.start_time,
            has_checkin_today=tx is not None,
            checkin_time=checkin_str,
            status=status,
        ).model_dump())

    # Ordenar: tarde primero, luego sin marcaje, luego a tiempo
    order = {"tarde": 0, "sin_marcaje": 1, "a_tiempo": 2}
    result.sort(key=lambda x: order.get(x["status"], 3))
    return {"date": target_date.isoformat(), "total": len(result), "employees": result}
