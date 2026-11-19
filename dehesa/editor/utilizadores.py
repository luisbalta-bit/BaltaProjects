"""Gere os utilizadores do editor Dehesa.

  venv/bin/python utilizadores.py adicionar <utilizador> "<Nome>"   (pede a palavra-passe)
  venv/bin/python utilizadores.py remover <utilizador>
  venv/bin/python utilizadores.py listar
"""
import getpass
import json
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

F = Path(__file__).resolve().parent / "data/users.json"


def load():
    return json.loads(F.read_text()) if F.exists() else {}


def save(u):
    F.parent.mkdir(exist_ok=True)
    F.write_text(json.dumps(u, ensure_ascii=False, indent=1))
    F.chmod(0o600)


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd, u = argv[1], load()
    if cmd == "listar":
        for k, v in u.items():
            print(f"{k}\t{v.get('name', '')}")
    elif cmd == "adicionar" and len(argv) >= 3:
        name = argv[2].strip().lower()
        pw = getpass.getpass("Palavra-passe (mín. 10 caracteres): ")
        if len(pw) < 10 or pw != getpass.getpass("Repetir: "):
            sys.exit("Palavra-passe curta ou diferente.")
        u[name] = {"name": argv[3] if len(argv) > 3 else name, "hash": generate_password_hash(pw)}
        save(u)
        print(f"Utilizador «{name}» guardado.")
    elif cmd == "remover" and len(argv) >= 3:
        u.pop(argv[2].strip().lower(), None)
        save(u)
        print("Removido.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
