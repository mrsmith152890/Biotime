"""
Monitor en tiempo real de llegadas tarde.

Funcionamiento:
- Corre como tarea de fondo en FastAPI (asyncio)
- Cada POLL_INTERVAL_SECONDS consulta nuevos marcajes en BioTime Colombia
- Evalúa inmediatamente cada CHECK-IN recibido
- Si el empleado llegó tarde → notificación INDIVIDUAL E INMEDIATA
- Lleva registro de quién ya fue notificado hoy para no duplicar

Ventana de monitoreo configurable: MONITOR_START_TIME → MONITOR_END_TIME
"""

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import Optional
import pytz

from .biotime_client import BioTimeColombiaClient
from .config import settings
from .detector import LateArrivalDetector, to_bogota
from .models import LateArrival, MonitorStatus, NotificationResult
from .notifier import NotificationService

logger = logging.getLogger(__name__)
BOGOTA_TZ = pytz.timezone("America/Bogota")


class LateArrivalMonitor:
    """
    Monitor asíncrono que detecta y notifica llegadas tarde en tiempo real.
    Una instancia vive durante toda la vida del servidor FastAPI.
    """

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._status = MonitorStatus(running=False, processed_today=[], errors=[])

        # Datos del día actual (se refrescan a medianoche)
        self._today: Optional[date] = None
        self._employees: dict = {}          # emp_code → BTEmployee
        self._schedules: dict = {}          # emp_code → BTEmployeeSchedule
        self._timetables: dict = {}         # nombre → start_time_str
        self._notified_today: set[str] = set()    # emp_codes ya notificados
        self._last_tx_time: Optional[datetime] = None

        self.detector = LateArrivalDetector()
        self.notifier = NotificationService()
        self.results_log: list[NotificationResult] = []

    # ── Control del monitor ───────────────────────────────────────────────────

    async def start(self):
        if self._running:
            logger.warning("Monitor ya está corriendo")
            return
        self._running = True
        self._status.running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Monitor de llegadas tarde iniciado")

    async def stop(self):
        self._running = False
        self._status.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Monitor detenido")

    def get_status(self) -> MonitorStatus:
        self._status.processed_today = list(self._notified_today)
        return self._status

    # ── Loop principal ────────────────────────────────────────────────────────

    async def _run_loop(self):
        """Loop de polling. Corre en asyncio usando executor para llamadas bloqueantes."""
        logger.info(f"Loop iniciado. Intervalo: {settings.POLL_INTERVAL_SECONDS}s")

        while self._running:
            now_bogota = datetime.now(BOGOTA_TZ)

            # Verificar si estamos en la ventana de monitoreo
            if self._is_within_window(now_bogota):
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._poll_and_notify
                    )
                except Exception as e:
                    error_msg = f"{now_bogota.strftime('%H:%M:%S')} - Error en poll: {e}"
                    logger.error(error_msg)
                    self._status.errors.append(error_msg)
                    if len(self._status.errors) > 50:
                        self._status.errors = self._status.errors[-50:]
            else:
                logger.debug(f"Fuera de ventana de monitoreo ({self._get_window_str()})")
                # Resetear al pasar la medianoche
                if now_bogota.date() != self._today:
                    self._reset_daily_state()

            await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)

    def _poll_and_notify(self):
        """
        Lógica de polling (síncrona, corre en executor).
        1. Carga datos del día si es necesario
        2. Obtiene marcajes nuevos desde la última consulta
        3. Por cada check-in nuevo → evalúa inmediatamente
        4. Si es tarde → notificación individual INMEDIATA
        """
        now_bogota = datetime.now(BOGOTA_TZ)
        self._status.last_poll = now_bogota
        self._status.next_poll = now_bogota + timedelta(seconds=settings.POLL_INTERVAL_SECONDS)

        with BioTimeColombiaClient() as client:
            # Cargar datos del día si es la primera vez o cambió el día
            if self._today != now_bogota.date():
                self._load_daily_data(client, now_bogota.date())

            # Determinar desde cuándo buscar marcajes
            if self._last_tx_time is None:
                # Primera consulta del día: desde el inicio de la ventana
                window_start = self._get_window_start_dt(now_bogota.date())
                since = window_start
            else:
                # Consultas siguientes: desde el último marcaje procesado
                since = self._last_tx_time - timedelta(minutes=1)  # solapamiento de 1 min

            logger.debug(f"Buscando marcajes desde {since.strftime('%H:%M:%S')}")
            transactions = client.get_transactions_since(since)

            if not transactions:
                logger.debug("Sin nuevos marcajes")
                return

            logger.info(f"Nuevos marcajes encontrados: {len(transactions)}")

            for tx in sorted(transactions, key=lambda t: t.punch_time):
                tx_bogota = to_bogota(tx.punch_time)
                # Actualizar cursor de tiempo
                if self._last_tx_time is None or tx_bogota > self._last_tx_time:
                    self._last_tx_time = tx_bogota

                # Solo check-ins (estado 0)
                if not tx.is_checkin:
                    continue

                # Ya notificamos a este empleado hoy?
                if tx.emp_code in self._notified_today:
                    logger.debug(f"Empleado {tx.emp_code} ya notificado hoy, omitiendo")
                    continue

                emp = self._employees.get(tx.emp_code)
                sched = self._schedules.get(tx.emp_code)

                if not emp:
                    logger.debug(f"Empleado {tx.emp_code} no encontrado en directorio")
                    continue

                if not sched:
                    logger.debug(f"Empleado {emp.full_name} sin turno asignado hoy")
                    continue

                # ── EVALUACIÓN INDIVIDUAL ─────────────────────────────────────
                late = self.detector.check_single_transaction(tx, emp, sched)

                if late is None:
                    logger.info(
                        f"✅ {emp.full_name} - A TIEMPO "
                        f"(entrada: {tx_bogota.strftime('%H:%M')})"
                    )
                    # Marcar como procesado para no revisar de nuevo
                    self._notified_today.add(tx.emp_code)
                    continue

                # ── NOTIFICACIÓN INMEDIATA ────────────────────────────────────
                logger.info(
                    f"🚨 {emp.full_name} - TARDE {late.minutes_late} min "
                    f"(programado: {late.scheduled_start.strftime('%H:%M')}, "
                    f"llegó: {tx_bogota.strftime('%H:%M')})"
                )

                results = self.notifier.notify_late_arrival(late)
                self.results_log.extend(results)
                self._notified_today.add(tx.emp_code)

                # Mantener log manejable
                if len(self.results_log) > 500:
                    self.results_log = self.results_log[-500:]

    # ── Carga de datos del día ────────────────────────────────────────────────

    def _load_daily_data(self, client: BioTimeColombiaClient, target_date: date):
        """Carga empleados y turnos del día. Se ejecuta una vez por día."""
        logger.info(f"Cargando datos del día {target_date}...")
        self._reset_daily_state()
        self._today = target_date

        employees = client.get_employees(active_only=True)
        self._employees = {e.emp_code: e for e in employees}
        logger.info(f"Empleados cargados: {len(self._employees)}")

        schedules = client.get_employee_schedules(target_date)
        self._schedules = {s.emp_code: s for s in schedules}
        logger.info(f"Turnos cargados: {len(self._schedules)}")

    def _reset_daily_state(self):
        """Reinicia el estado al cambiar de día."""
        self._today = None
        self._notified_today = set()
        self._last_tx_time = None
        logger.info("Estado diario reiniciado")

    # ── Helpers de ventana ────────────────────────────────────────────────────

    def _is_within_window(self, now: datetime) -> bool:
        start = self._parse_time(settings.MONITOR_START_TIME)
        end = self._parse_time(settings.MONITOR_END_TIME)
        current = now.time()
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    def _get_window_start_dt(self, d: date) -> datetime:
        t = self._parse_time(settings.MONITOR_START_TIME)
        return BOGOTA_TZ.localize(datetime(d.year, d.month, d.day, t.hour, t.minute))

    def _get_window_str(self) -> str:
        return f"{settings.MONITOR_START_TIME} - {settings.MONITOR_END_TIME}"

    @staticmethod
    def _parse_time(t_str: str) -> time:
        parts = t_str.split(":")
        return time(int(parts[0]), int(parts[1]))


# Instancia global del monitor (singleton)
monitor = LateArrivalMonitor()
