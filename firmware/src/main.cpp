// ─────────────────────────────────────────────────────────────────────────────
// main.cpp v5.0.0 — Lector RFID de asistencia docente (UCEVA)
// ESP32 + lector Hikvision (Wiegand 34) + LCD I2C 20x4 + Servo + LEDs + Buzzer + MQTT/TLS
// Cada ESP32 es un lector de ENTRADA o de SALIDA según su config.json.
// Incluye OTA, configuración remota y modo sin conexión (cola en LittleFS).
// ─────────────────────────────────────────────────────────────────────────────

#include "mbedtls/md.h"
#include <ArduinoJson.h>
#include <ESP32Servo.h>
#include <HTTPClient.h>
#include <LiquidCrystal_I2C.h>
#include <LittleFS.h>
#include <PubSubClient.h>
#include <Update.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <Wire.h>
#include <time.h>
#include "soc/gpio_reg.h"
#include "soc/soc.h"

#define FIRMWARE_VERSION "5.1.0"

// ─── PINES ────────────────────────────────────────────────────────────────────
// Lector Hikvision por Wiegand, entrando por divisor 10k / 20k (5 V -> 3,3 V):
//   D0 (cable verde)  -> GPIO 32
//   D1 (cable blanco) -> GPIO 33
// Respuesta al lector por el módulo ULN2003 (GPIO en HIGH = línea del lector a GND):
//   OK   (cable marrón, LED verde) -> IN2 <- GPIO 16 (RX2)
//   ERR  (cable naranja, LED rojo) -> IN3 <- GPIO 17 (TX2)
//   BEEP (cable morado, pito)      -> IN1 <- GPIO 4
// LCD I2C: SDA=21, SCL=22
#define WG_D0_PIN 32
#define WG_D1_PIN 33
#define LECTOR_OK_PIN 16
#define LECTOR_ERR_PIN 17
#define LECTOR_BEEP_PIN 4
#define SERVO_PIN 13
#define LED_R_PIN 25
#define LED_G_PIN 26
#define LED_B_PIN 27
#define BUZZER_PIN 14

// ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
struct Config {
  // Redes WiFi conocidas: prueba una y, si no conecta, pasa a la siguiente
  static const int MAX_REDES = 6;
  String redes_ssid[MAX_REDES];
  String redes_pass[MAX_REDES];
  int nRedes = 0;
  String mqtt_broker;
  int mqtt_port;
  String mqtt_user;
  String mqtt_password;
  String device_id;
  String device_secret;
  String rol;               // "entrada" o "salida" (solo para lo que muestra la LCD)
  int gate_open_ms;
  int mqtt_timeout_ms;
  bool abrir_sin_conexion;  // abrir la talanquera cuando no hay servidor
  bool wg_invertir_bytes;   // invierte el orden de bytes del número de tarjeta
};

Config cfg;
const char *RUTA_CONFIG = "/config.json";
const char *RUTA_COLA = "/pendientes.jsonl";
const char *RUTA_HUELLA = "/config_build.txt";

// data/config.json va incrustado en el firmware (board_build.embed_txtfiles).
// Así basta con darle Upload para que los cambios del config.json lleguen al ESP32.
extern const uint8_t config_embebido_inicio[] asm("_binary_data_config_json_start");
extern const uint8_t config_embebido_fin[] asm("_binary_data_config_json_end");

// LittleFS.exists() en el core 2.0.x imprime un error cuando el archivo no
// existe; esta versión recorre la raíz y no ensucia el monitor.
bool existeArchivo(const char *ruta) {
  File raiz = LittleFS.open("/");
  if (!raiz) return false;
  const char *buscado = ruta[0] == '/' ? ruta + 1 : ruta;
  File f = raiz.openNextFile();
  while (f) {
    String n = f.name();
    if (n.startsWith("/")) n = n.substring(1);
    f.close();
    if (n == buscado) { raiz.close(); return true; }
    f = raiz.openNextFile();
  }
  raiz.close();
  return false;
}

// Si el config.json del proyecto cambió desde la última carga, reemplaza el del
// ESP32. Si no cambió, se respeta lo que se haya configurado por MQTT.
void sincronizarConfigEmbebida() {
  size_t largo = config_embebido_fin - config_embebido_inicio;
  while (largo > 0 && config_embebido_inicio[largo - 1] == 0) largo--;
  if (largo == 0) return;

  uint32_t h = 2166136261u;  // FNV-1a
  for (size_t i = 0; i < largo; i++) {
    h ^= config_embebido_inicio[i];
    h *= 16777619u;
  }
  String huella = String(h, HEX);

  String anterior = "";
  if (existeArchivo(RUTA_HUELLA)) {
    File f = LittleFS.open(RUTA_HUELLA, "r");
    anterior = f.readString();
    f.close();
    anterior.trim();
  }
  if (anterior == huella) return;

  JsonDocument prueba;
  if (deserializeJson(prueba, (const char *)config_embebido_inicio, largo)) {
    Serial.println("[FS] El config.json del proyecto tiene un error de formato, no se copió");
    return;
  }
  File w = LittleFS.open(RUTA_CONFIG, "w");
  if (!w) return;
  w.write(config_embebido_inicio, largo);
  w.close();
  File hf = LittleFS.open(RUTA_HUELLA, "w");
  if (hf) {
    hf.print(huella);
    hf.close();
  }
  Serial.println("[FS] config.json nuevo copiado desde el proyecto");
}

