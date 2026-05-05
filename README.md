# BioTime Colombia 8.0 — Monitor de Llegadas Tarde
**MG Technology** | Versión Colombia (America/Bogota)

---

## ¿Qué hace?

Monitorea BioTime 8.0 en **tiempo real** y envía una notificación **individual e inmediata** por cada empleado que llega tarde.

| Empleado       | Estado        | Acción                                    |
|----------------|---------------|-------------------------------------------|
| Juan Pérez     | Llegó a tiempo | ✅ Sin notificación                        |
| María García   | Llegó 18 min tarde | 🚨 Notificación WhatsApp + Email a María y admins |
| Carlos López   | Llegó 32 min tarde | 🚨 Notificación WhatsApp + Email a Carlos y admins |

---

## Estructura del proyecto

```
biotime_colombia/
├── app/
│   ├── __init__.py
│   ├── config.py          # Variables de entorno
│   ├── models.py          # Modelos Pydantic
│   ├── biotime_client.py  # Cliente API BioTime Colombia
│   ├── detector.py        # Lógica de detección de retrasos
│   ├── notifier.py        # WhatsApp (Evolution API) + Email
│   ├── monitor.py         # Monitor en tiempo real (asyncio)
│   └── main.py            # FastAPI endpoints
├── logs/                  # Logs automáticos
├── .env.example
├── requirements.txt
├── Dockerfile
└── biotime-colombia.service  # Systemd
```

---

## Instalación rápida

### Opción 1: Python directo (Ubuntu/Debian)

```bash
# 1. Crear entorno virtual
cd /srv/biotime_colombia
python3 -m venv venv
source venv/bin/activate

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Configurar
cp .env.example .env
nano .env   # Editar con tus valores

# 4. Iniciar
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### Opción 2: Systemd (producción)

```bash
# Copiar servicio
sudo cp biotime-colombia.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable biotime-colombia
sudo systemctl start biotime-colombia

# Ver logs
journalctl -u biotime-colombia -f
```

### Opción 3: Docker

```bash
# Construir imagen
docker build -t biotime-colombia .

# Correr
docker run -d \
  --name biotime-colombia \
  --env-file .env \
  -p 8080:8080 \
  -v ./logs:/srv/biotime_colombia/logs \
  biotime-colombia
```

---

## Configuración (.env)

| Variable | Descripción | Ejemplo |
|---|---|---|
| `BIOTIME_BASE_URL` | URL servidor BioTime Colombia | `http://192.168.1.100:8000` |
| `BIOTIME_USERNAME` | Usuario BioTime | `admin` |
| `BIOTIME_PASSWORD` | Contraseña BioTime | `admin123` |
| `GRACE_PERIOD_MINUTES` | Minutos de gracia (Colombia: 15) | `15` |
| `POLL_INTERVAL_SECONDS` | Frecuencia de consulta | `60` |
| `MONITOR_START_TIME` | Inicio ventana monitoreo | `05:30` |
| `MONITOR_END_TIME` | Fin ventana monitoreo | `12:00` |
| `WHATSAPP_ENABLED` | Activar WhatsApp | `true` |
| `EVOLUTION_API_URL` | URL Evolution API | `https://...` |
| `EVOLUTION_API_KEY` | API Key Evolution | `...` |
| `EVOLUTION_INSTANCE` | Instancia WhatsApp | `empresa` |
| `EMAIL_ENABLED` | Activar email | `true` |
| `SMTP_HOST` | Servidor SMTP | `smtp.gmail.com` |
| `ADMIN_PHONES` | Teléfonos admins (col. format) | `573001234567` |
| `ADMIN_EMAILS` | Correos admins | `rrhh@empresa.com` |
| `API_KEY` | Clave seguridad API | `clave_secreta` |

---

## Endpoints disponibles

```
Documentación interactiva: http://servidor:8080/docs
```

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| GET | `/health` | Estado del servicio |
| GET | `/monitor/status` | Estado del monitor en tiempo real |
| POST | `/monitor/start` | Inicia el monitor |
| POST | `/monitor/stop` | Detiene el monitor |
| GET | `/report/today` | Reporte del día actual |
| GET | `/report/date/2025-05-02` | Reporte de fecha específica |
| POST | `/check/now` | Revisión inmediata manual |
| GET | `/notifications/log` | Log de notificaciones |
| GET | `/employees/today` | Estado de empleados hoy |

Todos los endpoints requieren header: `X-API-Key: tu_clave`

---

## Diferencias BioTime Colombia vs Internacional

1. **Autenticación**: Intenta `/jwt/api-token-auth/` primero, luego `/api-token-auth/`
2. **Zona horaria**: `America/Bogota` (UTC-5, sin cambio de hora)
3. **Campos empleado**: Puede incluir `document_number` (cédula colombiana)
4. **SSL**: Instalaciones colombianas frecuentemente usan certificados self-signed
5. **Turnos**: Soporte para jornada diurna/nocturna según Código Sustantivo del Trabajo
6. **Tolerancia**: 15 minutos por defecto (práctica habitual colombiana)
7. **Estructura de schedule**: Puede venir el turno anidado o plano según versión del servidor

---

## Flujo del monitor

```
Servidor arranca
      │
      ▼
  Carga empleados + turnos del día (BioTime Colombia)
      │
      ▼  cada 60 seg (configurable)
  ┌─────────────────────────────────────┐
  │  ¿Hay nuevos check-ins en BioTime? │
  └────────────────┬────────────────────┘
                   │
        Por cada CHECK-IN nuevo:
                   │
          ┌────────▼─────────┐
          │ ¿Tiene turno?    │──NO──► Ignorar
          └────────┬─────────┘
                   │ SÍ
          ┌────────▼─────────────────┐
          │ Entrada ≤ turno + gracia │──SÍ──► ✅ A tiempo, sin notificación
          └────────┬─────────────────┘
                   │ NO (llegó tarde)
          ┌────────▼──────────────────────────┐
          │ 🚨 Notificación INMEDIATA:         │
          │   - WhatsApp al empleado           │
          │   - WhatsApp a admins              │
          │   - Email al empleado              │
          │   - Email a admins                 │
          └───────────────────────────────────┘
```

---

## Soporte
**MG Technology** — soporte.mgtechnology.co
