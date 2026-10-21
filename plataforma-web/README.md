# Plataforma web

Backend y dashboard del sistema. Recibe las lecturas de los [lectores](../firmware), las valida y muestra todo en tiempo real.

## Funciones

- Dashboard en tiempo real: docentes en el campus, movimiento por hora, últimos registros y estado de los lectores
- Gestión de docentes, horarios y tarjetas RFID (con modo de enrolamiento)
- Reportes de asistencia y cumplimiento de horario, exportables a CSV
- Inicio de sesión con JWT, contraseñas con PBKDF2 y límite de intentos fallidos
- Administración de lectores: configuración remota y actualización de firmware por OTA
- Registro de auditoría de las acciones de los administradores

## Tecnologías

Python · Flask · Flask-SocketIO · paho-mqtt · SQLite · PyJWT · HTML/CSS/JavaScript · HiveMQ Cloud · ESP32

## Cómo ejecutarlo

```bash
cd plataforma-web
python -m venv .venv
.venv\Scripts\activate          # en Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # en Linux/Mac: cp .env.example .env
# edita .env con tus credenciales de HiveMQ y los secretos de los lectores
python app.py
```

Abre `http://localhost:5000`. La primera vez se crea el usuario `admin`; cambia su contraseña con `python cambiar_clave.py`.

> Este repositorio no incluye la base de datos, las fotos ni las credenciales reales del prototipo.