void agregarRed(const String &ssid, const String &pass) {
  if (ssid.length() == 0 || cfg.nRedes >= Config::MAX_REDES) return;
  for (int i = 0; i < cfg.nRedes; i++)
    if (cfg.redes_ssid[i] == ssid) return;  // ya estaba
  cfg.redes_ssid[cfg.nRedes] = ssid;
  cfg.redes_pass[cfg.nRedes] = pass;
  cfg.nRedes++;
}

// Redes de respaldo que se agregan aunque el config.json no las traiga
void agregarRedesPorDefecto() {
  // Agrega aquí redes de respaldo si las necesitas, por ejemplo:
  // agregarRed("MI_RED", "MI_CLAVE");
}

void cargarDefaults() {
  cfg.nRedes = 0;
  cfg.mqtt_broker = "";        // se toma de data/config.json
  cfg.mqtt_port = 8883;
  cfg.mqtt_user = "";
  cfg.mqtt_password = "";
  cfg.device_id = "entrada";
  cfg.device_secret = "";
  cfg.rol = "entrada";
  cfg.gate_open_ms = 4000;
  cfg.mqtt_timeout_ms = 5000;
  cfg.abrir_sin_conexion = false;
  cfg.wg_invertir_bytes = false;
}

void aplicarSettings(JsonObject s) {
  if (s.isNull()) return;
  if (s["gate_open_ms"].is<int>()) cfg.gate_open_ms = s["gate_open_ms"].as<int>();
  if (s["mqtt_timeout_ms"].is<int>()) cfg.mqtt_timeout_ms = s["mqtt_timeout_ms"].as<int>();
  if (s["abrir_sin_conexion"].is<bool>()) cfg.abrir_sin_conexion = s["abrir_sin_conexion"].as<bool>();
  if (s["wg_invertir_bytes"].is<bool>()) cfg.wg_invertir_bytes = s["wg_invertir_bytes"].as<bool>();
}

bool cargarConfig() {
  cargarDefaults();
  if (!LittleFS.begin(true)) {
    Serial.println("[FS] Error montando LittleFS, se usan valores por defecto");
    agregarRedesPorDefecto();
    return false;
  }
  sincronizarConfigEmbebida();
  if (!LittleFS.exists(RUTA_CONFIG)) {
    Serial.println("[FS] config.json no existe, se usan valores por defecto");
    agregarRedesPorDefecto();
    return false;
  }
  File f = LittleFS.open(RUTA_CONFIG, "r");
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) {
    Serial.println("[FS] config.json inválido: " + String(err.c_str()));
    agregarRedesPorDefecto();
    return false;
  }

  // "redes": [{ssid, password}, ...]  y/o  "client": {ssid, password} (formato anterior)
  for (JsonObject r : doc["network"]["redes"].as<JsonArray>()) agregarRed(r["ssid"] | "", r["password"] | "");
  agregarRed(doc["network"]["client"]["ssid"] | "", doc["network"]["client"]["password"] | "");
  if (doc["mqtt"]["broker"].is<const char *>()) cfg.mqtt_broker = doc["mqtt"]["broker"].as<String>();
  if (doc["mqtt"]["port"].is<int>()) cfg.mqtt_port = doc["mqtt"]["port"].as<int>();
  if (doc["mqtt"]["user"].is<const char *>()) cfg.mqtt_user = doc["mqtt"]["user"].as<String>();
  if (doc["mqtt"]["password"].is<const char *>()) cfg.mqtt_password = doc["mqtt"]["password"].as<String>();
  if (doc["system"]["device_id"].is<const char *>()) cfg.device_id = doc["system"]["device_id"].as<String>();
  if (doc["system"]["device_secret"].is<const char *>()) cfg.device_secret = doc["system"]["device_secret"].as<String>();

  // Rol: se toma del config o se deduce del device_id
  if (doc["system"]["rol"].is<const char *>()) cfg.rol = doc["system"]["rol"].as<String>();
  else cfg.rol = (cfg.device_id.indexOf("salida") >= 0) ? "salida" : "entrada";

  aplicarSettings(doc["settings"].as<JsonObject>());

  agregarRedesPorDefecto();
  String lista = "";
  for (int i = 0; i < cfg.nRedes; i++) lista += (i ? ", " : "") + cfg.redes_ssid[i];
  Serial.println("[FS] Config cargada: " + cfg.device_id + " (" + cfg.rol + "), redes WiFi: " + lista);
  return true;
}

