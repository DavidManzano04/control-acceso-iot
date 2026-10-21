# Firmware del lector

Cada nodo es un ESP32 conectado a un lector de tarjetas **Hikvision por Wiegand 34**. Hay un nodo de **entrada** y otro de **salida**; los dos envían cada lectura al servidor por MQTT y muestran la respuesta en el lector y en una pantalla LCD.

La plataforma que recibe las lecturas está en [plataforma-web](../plataforma-web).

## Qué hace

- Lee la tarjeta por Wiegand 34 (interrupciones en D0/D1, validación de paridad y filtro de ruido)
- Firma cada lectura con **HMAC-SHA256** y un nonce, y la publica por **MQTT sobre TLS** (HiveMQ Cloud)
- Recibe la respuesta del servidor y la muestra con el LED y el pito del lector, LEDs RGB, buzzer y LCD 20x4
- **Modo sin conexión:** si no hay red, guarda las lecturas en LittleFS y las envía cuando vuelve la conexión
- Prueba varias redes WiFi conocidas y se conecta a la de mejor señal
- Hora sincronizada por NTP (Colombia, UTC-5)
- **Configuración remota** y **actualización de firmware por OTA** desde el dashboard
- Envía un heartbeat con su estado (red, señal, versión, lecturas pendientes)

## Hardware

| Componente | Conexión |
| --- | --- |
| Lector Hikvision D0 (verde) | GPIO 32, por divisor 10k/20k (5 V → 3,3 V) |
| Lector Hikvision D1 (blanco) | GPIO 33, por divisor 10k/20k |
| LED verde del lector (OK) | GPIO 16 → ULN2003 IN2 |
| LED rojo del lector (ERR) | GPIO 17 → ULN2003 IN3 |
| Pito del lector (BEEP) | GPIO 4 → ULN2003 IN1 |
| LCD I2C 20x4 | SDA 21, SCL 22 |
| Servo | GPIO 13 |
| LED RGB | R 25, G 26, B 27 |
| Buzzer | GPIO 14 |

## Tópicos MQTT

| Tópico | Dirección | Uso |
| --- | --- | --- |
| `acceso/{device_id}/scan` | nodo → servidor | Lectura de tarjeta firmada |
| `acceso/{device_id}/resultado` | servidor → nodo | Permitido o denegado |
| `acceso/{device_id}/status` | nodo → servidor | Heartbeat |
| `acceso/{device_id}/config` | servidor → nodo | Configuración remota |
| `acceso/{device_id}/ota` | servidor → nodo | Orden de actualización |
| `acceso/{device_id}/offline` | nodo → servidor | Lecturas guardadas sin conexión |

## Cómo compilarlo

Necesitas [PlatformIO](https://platformio.org/) (en VS Code o similar). Abre la carpeta `firmware` como proyecto.

1. Copia `data/config.example.json` como `data/config.json` y llena tu WiFi, las credenciales de HiveMQ y el `device_secret` del lector. El `config.json` no se sube al repositorio.
2. Para el nodo de salida, cambia `device_id` y `rol` a `"salida"` y usa su propio `device_secret`.
3. Conecta el ESP32 y dale **Upload**. El `config.json` va incrustado en el firmware, así que no hace falta subir el sistema de archivos aparte.

## Tecnologías

C++ · Arduino framework · PlatformIO · ESP32 · Wiegand · MQTT/TLS · HMAC-SHA256 · LittleFS · ArduinoJson
