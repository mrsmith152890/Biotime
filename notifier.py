"""
Servicio de notificaciones para llegadas tarde.
Canales soportados:
  1. WhatsApp via Evolution API (preferido en Colombia)
  2. Email SMTP

Cada empleado recibe su propia notificación inmediata e individual.
"""

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx
import pytz

from .config import settings
from .models import LateArrival, NotificationResult

logger = logging.getLogger(__name__)
BOGOTA_TZ = pytz.timezone("America/Bogota")


# ── Mensajes ──────────────────────────────────────────────────────────────────

def _build_whatsapp_message(late: LateArrival) -> str:
    """Mensaje WhatsApp individual para el empleado tardío."""
    scheduled = late.scheduled_start.strftime("%I:%M %p")
    actual = late.actual_checkin.strftime("%I:%M %p")
    return (
        f"⏰ *Notificación de llegada tarde*\n\n"
        f"Hola *{late.full_name}*,\n\n"
        f"Se registró su entrada hoy *{late.date}* a las *{actual}*.\n"
        f"Su turno inicia a las *{scheduled}*.\n"
        f"Retraso: *{late.minutes_late} minutos*.\n\n"
        f"Por favor comuníquese con su supervisor si tiene alguna novedad.\n\n"
        f"📍 Terminal: {late.terminal or 'No registrado'}\n"
        f"_MG Technology - Control de Asistencia_"
    )


def _build_admin_whatsapp_message(late: LateArrival) -> str:
    """Mensaje WhatsApp para administradores/RRHH."""
    scheduled = late.scheduled_start.strftime("%I:%M %p")
    actual = late.actual_checkin.strftime("%I:%M %p")
    return (
        f"🚨 *Alerta llegada tarde*\n\n"
        f"👤 Empleado: *{late.full_name}* ({late.emp_code})\n"
        f"🏢 Área: {late.department or 'Sin área'}\n"
        f"📅 Fecha: {late.date}\n"
        f"⏱ Entrada programada: *{scheduled}*\n"
        f"⏱ Entrada real: *{actual}*\n"
        f"⚠️ Retraso: *{late.minutes_late} minutos*\n"
        f"📍 Terminal: {late.terminal or 'No registrado'}\n\n"
        f"_Sistema de Asistencia BioTime Colombia_"
    )


def _build_email_subject(late: LateArrival) -> str:
    return f"[Asistencia] Llegada tarde - {late.full_name} - {late.date}"


def _build_email_html(late: LateArrival) -> str:
    scheduled = late.scheduled_start.strftime("%I:%M %p")
    actual = late.actual_checkin.strftime("%I:%M %p")
    return f"""
    <html><body style="font-family: Arial, sans-serif; color: #333;">
    <div style="max-width:600px; margin:auto; border:1px solid #e0e0e0; border-radius:8px; overflow:hidden;">
      <div style="background:#c0392b; padding:20px; color:white;">
        <h2 style="margin:0;">⏰ Llegada Tarde Detectada</h2>
        <p style="margin:5px 0 0;">Sistema BioTime Colombia</p>
      </div>
      <div style="padding:24px;">
        <table style="width:100%; border-collapse:collapse;">
          <tr><td style="padding:8px; font-weight:bold; width:40%;">Empleado</td>
              <td style="padding:8px;">{late.full_name}</td></tr>
          <tr style="background:#f9f9f9;">
              <td style="padding:8px; font-weight:bold;">Código</td>
              <td style="padding:8px;">{late.emp_code}</td></tr>
          <tr><td style="padding:8px; font-weight:bold;">Área / Departamento</td>
              <td style="padding:8px;">{late.department or '—'}</td></tr>
          <tr style="background:#f9f9f9;">
              <td style="padding:8px; font-weight:bold;">Fecha</td>
              <td style="padding:8px;">{late.date}</td></tr>
          <tr><td style="padding:8px; font-weight:bold;">Entrada programada</td>
              <td style="padding:8px;">{scheduled}</td></tr>
          <tr style="background:#f9f9f9;">
              <td style="padding:8px; font-weight:bold;">Entrada real</td>
              <td style="padding:8px; color:#c0392b; font-weight:bold;">{actual}</td></tr>
          <tr><td style="padding:8px; font-weight:bold;">Retraso</td>
              <td style="padding:8px; color:#c0392b; font-weight:bold;">{late.minutes_late} minutos</td></tr>
          <tr style="background:#f9f9f9;">
              <td style="padding:8px; font-weight:bold;">Terminal</td>
              <td style="padding:8px;">{late.terminal or '—'}</td></tr>
        </table>
      </div>
      <div style="background:#f5f5f5; padding:16px; text-align:center; font-size:12px; color:#888;">
        Generado automáticamente por MG Technology · Control de Asistencia BioTime Colombia
      </div>
    </div>
    </body></html>
    """


# ── Canal WhatsApp ────────────────────────────────────────────────────────────