// Mezcla parámetros sueltos en el config.json sin tocar WiFi, MQTT ni identidad
bool guardarSettings(JsonObject nuevos) {
  JsonDocument doc;
  if (LittleFS.exists(RUTA_CONFIG)) {
    File f = LittleFS.open(RUTA_CONFIG, "r");
    deserializeJson(doc, f);
    f.close();
  }
  for (JsonPair kv : nuevos) doc["settings"][kv.key()] = kv.value();
  File w = LittleFS.open(RUTA_CONFIG, "w");
  if (!w) return false;
  serializeJson(doc, w);
  w.close();
  return true;
}

bool guardarConfigCompleta(const String &json) {
  File f = LittleFS.open(RUTA_CONFIG, "w");
  if (!f) return false;
  f.print(json);
  f.close();
  return true;
}

// ─── OBJETOS ──────────────────────────────────────────────────────────────────
LiquidCrystal_I2C lcd(0x27, 20, 4);
WiFiClientSecure espClient;
PubSubClient mqttClient(espClient);
Servo servo;

String respDecision = "";
String respNombre = "";
String respMensaje = "";
String respHora = "";

unsigned long ultimoHeartbeat = 0;
unsigned long ultimoIntentoWifi = 0;
unsigned long ultimoIntentoMqtt = 0;
unsigned long ultimoEnvioCola = 0;
unsigned long ultimoRefrescoLcd = 0;
bool pantallaReposo = false;
bool estabaConectado = false;

// ─── LCD ──────────────────────────────────────────────────────────────────────
String centrar(String texto) {
  if (texto.length() > 20) texto = texto.substring(0, 20);
  int espacios = (20 - (int)texto.length()) / 2;
  String r = "";
  for (int i = 0; i < espacios; i++) r += " ";
  return r + texto;
}

void lcdMensaje(String l1, String l2 = "", String l3 = "", String l4 = "") {
  lcd.clear();
  lcd.setCursor(0, 0); lcd.print(l1);
  lcd.setCursor(0, 1); lcd.print(l2);
  lcd.setCursor(0, 2); lcd.print(l3);
  lcd.setCursor(0, 3); lcd.print(l4);
}

// ─── HORA (NTP, Colombia UTC-5) ───────────────────────────────────────────────
bool horaValida() {
  struct tm t;
  return getLocalTime(&t, 10) && (t.tm_year + 1900) >= 2024;
}

String marcaTiempo() {
  struct tm t;
  if (!getLocalTime(&t, 10)) return "";
  char b[20];
  strftime(b, sizeof(b), "%Y-%m-%d %H:%M:%S", &t);
  return String(b);
}

String horaCorta() {
  struct tm t;
  if (!horaValida() || !getLocalTime(&t, 10)) return "";
  char b[6];
  strftime(b, sizeof(b), "%H:%M", &t);
  return String(b);
}

// ─── SEGURIDAD: HMAC-SHA256 ───────────────────────────────────────────────────
String generarHMAC(const String &mensaje) {
  byte resultado[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char *)cfg.device_secret.c_str(), cfg.device_secret.length());
  mbedtls_md_hmac_update(&ctx, (const unsigned char *)mensaje.c_str(), mensaje.length());
  mbedtls_md_hmac_finish(&ctx, resultado);
  mbedtls_md_free(&ctx);
  String hex = "";
  char s[3];
  for (int i = 0; i < 32; i++) {
    sprintf(s, "%02x", resultado[i]);
    hex += s;
  }
  return hex;
}

String nuevoNonce() { return String((uint32_t)esp_random()) + String(millis()); }

// ─── ACTUADORES ───────────────────────────────────────────────────────────────
void abrir() { servo.write(90); }
void cerrar() { servo.write(0); delay(400); }
// Los LEDs y el buzzer locales se repiten en el lector Hikvision:
// verde -> OK, rojo -> ERR, pitido -> BEEP. Azul no tiene equivalente en el lector.
void ledOff() {
  digitalWrite(LED_R_PIN, LOW); digitalWrite(LED_G_PIN, LOW); digitalWrite(LED_B_PIN, LOW);
  digitalWrite(LECTOR_OK_PIN, LOW); digitalWrite(LECTOR_ERR_PIN, LOW);
}
void ledVerde() { ledOff(); digitalWrite(LED_G_PIN, HIGH); digitalWrite(LECTOR_OK_PIN, HIGH); }
void ledRojo() { ledOff(); digitalWrite(LED_R_PIN, HIGH); digitalWrite(LECTOR_ERR_PIN, HIGH); }
void ledAzul() { ledOff(); digitalWrite(LED_B_PIN, HIGH); }
void beep(int veces, int ms) {
  for (int i = 0; i < veces; i++) {
    digitalWrite(BUZZER_PIN, HIGH); digitalWrite(LECTOR_BEEP_PIN, HIGH); delay(ms);
    digitalWrite(BUZZER_PIN, LOW); digitalWrite(LECTOR_BEEP_PIN, LOW); delay(80);
  }
}

