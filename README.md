# Control de acceso IoT con RFID — UCEVA

Trabajo de grado **"Sistema inteligente de gestión y control de acceso al campus universitario de la UCEVA basado en tecnología IoT y de identificación automática"** (Ingeniería Electrónica, Unidad Central del Valle del Cauca).

El sistema registra la entrada y salida de los docentes de tiempo completo y ocasionales con lectores RFID, y verifica en tiempo real si cumplen su horario.

## Cómo funciona

```
Lector de ENTRADA ─┐                                           ┌─> SQLite
 (ESP32+Hikvision) │                                           │
                   ├──MQTT/TLS──> HiveMQ Cloud ──> Backend Flask
Lector de SALIDA  ─┘     ^                            │        │
 (ESP32+Hikvision)       └── permitido / denegado ────┘        └─> Dashboard web en vivo
                                                                   (Socket.IO)
```

1. El lector lee la tarjeta por Wiegand, firma la lectura con HMAC-SHA256 y la publica por MQTT sobre TLS.
2. El backend valida la firma, identifica al docente y compara la hora con su horario.
3. El lector muestra la respuesta (LED, pito y LCD) y el registro aparece al instante en el dashboard.
4. Si se cae la red, el lector guarda las lecturas y las envía cuando vuelve la conexión.

## Estructura

| Carpeta | Contenido |
| --- | --- |
| [`firmware/`](firmware) | Firmware del nodo lector: ESP32, lector Hikvision por Wiegand 34, MQTT/TLS, OTA y modo sin conexión (C++, PlatformIO) |
| [`plataforma-web/`](plataforma-web) | Backend y dashboard: gestión de docentes y horarios, asistencia en tiempo real, reportes y administración de lectores (Python, Flask) |

Cada carpeta tiene su propio README con los detalles y cómo ejecutarla.

## Tecnologías

**Hardware:** ESP32 · lector RFID Hikvision (Wiegand 34) · LCD I2C · PCB propia diseñada en KiCad
**Firmware:** C++ · Arduino framework · PlatformIO · LittleFS · ArduinoJson
**Comunicación:** MQTT sobre TLS · HiveMQ Cloud · HMAC-SHA256
**Backend y web:** Python · Flask · Flask-SocketIO · SQLite · JWT · HTML/CSS/JavaScript

> Este repositorio no incluye credenciales, la base de datos ni datos personales del prototipo. Cada parte trae un archivo de ejemplo para configurar los tuyos.

## Autores

- David Felipe Manzano Aldana
- David Santiago Vallejo Rodríguez

Ingeniería Electrónica — Unidad Central del Valle del Cauca (UCEVA), 2026.