class WhatsAppNotifier:
    """Envía mensajes via Evolution API."""

    def __init__(self):
        self.base_url = (settings.EVOLUTION_API_URL or "").rstrip("/")
        self.api_key = settings.EVOLUTION_API_KEY or ""
        self.instance = settings.EVOLUTION_INSTANCE or ""
        self._client = httpx.Client(timeout=15)

    def send(self, phone: str, message: str) -> bool:
        """
        Envía mensaje a un número.
        phone: formato colombiano sin + (ej: 573001234567)
        """
        if not settings.WHATSAPP_ENABLED:
            logger.info(f"[WhatsApp MOCK] → {phone}: {message[:60]}...")
            return True

        url = f"{self.base_url}/message/sendText/{self.instance}"
        payload = {
            "number": phone,
            "text": message,
        }
        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json",
        }
        try:
            resp = self._client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            logger.info(f"WhatsApp enviado a {phone}")
            return True
        except Exception as e:
            logger.error(f"Error enviando WhatsApp a {phone}: {e}")
            return False

    def close(self):
        self._client.close()


# ── Canal Email ───────────────────────────────────────────────────────────────

class EmailNotifier:
    """Envía correos via SMTP."""

    def send(self, to_email: str, subject: str, html_body: str) -> bool:
        if not settings.EMAIL_ENABLED:
            logger.info(f"[Email MOCK] → {to_email}: {subject}")
            return True

        if not all([settings.SMTP_HOST, settings.SMTP_USER, settings.SMTP_PASSWORD]):
            logger.warning("Email no configurado (faltan SMTP_HOST, SMTP_USER, SMTP_PASSWORD)")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
            msg["To"] = to_email
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            if settings.SMTP_TLS:
                server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT)
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT)

            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.sendmail(msg["From"], [to_email], msg.as_string())
            server.quit()
            logger.info(f"Email enviado a {to_email}")
            return True
        except Exception as e:
            logger.error(f"Error enviando email a {to_email}: {e}")
            return False


# ── Servicio principal ────────────────────────────────────────────────────────

class NotificationService:
    """
    Orquesta el envío de notificaciones por llegada tarde.
    Cada empleado recibe su notificación individual e inmediata.
    """

    def __init__(self):
        self.whatsapp = WhatsAppNotifier()
        self.email = EmailNotifier()

    def notify_late_arrival(self, late: LateArrival) -> list[NotificationResult]:
        """
        Notifica a UN empleado específico que llegó tarde.
        Se llama de forma inmediata cuando se detecta el retraso.
        Retorna lista de resultados por canal utilizado.
        """
        results = []
        now_bogota = datetime.now(BOGOTA_TZ)

        logger.info(
            f"Notificando llegada tarde: {late.full_name} ({late.emp_code}) "
            f"- {late.minutes_late} min tarde"
        )

        # ── 1. WhatsApp al empleado (si tiene número) ─────────────────────────
        if late.mobile:
            emp_phone = late.mobile.strip().replace(" ", "").replace("-", "").replace("+", "")
            if emp_phone.startswith("3") and len(emp_phone) == 10:
                emp_phone = f"57{emp_phone}"
            if len(emp_phone) >= 10:
                msg = _build_whatsapp_message(late)
                ok = self.whatsapp.send(emp_phone, msg)
                results.append(NotificationResult(
                    emp_code=late.emp_code,
                    full_name=late.full_name,
                    channel="whatsapp_empleado",
                    success=ok,
                    message=f"Enviado a {emp_phone}" if ok else "Falló envío",
                    timestamp=now_bogota,
                ))
                late.notified_whatsapp = ok

        # ── 2. WhatsApp a administradores ─────────────────────────────────────
        admin_msg = _build_admin_whatsapp_message(late)
        for admin_phone in settings.admin_phone_list:
            ok = self.whatsapp.send(admin_phone, admin_msg)
            results.append(NotificationResult(
                emp_code=late.emp_code,
                full_name=late.full_name,
                channel="whatsapp_admin",
                success=ok,
                message=f"Admin {admin_phone}",
                timestamp=now_bogota,
            ))

        # ── 3. Email al empleado (si tiene correo) ────────────────────────────
        if late.email:
            subject = _build_email_subject(late)
            html = _build_email_html(late)
            ok = self.email.send(late.email, subject, html)
            results.append(NotificationResult(
                emp_code=late.emp_code,
                full_name=late.full_name,
                channel="email_empleado",
                success=ok,
                message=f"Enviado a {late.email}" if ok else "Falló envío",
                timestamp=now_bogota,
            ))
            late.notified_email = ok

        # ── 4. Email a administradores ────────────────────────────────────────
        subject = _build_email_subject(late)
        html = _build_email_html(late)
        for admin_email in settings.admin_email_list:
            ok = self.email.send(admin_email, subject, html)
            results.append(NotificationResult(
                emp_code=late.emp_code,
                full_name=late.full_name,
                channel="email_admin",
                success=ok,
                message=f"Admin {admin_email}",
                timestamp=now_bogota,
            ))

        if not results:
            logger.warning(
                f"Empleado {late.full_name} no tiene contacto registrado "
                f"y no hay admins configurados. Solo se registra en logs."
            )

        return results

    def close(self):
        self.whatsapp.close()