// ─── COLA SIN CONEXIÓN ────────────────────────────────────────────────────────
int contarPendientes() {
  if (!existeArchivo(RUTA_COLA)) return 0;
  File f = LittleFS.open(RUTA_COLA, "r");
  int n = 0;
  while (f.available()) {
    String l = f.readStringUntil('\n');
    l.trim();
    if (l.length()) n++;
  }
  f.close();
  return n;
}

void encolarRegistro(const String &uid) {
  JsonDocument d;
  String nonce = nuevoNonce();
  d["rfid"] = uid;
  d["ts"] = horaValida() ? marcaTiempo() : "";
  d["nonce"] = nonce;
  d["signature"] = generarHMAC(uid + nonce);
  File f = LittleFS.open(RUTA_COLA, "a");
  if (!f) return;
  serializeJson(d, f);
  f.print("\n");
  f.close();
  Serial.println("[COLA] Registro guardado sin conexión: " + uid);
}

// Envía hasta 10 registros pendientes por llamada y conserva el resto
void enviarPendientes() {
  if (!mqttClient.connected() || !existeArchivo(RUTA_COLA)) return;
  String topic = "acceso/" + cfg.device_id + "/offline";
  File f = LittleFS.open(RUTA_COLA, "r");
  String resto = "";
  int enviados = 0;
  while (f.available()) {
    String l = f.readStringUntil('\n');
    l.trim();
    if (!l.length()) continue;
    if (enviados < 10 && mqttClient.publish(topic.c_str(), l.c_str())) {
      enviados++;
      mqttClient.loop();
      delay(40);
    } else {
      resto += l + "\n";
    }
  }
  f.close();
  if (resto.length() == 0) {
    LittleFS.remove(RUTA_COLA);
  } else {
    File w = LittleFS.open(RUTA_COLA, "w");
    w.print(resto);
    w.close();
  }
  if (enviados) Serial.printf("[COLA] %d registros enviados al servidor\n", enviados);
}

// ─── PANTALLA EN REPOSO ───────────────────────────────────────────────────────
void mostrarReposo() {
  String titulo = (cfg.rol == "salida") ? "REGISTRO DE SALIDA" : "REGISTRO DE ENTRADA";
  String linea3 = horaCorta();
  String linea4;
  if (!mqttClient.connected()) {
    int p = contarPendientes();
    linea4 = p > 0 ? "Sin red: " + String(p) + " en cola" : "Modo sin conexion";
  } else {
    linea4 = "Acerque su carnet";
  }
  lcdMensaje(centrar("UCEVA Docentes"), centrar(titulo), centrar(linea3), centrar(linea4));
  pantallaReposo = true;
  ultimoRefrescoLcd = millis();
}

// ─── WIFI Y MQTT SIN BLOQUEAR EL LECTOR ───────────────────────────────────────
void onMessage(char *topic, byte *payload, unsigned int length);

int redActual = 0;

void iniciarRed(int i) {
  redActual = i;
  Serial.println("[WiFi] Probando red " + cfg.redes_ssid[i]);
  WiFi.disconnect();
  WiFi.begin(cfg.redes_ssid[i].c_str(), cfg.redes_pass[i].c_str());
}

// Busca cuáles redes conocidas están al alcance y deja primero la de mejor señal
int mejorRedVisible() {
  int n = WiFi.scanNetworks();
  int mejor = -1, mejorRssi = -1000;
  for (int k = 0; k < n; k++) {
    for (int i = 0; i < cfg.nRedes; i++) {
      if (WiFi.SSID(k) == cfg.redes_ssid[i] && WiFi.RSSI(k) > mejorRssi) {
        mejor = i;
        mejorRssi = WiFi.RSSI(k);
      }
    }
  }
  WiFi.scanDelete();
  if (mejor >= 0) Serial.printf("[WiFi] Red conocida al alcance: %s (%d dBm)\n", cfg.redes_ssid[mejor].c_str(), mejorRssi);
  return mejor;
}

void conectarWifiInicial() {
  WiFi.mode(WIFI_STA);
  ledAzul();
  if (cfg.nRedes == 0) {
    Serial.println("[WiFi] No hay redes configuradas");
    ledOff();
    return;
  }
  lcdMensaje(centrar("Buscando WiFi"), centrar(String(cfg.nRedes) + " redes conocidas"));
  int primera = mejorRedVisible();
  if (primera < 0) primera = 0;
  // Prueba primero la que se ve mejor y después las demás, 10 s cada una
  for (int intento = 0; intento < cfg.nRedes && WiFi.status() != WL_CONNECTED; intento++) {
    int i = (primera + intento) % cfg.nRedes;
    lcdMensaje(centrar("Conectando WiFi"), centrar(cfg.redes_ssid[i]));
    iniciarRed(i);
    unsigned long inicio = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - inicio < 10000) {
      delay(500);
      Serial.print(".");
    }
    Serial.println();
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("[WiFi] Conectado a " + WiFi.SSID() + ", IP " + WiFi.localIP().toString());
    configTime(-5 * 3600, 0, "pool.ntp.org", "time.google.com");
  } else {
    Serial.println("[WiFi] Sin conexión, el lector sigue funcionando en modo sin conexión");
  }
  ledOff();
}

