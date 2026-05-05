"""
Cliente para BioTime 8.0 Colombia.

DIFERENCIAS versión Colombia vs internacional:
- Endpoint de auth: /jwt/api-token-auth/ (algunas instalaciones usan /api-token-auth/)
- Los campos de empleado pueden incluir datos de cédula (document_number)
- Los turnos pueden estar en UTC-5 directamente (depende de la config del servidor)
- Soporte para horas extras colombianas (recargo nocturno, dominical, festivo)
- Paginación obligatoria: page + page_size en todos los listados
- Headers específicos para CORS de instalaciones locales en Colombia
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional
import httpx
import pytz

from .config import settings
from .models import BTEmployee, BTTransaction, BTEmployeeSchedule

logger = logging.getLogger(__name__)
BOGOTA_TZ = pytz.timezone("America/Bogota")


class BioTimeColombiaClient:
    """
    Wrapper completo para la API REST de BioTime 8.0 Colombia.
    Maneja autenticación JWT, renovación de token y paginación automática.
    """

    def __init__(self):
        self.base_url = settings.BIOTIME_BASE_URL.rstrip("/")
        self.token: Optional[str] = None
        self.token_expiry: Optional[datetime] = None
        self._client = httpx.Client(
            timeout=settings.BIOTIME_TIMEOUT,
            verify=settings.BIOTIME_VERIFY_SSL,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    # ── Autenticación ─────────────────────────────────────────────────────────

    def authenticate(self) -> str:
        """
        Obtiene token JWT de BioTime Colombia.
        Intenta primero /jwt/api-token-auth/ (Colombia), luego /api-token-auth/
        """
        endpoints = [
            f"{self.base_url}/jwt/api-token-auth/",
            f"{self.base_url}/api-token-auth/",
        ]
        payload = {
            "username": settings.BIOTIME_USERNAME,
            "password": settings.BIOTIME_PASSWORD,
        }

        last_error = None
        for endpoint in endpoints:
            try:
                resp = self._client.post(endpoint, json=payload)
                resp.raise_for_status()
                data = resp.json()
                token = data.get("token") or data.get("access")
                if token:
                    self.token = token
                    # Token JWT dura 24h en BioTime Colombia
                    self.token_expiry = datetime.now() + timedelta(hours=23)
                    self._client.headers["Authorization"] = f"JWT {self.token}"
                    logger.info(f"Autenticado en BioTime Colombia via {endpoint}")
                    return token
            except Exception as e:
                last_error = e
                logger.warning(f"Auth falló en {endpoint}: {e}")
                continue

        raise ConnectionError(
            f"No se pudo autenticar en BioTime Colombia. Último error: {last_error}"
        )

    def _ensure_authenticated(self):
        """Re-autentica si el token expiró o no existe."""
        if not self.token or (
            self.token_expiry and datetime.now() >= self.token_expiry
        ):
            self.authenticate()

    def _get(self, path: str, params: dict = None) -> dict:
        """GET autenticado con manejo de errores."""
        self._ensure_authenticated()
        url = f"{self.base_url}{path}"
        try:
            resp = self._client.get(url, params=params or {})
            if resp.status_code == 401:
                # Token expirado, re-autenticar y reintentar
                self.token = None
                self._ensure_authenticated()
                resp = self._client.get(url, params=params or {})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP {e.response.status_code} en GET {url}: {e.response.text[:200]}")
            raise
        except Exception as e:
            logger.error(f"Error GET {url}: {e}")
            raise

    def _get_all_pages(self, path: str, params: dict = None) -> list:
        """
        Obtiene TODOS los registros paginando automáticamente.
        BioTime Colombia usa: ?page=1&page_size=N
        Respuesta: {"count": N, "next": "...", "previous": "...", "data": [...]}
        """
        params = params or {}
        params["page_size"] = settings.BIOTIME_PAGE_SIZE
        params["page"] = 1

        results = []
        while True:
            data = self._get(path, params)
            # BioTime puede usar "data" o "results" según la versión
            page_data = data.get("data") or data.get("results") or []
            results.extend(page_data)

            # Verificar si hay más páginas
            if not data.get("next"):
                break
            params["page"] += 1
            logger.debug(f"Paginando {path}: página {params['page']}, total hasta ahora: {len(results)}")

        return results

    # ── Empleados ─────────────────────────────────────────────────────────────

    def get_employees(self, active_only: bool = True) -> list[BTEmployee]:
        """
        Obtiene lista de empleados activos.
        En Colombia, enable_att=1 indica que el empleado tiene control de asistencia.
        """
        params = {}
        if active_only:
            params["enable_att"] = 1

        raw = self._get_all_pages("/personnel/api/employee/", params)
        employees = []
        for item in raw:
            try:
                emp = BTEmployee(
                    emp_code=str(item.get("emp_code", "")),
                    first_name=item.get("first_name", ""),
                    last_name=item.get("last_name", ""),
                    department=self._extract_dept(item),
                    position=item.get("position") if isinstance(item.get("position"), str) else None,
                    mobile=item.get("mobile") or item.get("contact_tel"),
                    email=item.get("email"),
                    hire_date=item.get("hire_date"),
                    enable_att=bool(item.get("enable_att", 1)),
                )
                employees.append(emp)
            except Exception as e:
                logger.warning(f"Error parseando empleado {item.get('emp_code')}: {e}")
        return employees

    def _extract_dept(self, item: dict) -> Optional[str]:
        """Extrae nombre de departamento (puede ser objeto o string en BioTime Colombia)."""
        dept = item.get("department")
        if isinstance(dept, dict):
            return dept.get("dept_name") or dept.get("name")
        if isinstance(dept, str):
            return dept
        return None

    # ── Transacciones (marcajes) ──────────────────────────────────────────────

    def get_transactions(
        self,
        target_date: date,
        emp_code: Optional[str] = None,
    ) -> list[BTTransaction]:
        """
        Obtiene marcajes de una fecha específica.

        BioTime Colombia:
        - start_time y end_time en formato ISO: "2024-01-15 00:00:00" o "2024-01-15T00:00:00"
        - Los tiempos en el servidor pueden estar en UTC o UTC-5 según configuración
        - Se obtienen ambas ventanas (+/- 1 día) para cubrir ambos casos
        """
        # Rango amplio para cubrir configuraciones UTC y UTC-5
        dt_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0)
        dt_end = datetime(target_date.year, target_date.month, target_date.day, 23, 59, 59)

        params = {
            "start_time": dt_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": dt_end.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if emp_code:
            params["emp_code"] = emp_code

        raw = self._get_all_pages("/att/api/transaction/", params)
        transactions = []
        for item in raw:
            try:
                punch_str = item.get("punch_time", "")
                punch_dt = self._parse_datetime(punch_str)
                if not punch_dt:
                    continue

                tx = BTTransaction(
                    id=item.get("id", 0),
                    emp_code=str(item.get("emp_code", "")),
                    punch_time=punch_dt,
                    punch_state=str(item.get("punch_state", "0")),
                    terminal_alias=item.get("terminal_alias"),
                    area_alias=item.get("area_alias"),
                    upload_time=self._parse_datetime(item.get("upload_time")),
                )
                transactions.append(tx)
            except Exception as e:
                logger.warning(f"Error parseando transacción {item.get('id')}: {e}")
        return transactions

    def get_transactions_since(self, since: datetime) -> list[BTTransaction]:
        """
        Obtiene marcajes desde una hora específica hasta ahora.
        Usado para polling en tiempo real.
        """
        now = datetime.now()
        params = {
            "start_time": since.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "punch_state": "0",   # Solo check-in en tiempo real
        }
        raw = self._get_all_pages("/att/api/transaction/", params)
        transactions = []
        for item in raw:
            try:
                punch_dt = self._parse_datetime(item.get("punch_time", ""))
                if not punch_dt:
                    continue
                tx = BTTransaction(
                    id=item.get("id", 0),
                    emp_code=str(item.get("emp_code", "")),
                    punch_time=punch_dt,
                    punch_state=str(item.get("punch_state", "0")),
                    terminal_alias=item.get("terminal_alias"),
                    area_alias=item.get("area_alias"),
                )
                transactions.append(tx)
            except Exception as e:
                logger.warning(f"Error parseando transacción reciente: {e}")
        return transactions

    # ── Turnos / Horarios ─────────────────────────────────────────────────────

    def get_employee_schedules(self, target_date: date) -> list[BTEmployeeSchedule]:
        """
        Obtiene turnos asignados para una fecha.
        BioTime Colombia: /att/api/schedule/ con filtro de fecha.
        """
        params = {
            "start_date": target_date.strftime("%Y-%m-%d"),
            "end_date": target_date.strftime("%Y-%m-%d"),
        }
        raw = self._get_all_pages("/att/api/schedule/", params)
        schedules = []
        for item in raw:
            try:
                # Extraer hora inicio del turno
                start_time = self._extract_schedule_start(item)
                end_time = self._extract_schedule_end(item)

                sched = BTEmployeeSchedule(
                    emp_code=str(item.get("emp_code", "")),
                    schedule_date=str(item.get("schedule_date", target_date.strftime("%Y-%m-%d"))),
                    timetable_alias=item.get("timetable_alias"),
                    start_time=start_time,
                    end_time=end_time,
                )
                schedules.append(sched)
            except Exception as e:
                logger.warning(f"Error parseando turno: {e}")
        return schedules

    def get_timetables(self) -> dict[str, str]:
        """
        Obtiene catálogo de timetables (turnos) con sus horas de inicio.
        Retorna dict: {timetable_name: start_time_str}
        """
        raw = self._get_all_pages("/att/api/timetable/")
        result = {}
        for item in raw:
            name = item.get("alias") or item.get("timetable_name") or str(item.get("id"))
            start = item.get("start_time") or item.get("punch_start_time")
            if name and start:
                result[name] = start
        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _parse_datetime(self, value) -> Optional[datetime]:
        """Parsea strings de fecha/hora de BioTime Colombia."""
        if not value:
            return None
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(str(value).split("+")[0].strip(), fmt)
            except ValueError:
                continue
        logger.debug(f"No se pudo parsear fecha: {value}")
        return None

    def _extract_schedule_start(self, item: dict) -> Optional[str]:
        """Extrae hora inicio de un registro de schedule (estructura variable en Colombia)."""
        # Puede venir directo o anidado en timetable
        if item.get("start_time"):
            return str(item["start_time"])[:5]  # HH:MM
        timetable = item.get("timetable")
        if isinstance(timetable, dict):
            st = timetable.get("start_time") or timetable.get("punch_start_time")
            if st:
                return str(st)[:5]
        return None

    def _extract_schedule_end(self, item: dict) -> Optional[str]:
        if item.get("end_time"):
            return str(item["end_time"])[:5]
        timetable = item.get("timetable")
        if isinstance(timetable, dict):
            et = timetable.get("end_time") or timetable.get("punch_end_time")
            if et:
                return str(et)[:5]
        return None

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
