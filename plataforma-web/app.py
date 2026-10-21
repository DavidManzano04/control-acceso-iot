# ─────────────────────────────────────────────────────────────────────────────
# app.py v4.0.1 — Control de acceso y asistencia docente (UCEVA)
# Flask + Socket.IO + MQTT (HiveMQ) + SQLite
# Identificación únicamente por RFID. Un lector de entrada y uno de salida.
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, request, jsonify, render_template_string, g, send_from_directory
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash
import paho.mqtt.client as mqtt
import sqlite3, os, json, threading, ssl, hmac, hashlib, csv, io, unicodedata, secrets, time
from datetime import datetime, timedelta, date
import jwt as pyjwt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Las credenciales se leen de variables de entorno o de un archivo .env (ver .env.example).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"))
except ImportError:
    pass

def cargar_secreto():
    """Clave para firmar los JWT. Prioridad: variable de entorno ACCESO_JWT_SECRET;
    si no existe, se lee de jwt_secret.key, y si tampoco existe se genera una al azar
    y se guarda ahí (así las sesiones sobreviven a reinicios). Nunca va en el código."""
    env = os.environ.get("ACCESO_JWT_SECRET", "").strip()
    if len(env) >= 32:
        return env
    ruta = os.path.join(BASE_DIR, "jwt_secret.key")
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as fh:
            guardado = fh.read().strip()
        if len(guardado) >= 32:
            return guardado
    nuevo = secrets.token_hex(32)
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(nuevo)
    print("[SEGURIDAD] Se generó una clave nueva para los tokens en jwt_secret.key (no la subas a GitHub).")
    return nuevo

app = Flask(__name__)
app.config["SECRET_KEY"] = cargar_secreto()
# async_mode="threading": eventlet/gevent están instalados en el venv y Flask-SocketIO
# los elegiría solo; con ellos los emit() hechos desde el hilo de MQTT (paho) no
# llegan al navegador hasta recargar. Con hilos normales sí se envían al instante.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Enrolamiento: mientras está activo, la siguiente tarjeta leída se envía al dashboard
modo_registro_activo = False

# ─── MQTT ────────────────────────────────────────────────────────────────────
MQTT_BROKER   = os.environ.get("ACCESO_MQTT_BROKER", "")
MQTT_PORT     = int(os.environ.get("ACCESO_MQTT_PORT", "8883"))
MQTT_USER     = os.environ.get("ACCESO_MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("ACCESO_MQTT_PASSWORD", "")
TOPIC_RESULT_BASE = "acceso/{}/resultado"

# Lectores oficiales del prototipo. El secreto debe coincidir con el config.json de cada ESP32.
DISPOSITIVOS_BASE = [
    ("entrada", "Entrada principal", "entrada", "Portería principal", os.environ.get("ACCESO_SECRET_ENTRADA", "")),
    ("salida",  "Salida principal",  "salida",  "Portería principal", os.environ.get("ACCESO_SECRET_SALIDA", "")),
]

# ─── JWT dashboard ───────────────────────────────────────────────────────────
JWT_SECRET     = app.config["SECRET_KEY"]
JWT_EXPIRY_H   = 8
DASHBOARD_USER = "admin"
CLAVE_INICIAL  = os.environ.get("ACCESO_CLAVE_INICIAL", "admin123")   # solo se usa si la base no tiene ningún usuario; cámbiala con cambiar_clave.py
HASH_METODO    = "pbkdf2:sha256:600000"

# Rutas de la API que no piden sesión: el login y la descarga de firmware que hace el ESP32 por OTA
RUTAS_PUBLICAS = ("/api/login", "/api/firmware/")

# Freno a la fuerza bruta: máximo 5 intentos fallidos por IP en 5 minutos
LOGIN_MAX_FALLOS = 5
LOGIN_VENTANA_S  = 300
fallos_login = {}
fallos_lock = threading.Lock()

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
AJUSTES_DEFAULT = {"tolerancia_entrada_min": "10", "tolerancia_salida_min": "10", "meta_horas_semana": "40"}

# ─── Base de datos ───────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "acceso.db")

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn

def columnas(c, tabla):
    return [r[1] for r in c.execute(f"PRAGMA table_info({tabla})").fetchall()]

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS docentes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        codigo TEXT NOT NULL UNIQUE,
        nombre TEXT NOT NULL,
        correo TEXT,
        facultad TEXT,
        programa TEXT,
        vinculacion TEXT DEFAULT 'tiempo_completo',
        rfid TEXT UNIQUE,
        foto TEXT DEFAULT 'sin_foto.png',
        activo INTEGER DEFAULT 1,
        consentimiento INTEGER DEFAULT 0,
        consentimiento_fecha TEXT,
        creado_en TEXT DEFAULT (datetime('now','localtime'))
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS horarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        docente_id INTEGER NOT NULL,
        dia INTEGER NOT NULL,            -- 0 = lunes ... 6 = domingo
        hora_inicio TEXT NOT NULL,       -- HH:MM
        hora_fin TEXT NOT NULL,          -- HH:MM
        FOREIGN KEY (docente_id) REFERENCES docentes(id) ON DELETE CASCADE
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS dispositivos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL UNIQUE,
        nombre TEXT NOT NULL,
        tipo TEXT NOT NULL,
        ubicacion TEXT,
        device_secret TEXT,
        activo INTEGER DEFAULT 1,
        ultimo_ping TEXT,
        creado_en TEXT DEFAULT (datetime('now','localtime'))
    )""")
    for col, tipo in [("ip_address", "TEXT"), ("gate_open_ms", "INTEGER"), ("mqtt_timeout_ms", "INTEGER"),
                      ("pos_x", "REAL"), ("pos_y", "REAL"), ("firmware", "TEXT"), ("pendientes", "INTEGER")]:
        if col not in columnas(c, "dispositivos"):
            c.execute(f"ALTER TABLE dispositivos ADD COLUMN {col} {tipo}")

    c.execute("""CREATE TABLE IF NOT EXISTS registros (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rfid TEXT NOT NULL,
        docente_id INTEGER,
        tipo TEXT NOT NULL,              -- entrada | salida
        decision TEXT NOT NULL,          -- permit | deny
        nota TEXT,                       -- texto corto: Llegada puntual, Tarde 12 min, etc.
        offline INTEGER DEFAULT 0,       -- 1 si llegó desde la cola sin conexión del lector
        dispositivo_id INTEGER,
        fecha_hora TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (docente_id) REFERENCES docentes(id) ON DELETE SET NULL,
        FOREIGN KEY (dispositivo_id) REFERENCES dispositivos(id) ON DELETE SET NULL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_registros_fecha ON registros(fecha_hora)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_registros_docente ON registros(docente_id, fecha_hora)")

    c.execute("""CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT)""")
    for k, v in AJUSTES_DEFAULT.items():
        c.execute("INSERT OR IGNORE INTO ajustes (clave, valor) VALUES (?, ?)", (k, v))

    c.execute("""CREATE TABLE IF NOT EXISTS auditoria (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT NOT NULL,
        accion TEXT NOT NULL,
        detalle TEXT,
        ip TEXT,
        fecha_hora TEXT DEFAULT (datetime('now','localtime'))
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS usuarios_dashboard (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        rol TEXT DEFAULT 'admin',
        activo INTEGER DEFAULT 1,
        creado_en TEXT DEFAULT (datetime('now','localtime'))
    )""")
    # Antes esto era INSERT OR REPLACE y volvía a poner admin123 en cada arranque.
    # Ahora solo crea el usuario inicial si la tabla está vacía.
    if not c.execute("SELECT 1 FROM usuarios_dashboard LIMIT 1").fetchone():
        c.execute("INSERT INTO usuarios_dashboard (username,password_hash,rol) VALUES (?,?,?)",
                  (DASHBOARD_USER, generate_password_hash(CLAVE_INICIAL, method=HASH_METODO), "admin"))
    for u in c.execute("SELECT username, password_hash FROM usuarios_dashboard WHERE activo=1").fetchall():
        if verificar_clave(CLAVE_INICIAL, u[1]):
            print(f"[SEGURIDAD] El usuario '{u[0]}' sigue con la clave por defecto. Cámbiala con: python cambiar_clave.py")

    # Lectores oficiales: se crean si no existen y se asegura su secreto y tipo
    for dev_id, nombre, tipo, ubic, secreto in DISPOSITIVOS_BASE:
        existe = c.execute("SELECT id FROM dispositivos WHERE device_id=?", (dev_id,)).fetchone()
        if existe:
            c.execute("UPDATE dispositivos SET tipo=?, device_secret=? WHERE device_id=?", (tipo, secreto, dev_id))
        else:
            c.execute("INSERT INTO dispositivos (device_id,nombre,tipo,ubicacion,device_secret) VALUES (?,?,?,?,?)",
                      (dev_id, nombre, tipo, ubic, secreto))

    conn.commit()
    migrar_estudiantes_a_docentes(conn)
    conn.close()
    print("OK: Base de datos lista (docentes, horarios, registros).")

def migrar_estudiantes_a_docentes(conn):
    """Una sola vez: convierte los usuarios de prueba (estudiantes con RFID) en docentes
    para que las tarjetas físicas sigan funcionando. No borra la tabla de estudiantes."""
    c = conn.cursor()
    if c.execute("SELECT COUNT(*) FROM docentes").fetchone()[0] > 0:
        return
    tablas = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "estudiantes" not in tablas:
        return
    filas = c.execute("SELECT codigo, nombre, carrera, rfid, foto, activo FROM estudiantes").fetchall()
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for f in filas:
        c.execute("""INSERT OR IGNORE INTO docentes
            (codigo, nombre, facultad, programa, vinculacion, rfid, foto, activo, consentimiento, consentimiento_fecha)
            VALUES (?,?,?,?,?,?,?,?,1,?)""",
            (f["codigo"], f["nombre"], "Facultad de Ingenierías", f["carrera"], "tiempo_completo",
             f["rfid"] or None, f["foto"] or "sin_foto.png", f["activo"], ahora))
        doc = c.execute("SELECT id FROM docentes WHERE codigo=?", (f["codigo"],)).fetchone()
        if doc:
            for dia in range(5):
                c.execute("INSERT INTO horarios (docente_id, dia, hora_inicio, hora_fin) VALUES (?,?,?,?)", (doc["id"], dia, "08:00", "12:00"))
                c.execute("INSERT INTO horarios (docente_id, dia, hora_inicio, hora_fin) VALUES (?,?,?,?)", (doc["id"], dia, "14:00", "18:00"))
    conn.commit()
    if filas:
        print(f"[MIGRACIÓN] {len(filas)} usuarios de prueba convertidos en docentes con horario L-V 8-12 y 14-18.")

# ─── Utilidades ──────────────────────────────────────────────────────────────
def leer_ajustes(conn):
    d = dict(AJUSTES_DEFAULT)
    for r in conn.execute("SELECT clave, valor FROM ajustes").fetchall():
        d[r["clave"]] = r["valor"]
    return {
        "tolerancia_entrada_min": int(d["tolerancia_entrada_min"]),
        "tolerancia_salida_min": int(d["tolerancia_salida_min"]),
        "meta_horas_semana": float(d["meta_horas_semana"]),
    }

def a_minutos(hhmm):
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)