bool intentarMqtt() {
  espClient.setInsecure();
  espClient.setHandshakeTimeout(8);
  mqttClient.setSocketTimeout(6);
  mqttClient.setServer(cfg.mqtt_broker.c_str(), cfg.mqtt_port);
  mqttClient.setCallback(onMessage);
  mqttClient.setBufferSize(4096);
  String clientId = "esp32_" + cfg.device_id;
  if (mqttClient.connect(clientId.c_str(), cfg.mqtt_user.c_str(), cfg.mqtt_password.c_str())) {
    mqttClient.subscribe(("acceso/" + cfg.device_id + "/resultado").c_str());
    mqttClient.subscribe(("acceso/" + cfg.device_id + "/ota").c_str());
    mqttClient.subscribe(("acceso/" + cfg.device_id + "/config").c_str());
    Serial.println("[MQTT] Conectado como " + clientId);
    return true;
  }
  Serial.printf("[MQTT] Falló la conexión (estado %d)\n", mqttClient.state());
  return false;
}

void mantenerConexion() {
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - ultimoIntentoWifi > 10000) {
      ultimoIntentoWifi = millis();
      // Cada 10 s sin conexión pasa a la siguiente red de la lista
      if (cfg.nRedes > 0) iniciarRed((redActual + 1) % cfg.nRedes);
    }
  } else if (!mqttClient.connected()) {
    if (millis() - ultimoIntentoMqtt > 5000) {
      ultimoIntentoMqtt = millis();
      if (!horaValida()) configTime(-5 * 3600, 0, "pool.ntp.org", "time.google.com");
      intentarMqtt();
    }
  }

  bool conectado = mqttClient.connected();
  if (conectado != estabaConectado) {
    estabaConectado = conectado;
    if (pantallaReposo) mostrarReposo();
  }
}

// ─── HEARTBEAT ────────────────────────────────────────────────────────────────
void enviarHeartbeat() {
  if (!mqttClient.connected()) return;
  JsonDocument doc;
  doc["device_id"] = cfg.device_id;
  doc["rol"] = cfg.rol;
  doc["version"] = FIRMWARE_VERSION;
  doc["ip"] = WiFi.localIP().toString();
  doc["ssid"] = WiFi.SSID();
  doc["rssi"] = WiFi.RSSI();
  doc["uptime"] = millis() / 1000;
  doc["gate_open_ms"] = cfg.gate_open_ms;
  doc["mqtt_timeout_ms"] = cfg.mqtt_timeout_ms;
  doc["pendientes"] = contarPendientes();
  doc["lector"] = "wiegand";
  doc["wg_invertir_bytes"] = cfg.wg_invertir_bytes;
  char buf[512];
  serializeJson(doc, buf);
  mqttClient.publish(("acceso/" + cfg.device_id + "/status").c_str(), buf, true);
}

// ─── OTA ──────────────────────────────────────────────────────────────────────
void realizarOTA(const String &url) {
  Serial.println("[OTA] Descargando " + url);
  lcdMensaje(centrar("ACTUALIZANDO"), centrar("Firmware remoto"), centrar("No desconectar"), centrar("Espere..."));
  ledAzul();
  HTTPClient http;
  WiFiClient client;
  http.begin(client, url);
  int code = http.GET();
  if (code != 200) {
    Serial.println("[OTA] Error HTTP " + String(code));
    lcdMensaje(centrar("OTA FALLIDO"), centrar("Error " + String(code)));
    ledRojo(); beep(3, 200); http.end(); delay(2000);
    mostrarReposo();
    return;
  }
  int largo = http.getSize();
  if (largo <= 0 || !Update.begin(largo)) {
    http.end();
    mostrarReposo();
    return;
  }
  size_t escritos = Update.writeStream(*http.getStreamPtr());
  Serial.printf("[OTA] %d de %d bytes\n", escritos, largo);
  if (Update.end() && Update.isFinished()) {
    lcdMensaje(centrar("OTA EXITOSO"), centrar("Reiniciando..."));
    ledVerde(); beep(2, 300); delay(1500);
    ESP.restart();
  } else {
    Serial.printf("[OTA] Error: %s\n", Update.errorString());
    ledRojo(); beep(3, 100);
  }
  http.end();
  mostrarReposo();
}

