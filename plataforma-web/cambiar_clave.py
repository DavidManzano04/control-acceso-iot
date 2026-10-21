# ─────────────────────────────────────────────────────────────────────────────
# cambiar_clave.py — Cambia (o crea) la contraseña de un usuario del dashboard.
# Uso, desde la carpeta del proyecto y con el venv activado:
#     python cambiar_clave.py            -> cambia la clave de "admin"
#     python cambiar_clave.py otro_user  -> cambia o crea ese usuario
# No hace falta apagar el servidor. Las sesiones abiertas de ese usuario siguen
# funcionando hasta que venzan (8 h); si quieres sacarlas ya, borra jwt_secret.key
# y reinicia el servidor.
# ─────────────────────────────────────────────────────────────────────────────
import sqlite3, os, sys, getpass
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acceso.db")
HASH_METODO = "pbkdf2:sha256:600000"
MIN_LARGO = 8


def main():
    usuario = (sys.argv[1] if len(sys.argv) > 1 else "admin").strip()
    if not os.path.exists(DB_PATH):
        print(f"No encuentro la base de datos en {DB_PATH}. Arranca primero el servidor una vez.")
        return 1

    conn = sqlite3.connect(DB_PATH)
    existe = conn.execute("SELECT id FROM usuarios_dashboard WHERE username=?", (usuario,)).fetchone()
    print(("Cambiar la clave de " if existe else "Crear el usuario ") + f"'{usuario}'")

    while True:
        c1 = getpass.getpass("Nueva contraseña: ")
        if len(c1) < MIN_LARGO:
            print(f"Muy corta: mínimo {MIN_LARGO} caracteres.")
            continue
        if c1.lower() in ("admin123", "12345678", "password", "contraseña", usuario.lower()):
            print("Esa es demasiado fácil de adivinar. Usa otra.")
            continue
        if getpass.getpass("Repítela: ") != c1:
            print("No coinciden, otra vez.")
            continue
        break

    h = generate_password_hash(c1, method=HASH_METODO)
    if existe:
        conn.execute("UPDATE usuarios_dashboard SET password_hash=?, activo=1 WHERE id=?", (h, existe[0]))
    else:
        conn.execute("INSERT INTO usuarios_dashboard (username, password_hash, rol) VALUES (?,?,?)", (usuario, h, "admin"))
    conn.execute("INSERT INTO auditoria (usuario, accion, detalle, ip) VALUES (?,?,?,?)",
                 (usuario, "CAMBIO_CLAVE", None, "consola"))
    conn.commit()
    conn.close()
    print("Listo. Ya puedes entrar al dashboard con la clave nueva.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