def hora_str(minutos):
    return f"{int(minutos) // 60:02d}:{int(minutos) % 60:02d}"

def solo_ascii(texto, largo=20):
    """La LCD 20x4 no muestra tildes: se envía el texto sin acentos y recortado."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return t[:largo]

def nombre_corto(nombre):
    p = (nombre or "").split()
    if len(p) >= 4: return f"{p[0]} {p[2]}"
    if len(p) >= 2: return f"{p[0]} {p[1]}"
    return nombre or ""

def duracion_txt(minutos):
    minutos = int(round(minutos))
    h, m = divmod(minutos, 60)
    if h and m: return f"{h} h {m} min"
    if h: return f"{h} h"
    return f"{m} min"

def registrar_auditoria(usuario, accion, detalle=None, ip=None):
    try:
        conn = get_db()
        conn.execute("INSERT INTO auditoria (usuario,accion,detalle,ip) VALUES (?,?,?,?)",
                     (usuario, accion, json.dumps(detalle, ensure_ascii=False) if detalle else None, ip))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[AUDITORÍA] Error: {e}")

def horario_de(conn, docente_id):
    rows = conn.execute("SELECT dia, hora_inicio, hora_fin FROM horarios WHERE docente_id=? ORDER BY dia, hora_inicio",
                        (docente_id,)).fetchall()
    return [{"dia": r["dia"], "inicio": r["hora_inicio"], "fin": r["hora_fin"]} for r in rows]

def horas_semana(horario):
    return round(sum(max(0, a_minutos(b["fin"]) - a_minutos(b["inicio"])) for b in horario) / 60, 2)

def validar_horario(horario):
    limpio = []
    for b in horario or []:
        try:
            dia = int(b["dia"])
            ini, fin = b["inicio"][:5], b["fin"][:5]
            if not (0 <= dia <= 6) or a_minutos(fin) <= a_minutos(ini):
                return None, f"El bloque del {DIAS[dia] if 0 <= dia <= 6 else 'día'} termina antes de empezar."
            limpio.append({"dia": dia, "inicio": ini, "fin": fin})
        except Exception:
            return None, "Hay un bloque de horario con formato inválido."
    limpio.sort(key=lambda b: (b["dia"], b["inicio"]))
    for a, b in zip(limpio, limpio[1:]):
        if a["dia"] == b["dia"] and a_minutos(b["inicio"]) < a_minutos(a["fin"]):
            return None, f"Hay bloques que se cruzan el {DIAS[a['dia']].lower()}."
    return limpio, None

def guardar_horario(conn, docente_id, horario):
    conn.execute("DELETE FROM horarios WHERE docente_id=?", (docente_id,))
    for b in horario:
        conn.execute("INSERT INTO horarios (docente_id, dia, hora_inicio, hora_fin) VALUES (?,?,?,?)",
                     (docente_id, b["dia"], b["inicio"], b["fin"]))

# ─── Cálculo de asistencia ───────────────────────────────────────────────────
def calcular_dia(bloques, eventos, fecha, ajustes, ahora=None):
    """
    bloques: [{'inicio':'08:00','fin':'12:00'}] del día
    eventos: [{'tipo':'entrada'|'salida','min': minutos desde 00:00}] permitidos, ordenados
    Devuelve estado y métricas de cumplimiento del día.
    """
    ahora = ahora or datetime.now()
    es_hoy = fecha == ahora.date()
    min_ahora = ahora.hour * 60 + ahora.minute
    tol_e, tol_s = ajustes["tolerancia_entrada_min"], ajustes["tolerancia_salida_min"]

    prog_ini = min((a_minutos(b["inicio"]) for b in bloques), default=None)
    prog_fin = max((a_minutos(b["fin"]) for b in bloques), default=None)
    prog_min = sum(a_minutos(b["fin"]) - a_minutos(b["inicio"]) for b in bloques)

    entradas = [e["min"] for e in eventos if e["tipo"] == "entrada"]
    salidas = [e["min"] for e in eventos if e["tipo"] == "salida"]
    primera_entrada = min(entradas) if entradas else None
    ultima_salida = max(salidas) if salidas else None

    # Intervalos en campus: se emparejan entradas y salidas en orden
    intervalos, dentro_desde = [], None
    for e in eventos:
        if e["tipo"] == "entrada" and dentro_desde is None:
            dentro_desde = e["min"]
        elif e["tipo"] == "salida" and dentro_desde is not None:
            intervalos.append((dentro_desde, max(dentro_desde, e["min"])))
            dentro_desde = None
    en_campus = dentro_desde is not None and es_hoy
    if en_campus:
        intervalos.append((dentro_desde, max(dentro_desde, min_ahora)))
    sin_salida = dentro_desde is not None and not es_hoy
    trabajado = sum(f - i for i, f in intervalos)
    # Tiempo cumplido: solo la parte de cada intervalo que cae dentro de los bloques programados
    cumplido = 0
    for b in bloques:
        bi, bf = a_minutos(b["inicio"]), a_minutos(b["fin"])
        for i, f in intervalos:
            cumplido += max(0, min(f, bf) - max(i, bi))

    retraso = max(0, primera_entrada - prog_ini) if (primera_entrada is not None and prog_ini is not None) else 0
    tarde = prog_ini is not None and primera_entrada is not None and retraso > tol_e
    anticipada = 0
    if prog_fin is not None and ultima_salida is not None and not en_campus and ultima_salida < prog_fin - tol_s:
        anticipada = prog_fin - ultima_salida

    if not bloques:
        estado = "sin_horario" if not eventos else "sin_horario_presente"
    elif primera_entrada is None:
        if fecha > ahora.date() or (es_hoy and min_ahora <= prog_ini + tol_e):
            estado = "por_llegar"
        elif es_hoy and min_ahora < prog_fin:
            estado = "sin_llegar"
        else:
            estado = "ausente"
    else:
        estado = "tarde" if tarde else "puntual"

    cumplimiento = round(min(cumplido / prog_min, 1) * 100) if prog_min else None
    return {
        "estado": estado,
        "en_campus": en_campus,
        "sin_salida": sin_salida,
        "entrada": hora_str(primera_entrada) if primera_entrada is not None else None,
        "salida": hora_str(ultima_salida) if ultima_salida is not None else None,
        "programado_inicio": hora_str(prog_ini) if prog_ini is not None else None,
        "programado_fin": hora_str(prog_fin) if prog_fin is not None else None,
        "minutos_programados": prog_min,
        "minutos_trabajados": int(trabajado),
        "minutos_cumplidos": int(cumplido),
        "minutos_retraso": int(retraso) if tarde else 0,
        "minutos_salida_anticipada": int(anticipada),
        "cumplimiento": cumplimiento,
        "registros": len(eventos),
        # Para dibujar el horario: tramos reales en el campus y la entrada que quedó sin salida
        "intervalos": [[hora_str(i), hora_str(f)] for i, f in intervalos],
        "entrada_sin_salida": hora_str(dentro_desde) if sin_salida else None,
    }

def eventos_por_docente(conn, desde, hasta):
    rows = conn.execute("""
        SELECT docente_id, tipo, fecha_hora FROM registros
        WHERE decision='permit' AND docente_id IS NOT NULL
          AND date(fecha_hora) BETWEEN ? AND ?
        ORDER BY fecha_hora
    """, (desde.isoformat(), hasta.isoformat())).fetchall()
    out = {}
    for r in rows:
        dt = datetime.strptime(r["fecha_hora"], "%Y-%m-%d %H:%M:%S")
        out.setdefault((r["docente_id"], dt.date()), []).append({"tipo": r["tipo"], "min": dt.hour * 60 + dt.minute})
    return out

def asistencia_dia(conn, fecha, ahora=None):
    ajustes = leer_ajustes(conn)
    docentes = conn.execute("SELECT * FROM docentes WHERE activo=1 ORDER BY nombre").fetchall()
    eventos = eventos_por_docente(conn, fecha, fecha)
    resultado = []
    for d in docentes:
        bloques = [b for b in horario_de(conn, d["id"]) if b["dia"] == fecha.weekday()]
        info = calcular_dia(bloques, eventos.get((d["id"], fecha), []), fecha, ajustes, ahora)
        info.update({"codigo": d["codigo"], "nombre": d["nombre"], "facultad": d["facultad"],
                     "programa": d["programa"], "foto": d["foto"]})
        resultado.append(info)
    return resultado

def mensaje_lcd(conn, docente, tipo, fecha_hora):
    """Texto corto que el lector muestra en la LCD después de registrar."""
    ajustes = leer_ajustes(conn)
    dt = datetime.strptime(fecha_hora, "%Y-%m-%d %H:%M:%S")
    bloques = [b for b in horario_de(conn, docente["id"]) if b["dia"] == dt.weekday()]
    previos = conn.execute("""SELECT tipo, fecha_hora FROM registros
        WHERE docente_id=? AND decision='permit' AND date(fecha_hora)=? AND fecha_hora < ?
        ORDER BY fecha_hora""", (docente["id"], dt.date().isoformat(), fecha_hora)).fetchall()
    m = dt.hour * 60 + dt.minute
    tol_e, tol_s = ajustes["tolerancia_entrada_min"], ajustes["tolerancia_salida_min"]
    rangos = sorted((a_minutos(b["inicio"]), a_minutos(b["fin"])) for b in bloques)
    if tipo == "entrada":
        if not rangos:
            return "Reingreso" if any(p["tipo"] == "entrada" for p in previos) else "Sin horario hoy"
        # Bloque al que llega: el primero que aún no termina
        bloque = next(((i, f) for i, f in rangos if f > m), None)
        if any(p["tipo"] == "entrada" for p in previos):
            if bloque and m - bloque[0] > tol_e:
                return f"Regreso tarde {duracion_txt(m - bloque[0])}"
            return "Reingreso"
        retraso = m - rangos[0][0]
        if retraso > tol_e:
            return f"Tarde {duracion_txt(retraso)}"
        return "Llegada puntual"
    # Salida: es anticipada solo si ocurre dentro de un bloque, antes de su fin menos la tolerancia
    if any(i <= m < f - tol_s for i, f in rangos):
        return "Salida anticipada"
    return "Salida registrada"

# ─── JWT ─────────────────────────────────────────────────────────────────────
def generar_token(username, rol):
    from datetime import timezone
    now = datetime.now(timezone.utc)
    return pyjwt.encode({"sub": username, "rol": rol, "exp": now + timedelta(hours=JWT_EXPIRY_H), "iat": now},
                        JWT_SECRET, algorithm="HS256")

def verificar_token(token):
    """Devuelve el contenido del JWT si es válido y no ha vencido; si no, None."""
    if not token:
        return None
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except pyjwt.PyJWTError:
        return None
    # El usuario tiene que seguir existiendo y activo (si lo desactivan, su token deja de servir)
    conn = get_db()
    u = conn.execute("SELECT rol FROM usuarios_dashboard WHERE username=? AND activo=1", (payload["sub"],)).fetchone()
    conn.close()
    if not u:
        return None
    payload["rol"] = u["rol"]
    return payload

def token_de_request():
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else ""

def no_autorizado():
    return jsonify({"ok": False, "msg": "Sesión no válida o vencida. Inicia sesión de nuevo."}), 401

@app.before_request
def exigir_sesion():
    """Toda la API pide sesión, salvo RUTAS_PUBLICAS. Así ninguna ruta nueva queda abierta por olvido."""
    if request.method == "OPTIONS" or not request.path.startswith("/api/"):
        return None
    if any(request.path == r or (r.endswith("/") and request.path.startswith(r)) for r in RUTAS_PUBLICAS):
        return None
    payload = verificar_token(token_de_request())
    if not payload:
        return no_autorizado()
    g.usuario, g.rol = payload["sub"], payload["rol"]
    return None

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not getattr(g, "usuario", None):
            payload = verificar_token(token_de_request())
            if not payload:
                return no_autorizado()
            g.usuario, g.rol = payload["sub"], payload["rol"]
        return f(*args, **kwargs)
    return decorated

def verificar_clave(clave, guardado):
    """Acepta hashes nuevos (pbkdf2/scrypt) y el SHA-256 viejo sin sal, solo para poder migrarlo."""
    if not guardado:
        return False
    if guardado.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(guardado, clave)
    return hmac.compare_digest(guardado, hashlib.sha256(clave.encode()).hexdigest())

def login_bloqueado(ip):
    ahora = time.time()
    with fallos_lock:
        recientes = [t for t in fallos_login.get(ip, []) if ahora - t < LOGIN_VENTANA_S]
        fallos_login[ip] = recientes
        if len(recientes) >= LOGIN_MAX_FALLOS:
            return int(LOGIN_VENTANA_S - (ahora - recientes[0])) + 1
    return 0

def anotar_fallo_login(ip):
    with fallos_lock:
        fallos_login.setdefault(ip, []).append(time.time())

# ─── Registro de una lectura RFID ────────────────────────────────────────────
def obtener_dispositivo(conn, device_id):
    d = conn.execute("SELECT * FROM dispositivos WHERE device_id=?", (device_id,)).fetchone()
    if d:
        return d
    # Lector desconocido: se registra solo, sin secreto, con tipo deducido del nombre
    tipo = "salida" if "salida" in device_id.lower() else "entrada"
    conn.execute("INSERT INTO dispositivos (device_id, nombre, tipo, activo) VALUES (?,?,?,1)",
                 (device_id, f"Lector {device_id}", tipo))
    conn.commit()
    return conn.execute("SELECT * FROM dispositivos WHERE device_id=?", (device_id,)).fetchone()

def procesar_lectura(rfid, device_id, fecha_hora=None, offline=False):
    """Valida la tarjeta, guarda el registro y devuelve (respuesta_para_esp32, evento_para_dashboard)."""
    conn = get_db()
    disp = obtener_dispositivo(conn, device_id)
    tipo = disp["tipo"] if disp["tipo"] in ("entrada", "salida") else "entrada"
    ahora_txt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fecha_hora = fecha_hora or ahora_txt
    conn.execute("UPDATE dispositivos SET ultimo_ping=? WHERE id=?", (ahora_txt, disp["id"]))

    docente = conn.execute("SELECT * FROM docentes WHERE rfid=?", (rfid,)).fetchone()

    # Registros fuera de línea: evitar duplicados si el servidor ya lo había recibido
    if offline and docente:
        dup = conn.execute("""SELECT id FROM registros WHERE docente_id=? AND tipo=?
                              AND ABS(strftime('%s', fecha_hora) - strftime('%s', ?)) <= 90""",
                           (docente["id"], tipo, fecha_hora)).fetchone()
        if dup:
            conn.commit(); conn.close()
            return None, None

    if not docente:
        decision, nota = "deny", "Tarjeta desconocida"
    elif not docente["activo"]:
        decision, nota = "deny", "Docente inactivo"
    elif not docente["consentimiento"]:
        decision, nota = "deny", "Sin autorización de datos"
    else:
        decision = "permit"
        nota = mensaje_lcd(conn, docente, tipo, fecha_hora)

    if offline:
        nota = f"{nota} (sin conexión)"

    conn.execute("""INSERT INTO registros (rfid, docente_id, tipo, decision, nota, offline, dispositivo_id, fecha_hora)
                    VALUES (?,?,?,?,?,?,?,?)""",
                 (rfid, docente["id"] if docente else None, tipo, decision, nota, 1 if offline else 0, disp["id"], fecha_hora))
    conn.commit()

    evento = {
        "fecha_hora": fecha_hora,
        "rfid": rfid,
        "tipo": tipo,
        "decision": decision,
        "nota": nota,
        "offline": offline,
        "nombre": docente["nombre"] if docente else None,
        "codigo": docente["codigo"] if docente else None,
        "facultad": docente["facultad"] if docente else None,
        "foto": docente["foto"] if docente else None,
        "dispositivo": disp["nombre"],
    }
    respuesta = {
        "decision": decision,
        "tipo": tipo,
        "nombre": solo_ascii(nombre_corto(docente["nombre"]) if docente else ""),
        "mensaje": solo_ascii(nota),
        "hora": fecha_hora[11:16],
    }
    conn.close()
    return respuesta, evento

def firma_valida(device_id, rfid, nonce, signature):
    conn = get_db()
    d = conn.execute("SELECT device_secret FROM dispositivos WHERE device_id=?", (device_id,)).fetchone()
    conn.close()
    if not d or not d["device_secret"]:
        return True   # lector sin secreto asignado (registrado automáticamente)
    if not signature or not nonce:
        return False
    esperada = hmac.new(d["device_secret"].encode(), f"{rfid}{nonce}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, esperada)

# ─── MQTT ────────────────────────────────────────────────────────────────────
mqtt_client = mqtt.Client(client_id="backend_flask_v4")
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
mqtt_client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("OK: MQTT conectado a HiveMQ Cloud")
        for t in ("acceso/+/scan", "acceso/+/offline", "acceso/+/status", "acceso/+/heartbeat"):
            client.subscribe(t)
    else:
        print(f"Error: MQTT código {rc}")

def on_message(client, userdata, msg):
    global modo_registro_activo
    try:
        parts = msg.topic.split("/")
        if len(parts) < 3:
            return
        device_id, topico = parts[1], parts[2]
        data = json.loads(msg.payload.decode() or "{}")

        if topico in ("status", "heartbeat"):
            conn = get_db()
            obtener_dispositivo(conn, device_id)
            conn.execute("""UPDATE dispositivos SET ultimo_ping=datetime('now','localtime'), ip_address=?,
                            gate_open_ms=COALESCE(?, gate_open_ms), mqtt_timeout_ms=COALESCE(?, mqtt_timeout_ms),
                            firmware=COALESCE(?, firmware), pendientes=?
                            WHERE device_id=?""",
                         (data.get("ip"), data.get("gate_open_ms"), data.get("mqtt_timeout_ms"),
                          data.get("version"), data.get("pendientes", 0), device_id))
            conn.commit(); conn.close()
            socketio.emit("dispositivo_heartbeat", {"device_id": device_id, **data})
            return

        rfid = str(data.get("rfid", "")).strip().upper()
        if not rfid:
            return
        nonce, signature = str(data.get("nonce", "")), data.get("signature", "")

        if not firma_valida(device_id, rfid, nonce, signature):
            print(f"[{device_id}] Firma HMAC inválida, lectura descartada")
            if topico == "scan":
                mqtt_client.publish(TOPIC_RESULT_BASE.format(device_id),
                                    json.dumps({"decision": "deny", "tipo": "", "nombre": "", "mensaje": "Lector no autorizado"}))
            return

        if topico == "scan" and modo_registro_activo:
            print(f"[ENROLAMIENTO] UID {rfid} desde {device_id}")
            socketio.emit("tarjeta_detectada", {"rfid": rfid, "dispositivo": device_id})
            mqtt_client.publish(TOPIC_RESULT_BASE.format(device_id),
                                json.dumps({"decision": "enroll", "tipo": "", "nombre": "", "mensaje": "Tarjeta leida"}))
            return

        if topico == "scan":
            respuesta, evento = procesar_lectura(rfid, device_id)
            mqtt_client.publish(TOPIC_RESULT_BASE.format(device_id), json.dumps(respuesta))
            print(f"[{device_id}] {rfid} -> {respuesta['decision']} ({respuesta['mensaje']})")
            socketio.emit("nuevo_acceso", evento)
        elif topico == "offline":
            ts = data.get("ts") or None
            respuesta, evento = procesar_lectura(rfid, device_id, fecha_hora=ts, offline=True)
            if evento:
                print(f"[{device_id}] Registro diferido {rfid} {ts}")
                socketio.emit("nuevo_acceso", evento)
    except Exception as e:
        print(f"Error procesando mensaje MQTT: {e}")

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

def iniciar_mqtt():
    if not MQTT_BROKER:
        print("[MQTT] Falta ACCESO_MQTT_BROKER. Copia .env.example a .env y llena tus credenciales.")
        return
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
        mqtt_client.loop_forever()
    except Exception as e:
        print(f"[MQTT] No se pudo conectar: {e}")

# ─── Socket.IO ───────────────────────────────────────────────────────────────
@socketio.on('connect')
def handle_connect(auth=None):
    # Sin token válido no se acepta la conexión: así nadie ve los accesos en vivo
    # ni puede activar el modo enrolamiento sin haber iniciado sesión.
    token = (auth or {}).get("token") if isinstance(auth, dict) else None
    if not verificar_token(token):
        return False

@socketio.on('disconnect')
def handle_disconnect(*_):
    global modo_registro_activo
    modo_registro_activo = False

@socketio.on('toggle_registro')
def handle_toggle_registro(data):
    global modo_registro_activo
    modo_registro_activo = bool((data or {}).get('activo', False))
    print(f"[SOCKET] Modo enrolamiento: {modo_registro_activo}")

# ─── API: autenticación ──────────────────────────────────────────────────────
@app.route("/api/login", methods=["POST"])
def login():
    ip = request.remote_addr
    espera = login_bloqueado(ip)
    if espera:
        return jsonify({"ok": False, "msg": f"Demasiados intentos fallidos. Espera {max(1, round(espera / 60))} min."}), 429

    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    conn = get_db()
    user = conn.execute("SELECT * FROM usuarios_dashboard WHERE username=? AND activo=1", (username,)).fetchone()
    if not user or not verificar_clave(password, user["password_hash"]):
        conn.close()
        anotar_fallo_login(ip)
        registrar_auditoria("desconocido", "LOGIN_FALLIDO", {"username": username}, ip)
        return jsonify({"ok": False, "msg": "Usuario o contraseña incorrectos."}), 401

    # Si la clave estaba guardada con el SHA-256 viejo, se pasa a pbkdf2 de una vez
    if not user["password_hash"].startswith(("pbkdf2:", "scrypt:")):
        conn.execute("UPDATE usuarios_dashboard SET password_hash=? WHERE id=?",
                     (generate_password_hash(password, method=HASH_METODO), user["id"]))
        conn.commit()
    conn.close()
    with fallos_lock:
        fallos_login.pop(ip, None)
    registrar_auditoria(username, "LOGIN_OK", None, ip)
    return jsonify({"ok": True, "token": generar_token(username, user["rol"]), "rol": user["rol"],
                    "usuario": username, "clave_por_defecto": password == CLAVE_INICIAL})

@app.route("/api/sesion", methods=["GET"])
def sesion():
    # Si llega aquí es porque exigir_sesion() ya validó el token
    return jsonify({"ok": True, "usuario": g.usuario, "rol": g.rol})

# ─── API: docentes ───────────────────────────────────────────────────────────
def docente_dict(conn, r):
    d = dict(r)
    d["horario"] = horario_de(conn, r["id"])
    d["horas_semana"] = horas_semana(d["horario"])
    d["dias"] = sorted({b["dia"] for b in d["horario"]})
    return d

@app.route("/api/docentes", methods=["GET"])
def get_docentes():
    conn = get_db()
    rows = conn.execute("SELECT * FROM docentes ORDER BY nombre").fetchall()
    out = [docente_dict(conn, r) for r in rows]
    conn.close()
    return jsonify(out)

def limpiar_docente(data):
    rfid = (data.get("rfid") or "").strip().upper() or None
    vinc = data.get("vinculacion") or "tiempo_completo"
    if vinc not in ("tiempo_completo", "ocasional"):
        vinc = "tiempo_completo"
    return {
        "codigo": (data.get("codigo") or "").strip(),
        "nombre": " ".join((data.get("nombre") or "").split()),
        "correo": (data.get("correo") or "").strip() or None,
        "facultad": (data.get("facultad") or "").strip() or None,
        "programa": (data.get("programa") or "").strip() or None,
        "vinculacion": vinc,
        "rfid": rfid,
        "consentimiento": 1 if data.get("consentimiento") else 0,
    }

@app.route("/api/docentes", methods=["POST"])
def add_docente():
    data = request.json or {}
    d = limpiar_docente(data)
    if not d["codigo"] or not d["nombre"]:
        return jsonify({"ok": False, "msg": "El código y el nombre son obligatorios."}), 400
    if d["rfid"] and not d["consentimiento"]:
        return jsonify({"ok": False, "msg": "Para asignar una tarjeta se necesita la autorización de tratamiento de datos."}), 400
    horario, err = validar_horario(data.get("horario"))
    if err:
        return jsonify({"ok": False, "msg": err}), 400
    conn = get_db()
    try:
        cur = conn.execute("""INSERT INTO docentes (codigo,nombre,correo,facultad,programa,vinculacion,rfid,consentimiento,consentimiento_fecha)
                              VALUES (?,?,?,?,?,?,?,?,?)""",
                           (d["codigo"], d["nombre"], d["correo"], d["facultad"], d["programa"], d["vinculacion"], d["rfid"],
                            d["consentimiento"], datetime.now().strftime("%Y-%m-%d %H:%M:%S") if d["consentimiento"] else None))
        guardar_horario(conn, cur.lastrowid, horario)
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.close()
        msg = "La tarjeta RFID ya está asignada a otro docente." if "rfid" in str(e) else "Ya existe un docente con ese código."
        return jsonify({"ok": False, "msg": msg}), 400
    conn.close()
    registrar_auditoria(g.usuario, "AGREGAR_DOCENTE", {"codigo": d["codigo"], "nombre": d["nombre"], "rfid": d["rfid"]}, request.remote_addr)
    return jsonify({"ok": True, "msg": "Docente registrado"}), 201

@app.route("/api/docentes/<codigo>", methods=["PUT"])
def update_docente(codigo):
    data = request.json or {}
    conn = get_db()
    actual = conn.execute("SELECT * FROM docentes WHERE codigo=?", (codigo,)).fetchone()
    if not actual:
        conn.close()
        return jsonify({"ok": False, "msg": "Docente no encontrado"}), 404

    campos = {}
    for k in ("nombre", "correo", "facultad", "programa", "vinculacion"):
        if k in data:
            v = (data[k] or "").strip()
            campos[k] = " ".join(v.split()) if k == "nombre" else (v or None)
    if "rfid" in data:
        campos["rfid"] = (data["rfid"] or "").strip().upper() or None
    if "activo" in data:
        campos["activo"] = 1 if data["activo"] else 0
    if "consentimiento" in data:
        campos["consentimiento"] = 1 if data["consentimiento"] else 0
        if campos["consentimiento"] and not actual["consentimiento"]:
            campos["consentimiento_fecha"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rfid_final = campos.get("rfid", actual["rfid"])
    consent_final = campos.get("consentimiento", actual["consentimiento"])
    if rfid_final and not consent_final:
        conn.close()
        return jsonify({"ok": False, "msg": "Para asignar una tarjeta se necesita la autorización de tratamiento de datos."}), 400
    if "nombre" in campos and not campos["nombre"]:
        conn.close()
        return jsonify({"ok": False, "msg": "El nombre no puede quedar vacío."}), 400

    try:
        if campos:
            sets = ", ".join(f"{k}=?" for k in campos)
            conn.execute(f"UPDATE docentes SET {sets} WHERE codigo=?", (*campos.values(), codigo))
        if "horario" in data:
            horario, err = validar_horario(data["horario"])
            if err:
                conn.close()
                return jsonify({"ok": False, "msg": err}), 400
            guardar_horario(conn, actual["id"], horario)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"ok": False, "msg": "La tarjeta RFID ya está asignada a otro docente."}), 400
    conn.close()
    registrar_auditoria(g.usuario, "ACTUALIZAR_DOCENTE", {"codigo": codigo, "campos": list(campos.keys()) + (["horario"] if "horario" in data else [])}, request.remote_addr)
    return jsonify({"ok": True})

@app.route("/api/docentes/<codigo>", methods=["DELETE"])
def delete_docente(codigo):
    conn = get_db()
    doc = conn.execute("SELECT id, nombre FROM docentes WHERE codigo=?", (codigo,)).fetchone()
    if not doc:
        conn.close()
        return jsonify({"ok": False, "msg": "Docente no encontrado"}), 404
    conn.execute("UPDATE registros SET docente_id=NULL WHERE docente_id=?", (doc["id"],))
    conn.execute("DELETE FROM horarios WHERE docente_id=?", (doc["id"],))
    conn.execute("DELETE FROM docentes WHERE id=?", (doc["id"],))
    conn.commit(); conn.close()
    registrar_auditoria(g.usuario, "ELIMINAR_DOCENTE", {"codigo": codigo, "nombre": doc["nombre"]}, request.remote_addr)
    return jsonify({"ok": True})

@app.route("/api/docentes/<codigo>/detalle", methods=["GET"])
def get_docente_detalle(codigo):
    conn = get_db()
    r = conn.execute("SELECT * FROM docentes WHERE codigo=?", (codigo,)).fetchone()
    if not r:
        conn.close()
        return jsonify({"ok": False, "msg": "Docente no encontrado"}), 404
    doc = docente_dict(conn, r)
    registros = conn.execute("""
        SELECT r.tipo, r.decision, r.nota, r.offline, r.fecha_hora, d.nombre AS dispositivo
        FROM registros r LEFT JOIN dispositivos d ON r.dispositivo_id=d.id
        WHERE r.docente_id=? ORDER BY r.fecha_hora DESC LIMIT 12""", (r["id"],)).fetchall()

    # Resumen de la semana actual (lunes a hoy)
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    ajustes = leer_ajustes(conn)
    eventos = eventos_por_docente(conn, lunes, hoy)
    semana = {"minutos_programados": 0, "minutos_trabajados": 0, "minutos_cumplidos": 0, "tardanzas": 0, "ausencias": 0, "dias": []}
    for i in range((hoy - lunes).days + 1):
        dia = lunes + timedelta(days=i)
        bloques = [b for b in doc["horario"] if b["dia"] == dia.weekday()]
        info = calcular_dia(bloques, eventos.get((r["id"], dia), []), dia, ajustes)
        semana["minutos_programados"] += info["minutos_programados"]
        semana["minutos_trabajados"] += info["minutos_trabajados"]
        semana["minutos_cumplidos"] += info["minutos_cumplidos"]
        semana["tardanzas"] += 1 if info["estado"] == "tarde" else 0
        semana["ausencias"] += 1 if info["estado"] == "ausente" else 0
        semana["dias"].append({"fecha": dia.isoformat(), **info})
    conn.close()
    return jsonify({"ok": True, "docente": doc, "registros": [dict(x) for x in registros], "semana": semana})

@app.route("/api/horarios/semana", methods=["GET"])
def horarios_semana():
    """Horario programado de todos los docentes activos para una semana (lunes a domingo),
    cruzado con lo que pasó de verdad: estado del día, tramos en el campus y marcas de entrada/salida."""
    try:
        ref = date.fromisoformat(request.args.get("fecha") or date.today().isoformat())
    except ValueError:
        return jsonify({"ok": False, "msg": "Fecha inválida."}), 400
    lunes = ref - timedelta(days=ref.weekday())
    domingo = lunes + timedelta(days=6)
    ahora = datetime.now()
    conn = get_db()
    ajustes = leer_ajustes(conn)
    eventos = eventos_por_docente(conn, lunes, domingo)
    salida = []
    for r in conn.execute("SELECT * FROM docentes WHERE activo=1 ORDER BY nombre").fetchall():
        horario = horario_de(conn, r["id"])
        dias = []
        for i in range(7):
            f = lunes + timedelta(days=i)
            evs = eventos.get((r["id"], f), [])
            info = calcular_dia([b for b in horario if b["dia"] == i], evs, f, ajustes, ahora)
            info["fecha"] = f.isoformat()
            info["eventos"] = [{"tipo": e["tipo"], "hora": hora_str(e["min"])} for e in evs]
            dias.append(info)
        salida.append({"codigo": r["codigo"], "nombre": r["nombre"], "facultad": r["facultad"],
                       "programa": r["programa"], "vinculacion": r["vinculacion"], "foto": r["foto"],
                       "rfid": r["rfid"], "horario": horario, "horas_semana": horas_semana(horario), "dias": dias})
    conn.close()
    return jsonify({"ok": True, "desde": lunes.isoformat(), "hasta": domingo.isoformat(),
                    "hoy": ahora.date().isoformat(), "ahora": ahora.strftime("%H:%M"),
                    "ajustes": ajustes, "docentes": salida})

FOTO_MAX_BYTES = 5 * 1024 * 1024
FOTO_FIRMAS = {b"\x89PNG\r\n\x1a\n": ".png", b"\xff\xd8\xff": ".jpg"}

@app.route("/api/docentes/<codigo>/foto", methods=["POST"])
@require_auth
def upload_foto(codigo):
    archivo = request.files.get("foto")
    if not archivo or not archivo.filename:
        return jsonify({"ok": False, "msg": "No llegó ninguna imagen."}), 400
    datos = archivo.read(FOTO_MAX_BYTES + 1)
    if len(datos) > FOTO_MAX_BYTES:
        return jsonify({"ok": False, "msg": "La foto pesa más de 5 MB."}), 413
    ext = next((e for firma, e in FOTO_FIRMAS.items() if datos.startswith(firma)), None)
    if not ext:
        return jsonify({"ok": False, "msg": "El archivo no es una imagen PNG o JPG."}), 400

    conn = get_db()
    doc = conn.execute("SELECT id, nombre FROM docentes WHERE codigo=?", (codigo,)).fetchone()
    if not doc:
        conn.close()
        return jsonify({"ok": False, "msg": "Docente no encontrado."}), 404

    # Nombre de archivo seguro: solo letras, números, guion y guion bajo
    base = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(codigo))
    carpeta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fotos")
    os.makedirs(carpeta, exist_ok=True)
    filename = base + ext
    with open(os.path.join(carpeta, filename), "wb") as fh:
        fh.write(datos)
    # Si antes tenía la foto con la otra extensión, se borra para que no quede la vieja
    for otra in set(FOTO_FIRMAS.values()) - {ext}:
        vieja = os.path.join(carpeta, base + otra)
        if os.path.exists(vieja):
            os.remove(vieja)

    conn.execute("UPDATE docentes SET foto=? WHERE codigo=?", (filename, codigo))
    conn.commit(); conn.close()
    registrar_auditoria(g.usuario, "FOTO_DOCENTE", {"codigo": codigo, "nombre": doc["nombre"]}, request.remote_addr)
    return jsonify({"ok": True, "foto": filename, "foto_url": f"/static/fotos/{filename}"})

# ─── API: registros, estadísticas y asistencia ───────────────────────────────
@app.route("/api/accesos", methods=["GET"])
def get_accesos():
    conn = get_db()
    rows = conn.execute("""
        SELECT r.fecha_hora, r.rfid, r.tipo, r.decision, r.nota, r.offline,
               doc.nombre, doc.codigo, doc.facultad, doc.foto,
               COALESCE(d.nombre, 'Lector') AS dispositivo
        FROM registros r
        LEFT JOIN docentes doc ON r.docente_id=doc.id
        LEFT JOIN dispositivos d ON r.dispositivo_id=d.id
        ORDER BY r.fecha_hora DESC, r.id DESC LIMIT 100""").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/stats", methods=["GET"])
def get_stats():
    conn = get_db()
    hoy = date.today()
    asis = asistencia_dia(conn, hoy)
    cuenta = lambda e: sum(1 for a in asis if a["estado"] == e)
    out = {
        "docentes_activos": conn.execute("SELECT COUNT(*) FROM docentes WHERE activo=1").fetchone()[0],
        "con_horario_hoy": sum(1 for a in asis if a["estado"] not in ("sin_horario", "sin_horario_presente")),
        "en_campus": sum(1 for a in asis if a["en_campus"]),
        "puntuales": cuenta("puntual"),
        "tarde": cuenta("tarde"),
        "ausentes": cuenta("ausente"),
        "sin_llegar": cuenta("sin_llegar"),
        "por_llegar": cuenta("por_llegar"),
        "registros_hoy": conn.execute("SELECT COUNT(*) FROM registros WHERE decision='permit' AND date(fecha_hora)=?", (hoy.isoformat(),)).fetchone()[0],
        "rechazos_hoy": conn.execute("SELECT COUNT(*) FROM registros WHERE decision='deny' AND date(fecha_hora)=?", (hoy.isoformat(),)).fetchone()[0],
        "total_registros": conn.execute("SELECT COUNT(*) FROM registros").fetchone()[0],
    }
    conn.close()
    return jsonify(out)

@app.route("/api/stats/charts", methods=["GET"])
@require_auth
def get_chart_stats():
    conn = get_db()
    rows = conn.execute("""SELECT strftime('%H', fecha_hora) AS hora, tipo, COUNT(*) AS total FROM registros
                           WHERE decision='permit' AND date(fecha_hora)=date('now','localtime')
                           GROUP BY hora, tipo""").fetchall()
    conn.close()
    entradas = {f"{h:02d}": 0 for h in range(24)}
    salidas = dict(entradas)
    for r in rows:
        (entradas if r["tipo"] == "entrada" else salidas)[r["hora"]] = r["total"]
    return jsonify({"labels": list(entradas.keys()), "entradas": list(entradas.values()), "salidas": list(salidas.values())})

def leer_fecha(valor, defecto):
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except Exception:
        return defecto

@app.route("/api/asistencia", methods=["GET"])
def get_asistencia():
    fecha = leer_fecha(request.args.get("fecha"), date.today())
    conn = get_db()
    filas = asistencia_dia(conn, fecha)
    conn.close()
    return jsonify({"fecha": fecha.isoformat(), "dia": DIAS[fecha.weekday()], "docentes": filas})

def resumen_rango(conn, desde, hasta):
    ajustes = leer_ajustes(conn)
    eventos = eventos_por_docente(conn, desde, hasta)
    docentes = conn.execute("SELECT * FROM docentes WHERE activo=1 ORDER BY nombre").fetchall()
    hoy = date.today()
    out = []
    for d in docentes:
        horario = horario_de(conn, d["id"])
        acum = {"dias_programados": 0, "asistencias": 0, "puntuales": 0, "tardanzas": 0, "ausencias": 0,
                "salidas_anticipadas": 0, "sin_salida": 0, "minutos_programados": 0, "minutos_trabajados": 0,
                "minutos_cumplidos": 0, "minutos_retraso": 0}
        dia = desde
        while dia <= min(hasta, hoy):
            bloques = [b for b in horario if b["dia"] == dia.weekday()]
            info = calcular_dia(bloques, eventos.get((d["id"], dia), []), dia, ajustes)
            if bloques:
                acum["dias_programados"] += 1
                acum["minutos_programados"] += info["minutos_programados"]
            if info["estado"] in ("puntual", "tarde", "sin_horario_presente"):
                acum["asistencias"] += 1
            acum["puntuales"] += info["estado"] == "puntual"
            acum["tardanzas"] += info["estado"] == "tarde"
            acum["ausencias"] += info["estado"] == "ausente"
            acum["salidas_anticipadas"] += 1 if info["minutos_salida_anticipada"] else 0
            acum["sin_salida"] += 1 if info["sin_salida"] else 0
            acum["minutos_trabajados"] += info["minutos_trabajados"]
            acum["minutos_cumplidos"] += info["minutos_cumplidos"]
            acum["minutos_retraso"] += info["minutos_retraso"]
            dia += timedelta(days=1)
        prog = acum["minutos_programados"]
        acum["cumplimiento"] = round(min(acum["minutos_cumplidos"] / prog, 1) * 100) if prog else None
        out.append({"codigo": d["codigo"], "nombre": d["nombre"], "facultad": d["facultad"], "foto": d["foto"], **acum})
    return out

@app.route("/api/asistencia/resumen", methods=["GET"])
def get_asistencia_resumen():
    hoy = date.today()
    hasta = leer_fecha(request.args.get("hasta"), hoy)
    desde = leer_fecha(request.args.get("desde"), hasta - timedelta(days=hasta.weekday()))
    if desde > hasta:
        desde, hasta = hasta, desde
    if (hasta - desde).days > 186:
        return jsonify({"ok": False, "msg": "El rango máximo es de seis meses."}), 400
    conn = get_db()
    filas = resumen_rango(conn, desde, hasta)
    conn.close()
    return jsonify({"desde": desde.isoformat(), "hasta": hasta.isoformat(), "docentes": filas})

def respuesta_csv(nombre, encabezado, filas):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(encabezado)
    w.writerows(filas)
    return "\ufeff" + out.getvalue(), 200, {"Content-Type": "text/csv; charset=utf-8",
                                             "Content-Disposition": f"attachment; filename={nombre}"}

ESTADOS_TXT = {"puntual": "Puntual", "tarde": "Tarde", "ausente": "Ausente", "por_llegar": "Por llegar", "sin_llegar": "No ha llegado",
               "sin_horario": "Sin horario", "sin_horario_presente": "Presente sin horario"}

@app.route("/api/asistencia/export/csv", methods=["GET"])
def export_asistencia_dia():
    fecha = leer_fecha(request.args.get("fecha"), date.today())
    conn = get_db()
    filas = asistencia_dia(conn, fecha)
    conn.close()
    return respuesta_csv(f"asistencia_{fecha.isoformat()}.csv",
        ["Fecha", "Codigo", "Docente", "Facultad", "Estado", "Horario", "Entrada", "Salida",
         "Minutos tarde", "Minutos salida anticipada", "Horas programadas", "Horas en horario", "Horas en campus", "Cumplimiento %"],
        [[fecha.isoformat(), a["codigo"], a["nombre"], a["facultad"] or "", ESTADOS_TXT.get(a["estado"], a["estado"]),
          f"{a['programado_inicio']}-{a['programado_fin']}" if a["programado_inicio"] else "",
          a["entrada"] or "", a["salida"] or "", a["minutos_retraso"], a["minutos_salida_anticipada"],
          round(a["minutos_programados"] / 60, 2), round(a["minutos_cumplidos"] / 60, 2), round(a["minutos_trabajados"] / 60, 2),
          a["cumplimiento"] if a["cumplimiento"] is not None else ""] for a in filas])

@app.route("/api/asistencia/resumen/export/csv", methods=["GET"])
def export_asistencia_resumen():
    hoy = date.today()
    hasta = leer_fecha(request.args.get("hasta"), hoy)
    desde = leer_fecha(request.args.get("desde"), hasta - timedelta(days=hasta.weekday()))
    conn = get_db()
    filas = resumen_rango(conn, desde, hasta)
    conn.close()
    return respuesta_csv(f"resumen_asistencia_{desde.isoformat()}_{hasta.isoformat()}.csv",
        ["Desde", "Hasta", "Codigo", "Docente", "Facultad", "Dias programados", "Asistencias", "Puntuales", "Tardanzas",
         "Ausencias", "Salidas anticipadas", "Dias sin salida", "Horas programadas", "Horas en horario", "Horas en campus", "Cumplimiento %"],
        [[desde.isoformat(), hasta.isoformat(), a["codigo"], a["nombre"], a["facultad"] or "", a["dias_programados"],
          a["asistencias"], a["puntuales"], a["tardanzas"], a["ausencias"], a["salidas_anticipadas"], a["sin_salida"],
          round(a["minutos_programados"] / 60, 2), round(a["minutos_cumplidos"] / 60, 2), round(a["minutos_trabajados"] / 60, 2),
          a["cumplimiento"] if a["cumplimiento"] is not None else ""] for a in filas])

@app.route("/api/ajustes", methods=["GET"])
def get_ajustes():
    conn = get_db()
    a = leer_ajustes(conn)
    conn.close()
    return jsonify(a)

@app.route("/api/ajustes", methods=["PUT"])
def put_ajustes():
    data = request.json or {}
    conn = get_db()
    try:
        for k in AJUSTES_DEFAULT:
            if k in data:
                v = float(data[k])
                if v < 0 or v > (80 if k == "meta_horas_semana" else 120):
                    raise ValueError
                conn.execute("INSERT OR REPLACE INTO ajustes (clave, valor) VALUES (?, ?)", (k, str(int(v) if k != "meta_horas_semana" else v)))
        conn.commit()
    except Exception:
        conn.close()
        return jsonify({"ok": False, "msg": "Valores fuera de rango."}), 400
    a = leer_ajustes(conn)
    conn.close()
    registrar_auditoria(g.usuario, "ACTUALIZAR_AJUSTES", a, request.remote_addr)
    return jsonify({"ok": True, **a})

# ─── API: dispositivos ───────────────────────────────────────────────────────
@app.route("/api/dispositivos", methods=["GET"])
def get_dispositivos():
    conn = get_db()
    rows = conn.execute("SELECT * FROM dispositivos ORDER BY tipo, nombre").fetchall()
    conn.close()
    ahora = datetime.now()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("device_secret", None)
        d["online"] = False
        if d["ultimo_ping"]:
            try:
                d["online"] = (ahora - datetime.strptime(d["ultimo_ping"], "%Y-%m-%d %H:%M:%S")).total_seconds() < 120
            except Exception:
                pass
        out.append(d)
    return jsonify(out)

@app.route("/api/dispositivos/<int:id>", methods=["PUT"])
def update_dispositivo(id):
    data = request.json or {}
    conn = get_db()
    curr = conn.execute("SELECT * FROM dispositivos WHERE id=?", (id,)).fetchone()
    if not curr:
        conn.close()
        return jsonify({"ok": False, "msg": "Dispositivo no encontrado"}), 404
    tipo = data.get("tipo", curr["tipo"])
    if tipo not in ("entrada", "salida"):
        tipo = curr["tipo"]
    valores = (data.get("nombre", curr["nombre"]), tipo, data.get("ubicacion", curr["ubicacion"]),
               data.get("activo", curr["activo"]), data.get("pos_x", data.get("x", curr["pos_x"])),
               data.get("pos_y", data.get("y", curr["pos_y"])), id)
    conn.execute("UPDATE dispositivos SET nombre=?, tipo=?, ubicacion=?, activo=?, pos_x=?, pos_y=? WHERE id=?", valores)
    conn.commit(); conn.close()
    registrar_auditoria(g.usuario, "EDITAR_DISPOSITIVO", {"id": id, "nombre": valores[0], "tipo": tipo}, request.remote_addr)
    return jsonify({"ok": True})

@app.route("/api/dispositivos/<int:id>/posicion", methods=["PUT"])
@require_auth
def update_dispositivo_pos(id):
    data = request.json or {}
    conn = get_db()
    conn.execute("UPDATE dispositivos SET pos_x=?, pos_y=? WHERE id=?", (data.get("x"), data.get("y"), id))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

@app.route("/api/dispositivos/<int:id>/configurar", methods=["POST"])
@require_auth
def configurar_dispositivo(id):
    """Envía parámetros sueltos (tiempo de puerta, timeout). El ESP32 los mezcla con su config.json."""
    data = request.json or {}
    conn = get_db()
    disp = conn.execute("SELECT device_id FROM dispositivos WHERE id=?", (id,)).fetchone()
    if not disp:
        conn.close()
        return jsonify({"ok": False, "msg": "Dispositivo no encontrado"}), 404
    ajustes = {k: int(data[k]) for k in ("gate_open_ms", "mqtt_timeout_ms") if data.get(k) is not None}
    if "abrir_sin_conexion" in data:
        ajustes["abrir_sin_conexion"] = bool(data["abrir_sin_conexion"])
    if not ajustes:
        conn.close()
        return jsonify({"ok": False, "msg": "Sin parámetros para actualizar"}), 400
    mqtt_client.publish(f"acceso/{disp['device_id']}/config", json.dumps({"settings": ajustes}))
    for k in ("gate_open_ms", "mqtt_timeout_ms"):
        if k in ajustes:
            conn.execute(f"UPDATE dispositivos SET {k}=? WHERE id=?", (ajustes[k], id))
    conn.commit(); conn.close()
    registrar_auditoria(g.usuario, "CONFIGURAR_DISPOSITIVO", {"id": id, **ajustes}, request.remote_addr)
    return jsonify({"ok": True, "msg": f"Configuración enviada a {disp['device_id']}"})

@app.route("/api/dispositivos/<int:id>/actualizar_ota", methods=["POST"])
@require_auth
def actualizar_ota(id):
    if "firmware" not in request.files or request.files["firmware"].filename == "":
        return jsonify({"ok": False, "msg": "Falta el archivo firmware"}), 400
    conn = get_db()
    disp = conn.execute("SELECT device_id FROM dispositivos WHERE id=?", (id,)).fetchone()
    conn.close()
    if not disp:
        return jsonify({"ok": False, "msg": "Dispositivo no encontrado"}), 404
    firmware_dir = os.path.join(os.path.dirname(__file__), "firmware")
    os.makedirs(firmware_dir, exist_ok=True)
    request.files["firmware"].save(os.path.join(firmware_dir, "firmware.bin"))
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1)); local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()
    url = f"http://{local_ip}:5000/api/firmware/firmware.bin"
    mqtt_client.publish(f"acceso/{disp['device_id']}/ota", json.dumps({"url": url}))
    registrar_auditoria(g.usuario, "ACTUALIZAR_OTA", {"id": id, "device_id": disp["device_id"], "url": url}, request.remote_addr)
    return jsonify({"ok": True, "msg": f"Instrucción OTA enviada. El lector descargará el firmware desde {url}"})

@app.route("/api/dispositivos/<int:id>", methods=["DELETE"])
def delete_dispositivo(id):
    conn = get_db()
    d = conn.execute("SELECT device_id FROM dispositivos WHERE id=?", (id,)).fetchone()
    if d and d["device_id"] in [x[0] for x in DISPOSITIVOS_BASE]:
        conn.close()
        return jsonify({"ok": False, "msg": "Los lectores principales de entrada y salida no se pueden eliminar."}), 400
    conn.execute("UPDATE registros SET dispositivo_id=NULL WHERE dispositivo_id=?", (id,))
    conn.execute("DELETE FROM dispositivos WHERE id=?", (id,))
    conn.commit(); conn.close()
    registrar_auditoria(g.usuario, "ELIMINAR_DISPOSITIVO", {"id": id}, request.remote_addr)
    return jsonify({"ok": True})

# ─── API: auditoría ──────────────────────────────────────────────────────────
@app.route("/api/auditoria", methods=["GET"])
def get_auditoria():
    conn = get_db()
    rows = conn.execute("SELECT * FROM auditoria ORDER BY fecha_hora DESC, id DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

# ─── Páginas ─────────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    return render_template_string(open(os.path.join(os.path.dirname(__file__), "dashboard.html"), encoding="utf-8").read())

@app.route("/api/firmware/<path:filename>")
def serve_firmware(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "firmware"), filename)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=iniciar_mqtt, daemon=True).start()
    print("Servidor iniciado en http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