// ─── MENSAJES MQTT ────────────────────────────────────────────────────────────
void onMessage(char *topic, byte *payload, unsigned int length) {
  String msg;
  msg.reserve(length);
  for (unsigned int i = 0; i < length; i++) msg += (char)payload[i];
  String t = String(topic);
  String base = "acceso/" + cfg.device_id;

  JsonDocument doc;
  if (deserializeJson(doc, msg)) return;

  if (t == base + "/resultado") {
    respDecision = doc["decision"] | "deny";
    respNombre = doc["nombre"] | "";
    respMensaje = doc["mensaje"] | "";
    respHora = doc["hora"] | "";
  } else if (t == base + "/ota") {
    String url = doc["url"] | "";
    if (url.length()) realizarOTA(url);
  } else if (t == base + "/config") {
    // Config completa (trae red o MQTT): se reemplaza el archivo y se reinicia.
    if (!doc["network"].isNull() || !doc["mqtt"].isNull() || !doc["system"].isNull()) {
      lcdMensaje(centrar("Nueva configuracion"), centrar("Guardando..."));
      if (guardarConfigCompleta(msg)) {
        lcdMensaje(centrar("Configuracion OK"), centrar("Reiniciando..."));
        ledVerde(); beep(2, 200); delay(1500);
        ESP.restart();
      }
      return;
    }
    // Ajustes sueltos: se aplican en caliente y se guardan sin reiniciar.
    JsonObject s = doc["settings"].isNull() ? doc.as<JsonObject>() : doc["settings"].as<JsonObject>();
    aplicarSettings(s);
    guardarSettings(s);
    enviarHeartbeat();
    Serial.printf("[CONFIG] Puerta %d ms, timeout %d ms, abrir sin red: %d, invertir bytes: %d\n", cfg.gate_open_ms,
                  cfg.mqtt_timeout_ms, cfg.abrir_sin_conexion, cfg.wg_invertir_bytes);
  }
}

// ─── PROCESAR TARJETA ─────────────────────────────────────────────────────────
void procesarTarjeta(const String &uid) {
  Serial.println("[LECTOR] Tarjeta " + uid);
  pantallaReposo = false;
  ledAzul();
  lcdMensaje(centrar(cfg.rol == "salida" ? "SALIDA" : "ENTRADA"), centrar("Verificando..."), "", centrar(uid));

  bool enLinea = mqttClient.connected();
  respDecision = "";

  if (enLinea) {
    String nonce = nuevoNonce();
    JsonDocument d;
    d["device_id"] = cfg.device_id;
    d["rfid"] = uid;
    d["nonce"] = nonce;
    d["signature"] = generarHMAC(uid + nonce);
    char payload[384];
    serializeJson(d, payload);
    enLinea = mqttClient.publish(("acceso/" + cfg.device_id + "/scan").c_str(), payload);

    unsigned long inicio = millis();
    while (enLinea && respDecision == "" && millis() - inicio < (unsigned long)cfg.mqtt_timeout_ms) {
      mqttClient.loop();
      delay(20);
    }
  }

  if (respDecision == "") {
    // Sin servidor: el registro se guarda y se envía cuando vuelva la conexión
    encolarRegistro(uid);
    ledAzul(); beep(2, 120);
    lcdMensaje(centrar("SIN CONEXION"), centrar("Registro guardado"), centrar("Se enviara al volver"), centrar(horaCorta()));
    if (cfg.abrir_sin_conexion) {
      abrir(); delay(cfg.gate_open_ms); cerrar();
    } else {
      delay(2500);
    }
  } else if (respDecision == "permit") {
    bool esSalida = cfg.rol == "salida";
    ledVerde(); beep(1, 180);
    lcdMensaje(centrar(esSalida ? "Hasta pronto" : "Bienvenido/a"), centrar(respNombre), centrar(respMensaje),
               centrar((esSalida ? "Salida " : "Entrada ") + respHora));
    abrir(); delay(cfg.gate_open_ms); cerrar();
  } else if (respDecision == "enroll") {
    ledAzul(); beep(2, 80);
    lcdMensaje(centrar("TARJETA LEIDA"), centrar("Registro desde"), centrar("el panel web"), centrar(uid));
    delay(2000);
  } else {
    ledRojo(); beep(3, 80);
    lcdMensaje(centrar("ACCESO DENEGADO"), centrar(respMensaje.length() ? respMensaje : "No autorizado"), "", centrar("Contacte seguridad"));
    delay(2000);
  }

  ledOff();
  mostrarReposo();
}

// ─── LECTOR WIEGAND (Hikvision) ──────────────────────────────────────────────
// Cada bit llega como un pulso a 0 V de ~50-100 us: un pulso en D0 es un 0 y un
// pulso en D1 es un 1. Las interrupciones solo guardan los bits; el loop arma la
// trama cuando pasan 30 ms sin pulsos.
volatile uint64_t wgBits = 0;
volatile uint8_t wgCuenta = 0;
volatile uint32_t wgUltimoPulso = 0;
volatile uint32_t wgRuido = 0;  // pulsos rechazados por el filtro
portMUX_TYPE wgMux = portMUX_INITIALIZER_UNLOCKED;

