import hashlib
import getpass
import secrets

usuario = "concejaltintina"

contrasena = getpass.getpass("Elegí tu nueva contraseña: ")
confirmacion = getpass.getpass("Escribila nuevamente: ")

if contrasena != confirmacion:
    print("Las contraseñas no coinciden.")
    raise SystemExit

salt = secrets.token_hex(16)

password_hash = hashlib.pbkdf2_hmac(
    "sha256",
    contrasena.encode("utf-8"),
    salt.encode("utf-8"),
    300_000
).hex()

print("\nUsuario:", usuario)
print("Password hash:", password_hash)
print("Salt:", salt)