// Filtro anti-ruido: un bit válido es un pulso a 0 V en UNA sola línea mientras
// la otra sigue arriba, y separado al menos 200 us del bit anterior. El ruido
// (tierra no común, cables largos, 60 Hz) suele mover las dos líneas a la vez.
// GPIO 32 y 33 se leen directo del registro GPIO_IN1 (bits 0 y 1).
void IRAM_ATTR wgPulso(bool esUno) {
  uint32_t entrada = REG_READ(GPIO_IN1_REG);
  bool d0Alto = entrada & (1UL << (WG_D0_PIN - 32));
  bool d1Alto = entrada & (1UL << (WG_D1_PIN - 32));
  bool propioAlto = esUno ? d1Alto : d0Alto;
  bool otroAlto = esUno ? d0Alto : d1Alto;
  uint32_t ahora = micros();
  portENTER_CRITICAL_ISR(&wgMux);
  if (propioAlto || !otroAlto || (wgCuenta > 0 && ahora - wgUltimoPulso < 200)) {
    wgRuido++;
  } else if (wgCuenta < 64) {
    wgBits = (wgBits << 1) | (esUno ? 1 : 0);
    wgCuenta++;
    wgUltimoPulso = ahora;
  }
  portEXIT_CRITICAL_ISR(&wgMux);
}

void IRAM_ATTR wgPulsoD0() { wgPulso(false); }
void IRAM_ATTR wgPulsoD1() { wgPulso(true); }

// Cada 5 s avisa si llegó ruido, con el voltaje que se ve en las líneas
void reportarRuidoWiegand() {
  static unsigned long ultimo = 0;
  if (millis() - ultimo < 5000) return;
  ultimo = millis();
  portENTER_CRITICAL(&wgMux);
  uint32_t n = wgRuido;
  wgRuido = 0;
  portEXIT_CRITICAL(&wgMux);
  if (n == 0) return;
  Serial.printf("[WIEGAND] %u pulsos de ruido ignorados. En reposo D0=%d D1=%d (deben estar en 1). "
                "Revise GND comun y que el punto medio de cada divisor marque ~3,3 V\n",
                (unsigned int)n, digitalRead(WG_D0_PIN), digitalRead(WG_D1_PIN));
}

void iniciarWiegand() {
  pinMode(WG_D0_PIN, INPUT);
  pinMode(WG_D1_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(WG_D0_PIN), wgPulsoD0, FALLING);
  attachInterrupt(digitalPinToInterrupt(WG_D1_PIN), wgPulsoD1, FALLING);
  Serial.printf("[WIEGAND] Lineas en reposo: D0(GPIO%d)=%d D1(GPIO%d)=%d (deben estar en 1)\n", WG_D0_PIN,
                digitalRead(WG_D0_PIN), WG_D1_PIN, digitalRead(WG_D1_PIN));
}

void descartarWiegand() {
  portENTER_CRITICAL(&wgMux);
  wgBits = 0;
  wgCuenta = 0;
  portEXIT_CRITICAL(&wgMux);
}

// Devuelve true cuando hay una trama completa (30 ms sin pulsos nuevos)
bool tramaWiegand(uint64_t &bits, uint8_t &n) {
  portENTER_CRITICAL(&wgMux);
  n = wgCuenta;
  uint32_t ultimo = wgUltimoPulso;
  portEXIT_CRITICAL(&wgMux);
  if (n == 0 || micros() - ultimo < 30000) return false;
  portENTER_CRITICAL(&wgMux);
  bits = wgBits;
  n = wgCuenta;
  wgBits = 0;
  wgCuenta = 0;
  portEXIT_CRITICAL(&wgMux);
  return true;
}

// Wiegand 34: P(par) + 32 bits de datos + P(impar)
// Wiegand 26: P(par) + 24 bits de datos + P(impar)
// La paridad par cubre la primera mitad de los datos y la impar la segunda.
bool decodificarWiegand(uint64_t bits, uint8_t n, uint32_t &dato) {
  if (n != 34 && n != 26) return false;
  uint8_t largo = n - 2;
  uint8_t mitad = largo / 2;
  dato = (uint32_t)((bits >> 1) & ((1ULL << largo) - 1));
  uint32_t alta = dato >> mitad;
  uint32_t baja = dato & ((1UL << mitad) - 1);
  int bitPar = (bits >> (n - 1)) & 1;
  int bitImpar = bits & 1;
  bool parOk = ((__builtin_popcount(alta) + bitPar) % 2) == 0;
  bool imparOk = ((__builtin_popcount(baja) + bitImpar) % 2) == 1;
  return parOk && imparOk;
}

String hex32(uint32_t v, int digitos) {
  char b[9];
  snprintf(b, sizeof(b), digitos == 6 ? "%06X" : "%08X", (unsigned int)v);
  return String(b);
}

// Número de la tarjeta en el mismo formato que usaba el RC522 (hex en mayúsculas)
String uidDesdeWiegand(uint32_t dato, uint8_t n) {
  if (n == 26) return hex32(dato, 6);
  return hex32(cfg.wg_invertir_bytes ? __builtin_bswap32(dato) : dato, 8);
}

// ─── PRUEBA DE SALIDAS DESDE EL MONITOR SERIAL ───────────────────────────────
// Escriba en el monitor:  ok  |  err  |  beep  |  test  (prueba las tres)
void probarSalida(const char *nombre, int pin, int ms) {
  Serial.printf("[PRUEBA] %s (GPIO %d) en HIGH por %d ms\n", nombre, pin, ms);
  digitalWrite(pin, HIGH);
  delay(ms);
  digitalWrite(pin, LOW);
}

void ejecutarPrueba(const String &c);

void atenderSerial() {
  static String linea = "";
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch != '\r' && ch != '\n') {
      if (linea.length() < 20) linea += ch;
      continue;
    }
    if (linea.length() == 0) continue;
    String c = linea;
    linea = "";
    c.trim();
    c.toLowerCase();
    ejecutarPrueba(c);
  }
}

void ejecutarPrueba(const String &c) {
  if (c == "ok" || c == "test") probarSalida("OK / LED verde (marron)", LECTOR_OK_PIN, 2000);
  if (c == "test") delay(500);
  if (c == "err" || c == "test") probarSalida("ERR / LED rojo (naranja)", LECTOR_ERR_PIN, 2000);
  if (c == "test") delay(500);
  if (c == "beep" || c == "test") {
    for (int i = 0; i < 3; i++) { probarSalida("BEEP / pito (morado)", LECTOR_BEEP_PIN, 300); delay(300); }
  }
}

// ─── SETUP ────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(300);

  Wire.begin(21, 22);
  lcd.init();
  lcd.backlight();
  lcdMensaje(centrar("UCEVA Docentes"), centrar("Control de acceso"), centrar("v" + String(FIRMWARE_VERSION)), centrar("Iniciando..."));

  cargarConfig();

  // Salidas hacia el lector (ULN2003) en reposo antes de cualquier otra cosa
  pinMode(LECTOR_OK_PIN, OUTPUT);
  pinMode(LECTOR_ERR_PIN, OUTPUT);
  pinMode(LECTOR_BEEP_PIN, OUTPUT);
  digitalWrite(LECTOR_OK_PIN, LOW);
  digitalWrite(LECTOR_ERR_PIN, LOW);
  digitalWrite(LECTOR_BEEP_PIN, LOW);

  iniciarWiegand();

  pinMode(LED_R_PIN, OUTPUT);
  pinMode(LED_G_PIN, OUTPUT);
  pinMode(LED_B_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  servo.attach(SERVO_PIN);
  cerrar();
  ledOff();

  conectarWifiInicial();
  if (WiFi.status() == WL_CONNECTED) {
    lcdMensaje(centrar("Conectando MQTT"), centrar("HiveMQ Cloud"));
    intentarMqtt();
    // Espera breve a que el NTP entregue la hora
    for (int i = 0; i < 20 && !horaValida(); i++) delay(150);
  }
  estabaConectado = mqttClient.connected();
  enviarHeartbeat();
  enviarPendientes();

  ledVerde(); beep(1, 120); delay(200); ledOff();
  mostrarReposo();

  Serial.println("[SISTEMA] Firmware v" + String(FIRMWARE_VERSION) + " listo. Lector de " + cfg.rol + " (" + cfg.device_id + ")");
}

// ─── LOOP ─────────────────────────────────────────────────────────────────────
void loop() {
  mantenerConexion();
  if (mqttClient.connected()) mqttClient.loop();

  if (millis() - ultimoHeartbeat > 30000) {
    enviarHeartbeat();
    ultimoHeartbeat = millis();
  }
  if (millis() - ultimoEnvioCola > 3000) {
    enviarPendientes();
    ultimoEnvioCola = millis();
  }
  // Refresca la hora de la pantalla de reposo cada 30 s
  if (pantallaReposo && millis() - ultimoRefrescoLcd > 30000) mostrarReposo();

  reportarRuidoWiegand();
  atenderSerial();

  uint64_t bits;
  uint8_t n;
  if (!tramaWiegand(bits, n)) {
    delay(5);
    return;
  }

  uint32_t dato;
  if (!decodificarWiegand(bits, n, dato)) {
    Serial.printf("[WIEGAND] Trama descartada: %d bits (0x%08X%08X). Revise DIP (Wiegand 34) y cables D0/D1\n", n,
                  (unsigned int)(bits >> 32), (unsigned int)(bits & 0xFFFFFFFF));
    return;
  }

  String uid = uidDesdeWiegand(dato, n);
  if (n == 34) {
    Serial.printf("[WIEGAND] %d bits OK -> %s (orden invertido: %s)\n", n, uid.c_str(),
                  hex32(cfg.wg_invertir_bytes ? dato : __builtin_bswap32(dato), 8).c_str());
  } else {
    Serial.printf("[WIEGAND] %d bits OK -> %s\n", n, uid.c_str());
  }

  // Antirrebote: ignora la misma tarjeta durante 3 s
  static String ultimoUID = "";
  static unsigned long ultimaLectura = 0;
  if (uid == ultimoUID && millis() - ultimaLectura < 3000) return;
  ultimoUID = uid;
  ultimaLectura = millis();

  procesarTarjeta(uid);
  // Lo que llegó mientras se mostraba el resultado es la misma tarjeta otra vez
  descartarWiegand();
}
