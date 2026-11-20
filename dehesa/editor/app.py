"""Dehesa — editor por pedido (prompt + ficheiros). Flask, porta 5056.

Cada pedido corre o Claude Code (modo restrito) na cópia de pré-visualização
(/home/balta/dehesa/preview, ramo git `rascunho`). A pré-visualização corre
na porta 5057 e é mostrada através deste servidor (proxy). "Publicar" junta o
rascunho à loja real (ramo `main`) e reinicia o serviço dehesa-loja.
"""
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from PIL import Image, ImageOps
from werkzeug.security import check_password_hash, generate_password_hash

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
UPLOADS = DATA / "uploads"
LOJA = Path("/home/balta/dehesa/loja")
PREVIEW = Path("/home/balta/dehesa/preview")
SHOP_PY = LOJA / "venv/bin/python"
PREVIEW_PORT = 5057
LIVE_PORT = 5055
CLAUDE = os.environ.get("CLAUDE_BIN", "/home/balta/.local/bin/claude")
MODEL = os.environ.get("EDITOR_MODEL", "sonnet")
JOB_TIMEOUT = 15 * 60
PROTECTED = re.compile(r"(\.sh|\.service|\.gitignore|caddy[^/]*\.txt|requirements\.txt)$")
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}
DOC_EXT = {".pdf", ".txt", ".md", ".csv", ".json"}
MAX_FILES = 10

DATA.mkdir(exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
KEY_FILE = DATA / ".secret_key"
if not KEY_FILE.exists():
    KEY_FILE.write_text(secrets.token_hex(32))
    KEY_FILE.chmod(0o600)

app = Flask(__name__, static_url_path="/_admin/static")
app.secret_key = KEY_FILE.read_text().strip()
app.config.update(
    SESSION_COOKIE_NAME="dehesa_admin", SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14, MAX_CONTENT_LENGTH=60 * 1024 * 1024,
)

SYSTEM = """\
Estás a editar a loja online Dehesa (malas feitas à mão em tecido jacquard; Flask + Jinja).
O diretório atual é uma CÓPIA DE PRÉ-VISUALIZAÇÃO: a equipa vê o resultado antes de publicar.
- catalog.py: produtos, preços, categorias e todos os textos (dicionário T) em PT, ES e EN.
- templates/*.html: páginas (base.html = cabeçalho e rodapé). static/css/style.css: estilos. static/img/: fotos.
Quem faz os pedidos não tem conhecimentos técnicos. Interpreta com bom senso; se o pedido for ambíguo
segue a interpretação mais provável e diz qual foi.
Regras:
1. Todo o texto visível existe em PT, ES e EN: ao mudar ou acrescentar um, traduz os outros dois.
2. Mantém a estética da marca (marfim #FAF6F1, castanho #4A3020, Cormorant Garamond + Jost) salvo pedido em contrário.
3. As imagens enviadas já estão em static/img/ com o nome indicado no pedido; usa esse nome.
4. Não alteres ficheiros .sh, .service, caddy*.txt, requirements.txt nem .gitignore.
5. Não cries rotas que exponham ficheiros do servidor nem guardes dados pessoais. Os preços são ilustrativos.
6. Não podes correr comandos; o sistema testa as páginas automaticamente depois de terminares.
No fim responde em português de Portugal, em 2 a 5 frases simples, sem jargão nem nomes de ficheiros:
o que mudou e onde se vê. Se não for possível fazer o pedido, explica porquê.
"""

FRIENDLY = {
    "catalog.py": "o catálogo (produtos, preços e textos)",
    "app.py": "o funcionamento da loja",
    "templates/base.html": "o cabeçalho e o rodapé",
    "templates/home.html": "a página inicial",
    "templates/product.html": "a página de produto",
    "templates/cart.html": "o carrinho",
    "templates/checkout.html": "a página de pagamento",
    "templates/done.html": "a página de encomenda concluída",
    "static/css/style.css": "o aspeto visual (cores, letras, espaços)",
}

lock = threading.Lock()          # um pedido de cada vez (há uma só pré-visualização)
state_lock = threading.Lock()
jobs = {}                        # id -> job (em memória; histórico em jobs.jsonl)
current = {"job": None, "proc": None}
preview_proc = {"p": None}
login_fails = {}


# ---------- utilidades ----------

def git(*args, cwd=PREVIEW, check=True):
    r = subprocess.run(["git", "-c", "user.name=Dehesa Editor", "-c", "user.email=editor@dehesa.local", *args],
                       cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


def friendly(path):
    rel = str(path)
    for root in (str(PREVIEW) + "/", str(UPLOADS) + "/"):
        if rel.startswith(root):
            rel = rel[len(root):]
    if rel in FRIENDLY:
        return FRIENDLY[rel]
    if rel.startswith("static/img/"):
        return "a imagem " + rel.split("/")[-1]
    if rel.startswith("templates/"):
        return "a página " + rel[10:]
    return rel.split("/")[-1]


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def save_json(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


def draft_state():
    return load_json(DATA / "state.json", {"session": None})


def set_draft_state(**kw):
    s = draft_state()
    s.update(kw)
    save_json(DATA / "state.json", s)


def record_usage(result):
    try:
        u = result.get("usage") or {}
        row = {"ts": time.time(), "model": MODEL, "input": u.get("input_tokens", 0),
               "output": u.get("output_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
               "cache_write": u.get("cache_creation_input_tokens", 0), "cost_usd": result.get("total_cost_usd")}
        d = Path.home() / ".agent_m/usage"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "dehesa-loja.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def slug(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "ficheiro"


# ---------- pré-visualização ----------

def start_preview():
    stop_preview()
    env = dict(os.environ, PORT=str(PREVIEW_PORT))
    preview_proc["p"] = subprocess.Popen([str(SHOP_PY), "app.py"], cwd=PREVIEW, env=env, start_new_session=True,
                                         stdout=subprocess.DEVNULL, stderr=open(DATA / "preview.log", "w"))
    (DATA / "preview.pid").write_text(str(preview_proc["p"].pid))
    for _ in range(40):
        time.sleep(0.25)
        if preview_proc["p"].poll() is not None:
            return False
        try:
            requests.get(f"http://127.0.0.1:{PREVIEW_PORT}/", timeout=2)
            return True
        except requests.RequestException:
            pass
    return False


def stop_preview():
    p = preview_proc["p"]
    pids = [p.pid] if p else []
    if not p and (DATA / "preview.pid").exists():
        try:
            pids = [int((DATA / "preview.pid").read_text())]
        except ValueError:
            pass
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if p:
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
    preview_proc["p"] = None


def check_site(port):
    """Devolve lista de erros (vazia se as páginas principais respondem bem)."""
    errors = []
    base = f"http://127.0.0.1:{port}"
    pages = ["/", "/cart", "/lang/es", "/lang/en", "/lang/pt"]
    try:
        home = requests.get(base + "/", timeout=10)
        pages += sorted(set(re.findall(r'href="(/p/[^"#?]+)"', home.text)))[:30]
        pages += sorted(set(re.findall(r'href="(/\?cat=[^"#]+)"', home.text)))[:10]
    except requests.RequestException as e:
        return [f"A loja não arrancou: {e}"]
    for path in pages:
        try:
            r = requests.get(base + path.replace("&amp;", "&"), timeout=10, allow_redirects=True)
            if r.status_code >= 500:
                errors.append(f"{path} devolveu erro {r.status_code}")
        except requests.RequestException as e:
            errors.append(f"{path}: {e}")
    return errors


def compile_errors():
    errs = []
    for f in PREVIEW.rglob("*.py"):
        if "venv" in f.parts:
            continue
        r = subprocess.run([str(SHOP_PY), "-m", "py_compile", str(f)], capture_output=True, text=True)
        if r.returncode:
            errs.append(r.stderr.strip()[-800:])
    return errs


def preview_log_tail():
    try:
        return (DATA / "preview.log").read_text()[-2500:]
    except OSError:
        return ""


def verify_preview():
    errs = compile_errors()
    if errs:
        return errs
    if not start_preview():
        return ["A pré-visualização não arrancou:\n" + preview_log_tail()]
    errs = check_site(PREVIEW_PORT)
    if errs:
        errs.append(preview_log_tail())
    return errs


# ---------- agente ----------

def describe_tool(name, inp):
    path = inp.get("file_path") or inp.get("path") or ""
    if name == "Read":
        return f"A consultar {friendly(path)}"
    if name in ("Edit", "MultiEdit"):
        return f"A alterar {friendly(path)}"
    if name == "Write":
        return f"A escrever {friendly(path)}"
    if name in ("Glob", "Grep"):
        return "A procurar nos ficheiros da loja"
    return f"A usar {name}"


def run_claude(job, prompt, resume):
    cmd = [CLAUDE, "-p", prompt, "--restricted", "--tools", "Read,Edit,Write,Glob,Grep",
           "--permission-mode", "acceptEdits", "--permission-prompts", "none", "--disable-slash-commands",
           "--output-format", "stream-json", "--verbose", "--model", MODEL,
           "--append-system-prompt", SYSTEM]
    if job["upload_dir"]:
        cmd += ["--add-dir", job["upload_dir"]]
    if resume:
        cmd += ["--resume", resume]
    env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    errlog = DATA / "agent.log"
    proc = subprocess.Popen(cmd, cwd=PREVIEW, stdout=subprocess.PIPE, stderr=open(errlog, "w"), text=True,
                            env=env, start_new_session=True)
    current["proc"] = proc

    def kill():
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    timer = threading.Timer(JOB_TIMEOUT, kill)
    timer.start()
    result = None
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                job["session"] = ev.get("session_id")
            elif ev.get("type") == "assistant":
                for block in ev["message"].get("content", []):
                    if block.get("type") == "tool_use":
                        job["log"].append({"k": "tool", "t": describe_tool(block["name"], block.get("input", {}))})
                    elif block.get("type") == "text" and block.get("text", "").strip():
                        job["log"].append({"k": "text", "t": block["text"].strip()})
            elif ev.get("type") == "result":
                result = ev
        proc.wait()
    finally:
        timer.cancel()
        current["proc"] = None
    if result:
        record_usage(result)
        job["cost"] = round((job.get("cost") or 0) + (result.get("total_cost_usd") or 0), 4)
        job["session"] = result.get("session_id") or job.get("session")
    if job.get("cancelled"):
        raise RuntimeError("Pedido cancelado.")
    if not result or result.get("is_error"):
        err = (result or {}).get("result") or errlog.read_text()[-600:] or "o agente terminou sem resposta"
        raise RuntimeError(f"O agente falhou: {err}")
    return result.get("result", "")


def changed_files():
    out = git("status", "--porcelain", "-uall")
    return [line.split(maxsplit=1)[1].split(" -> ")[-1].strip('"') for line in out.splitlines() if line.strip()]


def save_uploads(files, job_id):
    """Imagens vão já otimizadas para static/img/ da pré-visualização; documentos para data/uploads/<job>."""
    notes, updir = [], None
    for f in files[:MAX_FILES]:
        name = f.filename or "ficheiro"
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if ext in IMG_EXT:
            try:
                im = ImageOps.exif_transpose(Image.open(f.stream))
                im = im.convert("RGB")
                im.thumbnail((1800, 1800))
            except Exception:
                raise ValueError(f"Não consegui abrir a imagem «{name}».")
            target = PREVIEW / "static/img" / f"{slug(stem)}.jpg"
            n = 2
            while target.exists():
                target = PREVIEW / "static/img" / f"{slug(stem)}-{n}.jpg"
                n += 1
            im.save(target, "JPEG", quality=86, optimize=True, progressive=True)
            notes.append(f"- imagem «{name}» guardada em static/img/{target.name} ({im.width}x{im.height})")
        elif ext in DOC_EXT:
            updir = UPLOADS / job_id
            updir.mkdir(parents=True, exist_ok=True)
            target = updir / (slug(stem) + ext)
            f.save(target)
            notes.append(f"- documento «{name}» disponível para leitura em {target}")
        else:
            raise ValueError(f"Tipo de ficheiro não suportado: «{name}». Use imagens (JPG, PNG, WEBP) ou PDF/TXT/CSV.")
    return notes, (str(updir) if updir else None)


def reset_preview_tree():
    git("reset", "--hard", "-q", "HEAD")
    git("clean", "-fdq")


def job_worker(job, files_notes):
    try:
        prompt = job["prompt"]
        if files_notes:
            prompt += "\n\nFicheiros enviados com este pedido:\n" + "\n".join(files_notes)
        resume = draft_state().get("session")
        job["status"] = "a trabalhar"
        summary = run_claude(job, prompt, resume)
        set_draft_state(session=job["session"])
        job["status"] = "a testar"
        job["log"].append({"k": "tool", "t": "A testar a pré-visualização"})
        for p in [p for p in changed_files() if PROTECTED.search(p)]:
            if git("ls-files", p):
                git("checkout", "HEAD", "--", p)
            else:
                (PREVIEW / p).unlink(missing_ok=True)
        errs = verify_preview()
        if errs:
            job["log"].append({"k": "tool", "t": "Encontrei um problema — a pedir ao agente que corrija"})
            fix = ("O sistema testou a loja e encontrou estes erros. Corrige-os sem mudar o resultado pedido:\n"
                   + "\n".join(errs)[:5000])
            summary = run_claude(job, fix, job["session"])
            set_draft_state(session=job["session"])
            errs = verify_preview()
            if errs:
                reset_preview_tree()
                start_preview()
                raise RuntimeError("A alteração partia a loja e foi anulada. Tente descrever o pedido de outra forma.")
        files = changed_files()
        if not files:
            job.update(status="sem alterações", summary=summary)
            return
        git("add", "-A")
        msg = json.dumps({"prompt": job["prompt"][:500], "user": job["user"], "summary": summary[:1500]},
                         ensure_ascii=False)
        git("commit", "-qm", msg)
        job.update(status="pronto", summary=summary, commit=git("rev-parse", "--short", "HEAD"),
                   files=sorted({friendly(f) for f in files}))
    except Exception as e:
        job.update(status="erro", error=str(e))
        try:
            reset_preview_tree()
            start_preview()
        except Exception:
            pass
    finally:
        job["ended"] = time.time()
        with open(DATA / "jobs.jsonl", "a") as f:
            f.write(json.dumps({k: v for k, v in job.items() if k != "log"}, ensure_ascii=False) + "\n")
        current["job"] = None
        lock.release()


# ---------- histórico ----------

def parse_commit_msg(msg):
    try:
        d = json.loads(msg)
        if isinstance(d, dict):
            return d
    except ValueError:
        pass
    return {"prompt": msg, "user": "", "summary": ""}


def commits(rev_range, cwd=PREVIEW, limit=30):
    out = git("log", f"-{limit}", "--format=%h%x1f%ct%x1f%B%x1e", rev_range, cwd=cwd, check=False)
    items = []
    for rec in out.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        h, ts, body = rec.split("\x1f", 2)
        d = parse_commit_msg(body.strip())
        if d.get("kind") == "publish":
            continue
        files = git("show", "--name-only", "--format=", h, cwd=cwd, check=False).split()
        items.append({"id": h, "ts": int(ts), "prompt": d.get("prompt", ""), "user": d.get("user", ""),
                      "summary": d.get("summary", ""), "files": sorted({friendly(f) for f in files})})
    return items


def publications(limit=20):
    out = git("log", f"-{limit}", "--first-parent", "--format=%h%x1f%ct%x1f%B%x1e", "main", cwd=LOJA, check=False)
    items = []
    for rec in out.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        h, ts, body = rec.split("\x1f", 2)
        d = parse_commit_msg(body.strip())
        items.append({"id": h, "ts": int(ts), "user": d.get("user", ""),
                      "title": d.get("title") or d.get("prompt", "")[:120]})
    return items


# ---------- autenticação ----------

def users():
    return load_json(DATA / "users.json", {})


def pw_tag(u):
    # muda sempre que a palavra-passe muda: termina as sessões abertas noutros dispositivos
    return u["hash"][-16:]


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = session.get("user")
        known = users().get(u) if u else None
        if not known or session.get("pw") != pw_tag(known):
            if request.path.startswith("/_admin/api"):
                return jsonify(error="Sessão expirada. Entre de novo."), 401
            return redirect(url_for("login", next=request.full_path))
        return fn(*a, **kw)
    return wrapper


def same_origin():
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    return urlparse(origin).netloc == request.host


@app.before_request
def csrf():
    if request.method == "POST" and request.path.startswith("/_admin") and not same_origin():
        abort(403)


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


@app.route("/_admin/login", methods=["GET", "POST"])
def login():
    error = None
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if request.method == "POST":
        fails = [t for t in login_fails.get(ip, []) if t > time.time() - 900]
        login_fails[ip] = fails
        name = request.form.get("user", "").strip().lower()
        u = users().get(name)
        if len(fails) >= 8:
            error = "Demasiadas tentativas. Tente de novo dentro de 15 minutos."
        elif u and check_password_hash(u["hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["user"] = name
            session["pw"] = pw_tag(u)
            nxt = request.args.get("next") or ""
            if not nxt.startswith("/_admin") or nxt.startswith("//"):
                nxt = url_for("index")
            return redirect(nxt)
        else:
            fails.append(time.time())
            error = "Utilizador ou palavra-passe errados."
    return render_template("login.html", error=error)


users_lock = threading.Lock()


@app.route("/_admin/password", methods=["GET", "POST"])
@login_required
def change_password():
    error = done = None
    if request.method == "POST":
        name = session["user"]
        f = request.form
        new = f.get("new", "")
        with users_lock:
            all_users = users()
            u = all_users[name]
            if not check_password_hash(u["hash"], f.get("current", "")):
                error = "A palavra-passe atual está errada."
            elif len(new) < 10:
                error = "A nova palavra-passe tem de ter pelo menos 10 caracteres."
            elif new != f.get("repeat", ""):
                error = "As duas novas palavras-passe não são iguais."
            elif new == f.get("current"):
                error = "A nova palavra-passe tem de ser diferente da atual."
            else:
                u["hash"] = generate_password_hash(new)
                save_json(DATA / "users.json", all_users)
                (DATA / "users.json").chmod(0o600)
                session["pw"] = pw_tag(u)
                done = True
    return render_template("password.html", error=error, done=done)


@app.post("/_admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- páginas e API ----------

@app.route("/_admin")
@app.route("/_admin/")
@login_required
def index():
    u = users()[session["user"]]
    return render_template("index.html", name=u.get("name") or session["user"])


def public_job(job):
    if not job:
        return None
    return {k: job.get(k) for k in ("id", "prompt", "user", "status", "log", "summary", "error", "files",
                                     "started", "ended", "cost", "commit")}


@app.get("/_admin/api/state")
@login_required
def api_state():
    last = request.args.get("job")
    return jsonify(
        running=public_job(current["job"]),
        job=public_job(jobs.get(last)) if last else None,
        draft=commits("main..rascunho"),
        published=publications(),
        preview_ok=preview_proc["p"] is not None and preview_proc["p"].poll() is None,
    )


@app.post("/_admin/api/job")
@login_required
def api_job():
    prompt = (request.form.get("prompt") or "").strip()
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not prompt:
        return jsonify(error="Escreva o que pretende alterar."), 400
    if len(prompt) > 6000:
        return jsonify(error="O pedido é demasiado longo."), 400
    if len(files) > MAX_FILES:
        return jsonify(error=f"No máximo {MAX_FILES} ficheiros por pedido."), 400
    if not lock.acquire(blocking=False):
        who = (current["job"] or {}).get("user", "outra pessoa")
        return jsonify(error=f"Já está um pedido em curso ({who}). Aguarde que termine."), 409
    try:
        reset_preview_tree()
        job_id = uuid.uuid4().hex[:12]
        notes, updir = save_uploads(files, job_id)
    except Exception as e:
        lock.release()
        return jsonify(error=str(e)), 400
    job = {"id": job_id, "prompt": prompt, "user": session["user"], "status": "a começar", "log": [],
           "started": time.time(), "upload_dir": updir, "cost": 0}
    jobs[job_id] = job
    current["job"] = job
    threading.Thread(target=job_worker, args=(job, notes), daemon=True).start()
    return jsonify(id=job_id)


@app.post("/_admin/api/cancel")
@login_required
def api_cancel():
    job, proc = current["job"], current["proc"]
    if job and proc:
        job["cancelled"] = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return jsonify(ok=True)


def busy():
    if not lock.acquire(blocking=False):
        return jsonify(error="Há um pedido em curso. Aguarde que termine."), 409
    return None


@app.post("/_admin/api/undo")
@login_required
def api_undo():
    """Desfaz a última alteração ainda não publicada."""
    if (r := busy()):
        return r
    try:
        if not git("rev-list", "main..rascunho"):
            return jsonify(error="Não há alterações por publicar."), 400
        reset_preview_tree()
        git("reset", "--hard", "-q", "HEAD~1")
        set_draft_state(session=None)
        start_preview()
        return jsonify(ok=True)
    finally:
        lock.release()


@app.post("/_admin/api/discard")
@login_required
def api_discard():
    """Deita fora todas as alterações por publicar (o rascunho volta a ser igual à loja)."""
    if (r := busy()):
        return r
    try:
        reset_preview_tree()
        git("reset", "--hard", "-q", "main")
        set_draft_state(session=None)
        start_preview()
        return jsonify(ok=True)
    finally:
        lock.release()


@app.post("/_admin/api/restore")
@login_required
def api_restore():
    """Põe no rascunho o conteúdo de uma versão publicada anterior (depois é preciso publicar)."""
    if (r := busy()):
        return r
    try:
        rev = str((request.get_json(silent=True) or {}).get("id", ""))
        if not re.fullmatch(r"[0-9a-f]{4,40}", rev) or rev not in {p["id"] for p in publications(50)}:
            return jsonify(error="Versão desconhecida."), 400
        reset_preview_tree()
        head = git("rev-parse", "HEAD")
        git("reset", "--hard", "-q", rev)
        git("reset", "--soft", "-q", head)
        if git("status", "--porcelain"):
            when = time.strftime("%d/%m %H:%M", time.localtime(int(git("show", "-s", "--format=%ct", rev))))
            git("commit", "-qm", json.dumps({"prompt": f"Repor a versão publicada a {when}",
                                             "user": session["user"], "summary": ""}, ensure_ascii=False))
        set_draft_state(session=None)
        start_preview()
        return jsonify(ok=True)
    finally:
        lock.release()


def restart_live():
    """Termina o processo da loja; o systemd (Restart=always) volta a arrancá-lo com o código novo."""
    pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "dehesa-loja"],
                         capture_output=True, text=True).stdout.strip()
    if not pid.isdigit() or pid == "0":
        return False
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    for _ in range(40):
        time.sleep(0.5)
        new = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "dehesa-loja"],
                             capture_output=True, text=True).stdout.strip()
        if new.isdigit() and new not in ("0", pid):
            time.sleep(1)
            return True
    return False


@app.post("/_admin/api/publish")
@login_required
def api_publish():
    if (r := busy()):
        return r
    try:
        pending = commits("main..rascunho", limit=100)
        if not pending:
            return jsonify(error="Não há alterações por publicar."), 400
        if git("status", "--porcelain", cwd=LOJA):
            return jsonify(error="A loja tem alterações manuais por guardar no servidor. Peça ajuda ao administrador."), 409
        errs = check_site(PREVIEW_PORT)
        if errs:
            return jsonify(error="A pré-visualização tem erros; não é seguro publicar.\n" + "\n".join(errs[:5])), 409
        title = "; ".join(p["prompt"][:80] for p in reversed(pending))[:300]
        old = git("rev-parse", "HEAD", cwd=LOJA)
        git("merge", "--no-ff", "-q", "rascunho", "-m",
            json.dumps({"kind": "publish", "user": session["user"], "title": title}, ensure_ascii=False), cwd=LOJA)
        if not restart_live():
            git("reset", "--hard", "-q", old, cwd=LOJA)
            return jsonify(error="Não foi possível reiniciar a loja. Peça ajuda ao administrador."), 500
        errs = []
        for _ in range(10):
            errs = check_site(LIVE_PORT)
            if not errs:
                break
            time.sleep(1)
        if errs:
            git("reset", "--hard", "-q", old, cwd=LOJA)
            restart_live()
            return jsonify(error="A loja não arrancou com as alterações; foi reposta a versão anterior."), 500
        # o rascunho passa a partir da versão publicada
        git("reset", "--hard", "-q", "main")
        set_draft_state(session=None)
        return jsonify(ok=True)
    finally:
        lock.release()


# ---------- proxy para a pré-visualização ----------

HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
       "proxy-authenticate", "content-length", "content-encoding", "host"}


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "HEAD"])
@app.route("/<path:path>", methods=["GET", "POST", "HEAD"])
@login_required
def proxy(path):
    if path.startswith("_admin"):
        abort(404)
    url = f"http://127.0.0.1:{PREVIEW_PORT}/{path}"
    if request.query_string:
        url += "?" + request.query_string.decode()
    hdrs = {k: v for k, v in request.headers if k.lower() not in HOP}
    try:
        r = requests.request(request.method, url, headers=hdrs, data=request.get_data(),
                             allow_redirects=False, timeout=30)
    except requests.RequestException:
        return render_template("preview_down.html"), 502
    resp = Response(r.content, status=r.status_code)
    for k, v in r.raw.headers.items():
        if k.lower() not in HOP and k.lower() != "set-cookie":
            resp.headers[k] = v
    for c in r.raw.headers.getlist("Set-Cookie"):
        resp.headers.add("Set-Cookie", c)
    return resp


def boot():
    # se o editor reiniciou a meio de um pedido, limpa a cópia de trabalho
    try:
        reset_preview_tree()
    except Exception as e:
        print("aviso:", e, file=sys.stderr)
    start_preview()


if __name__ == "__main__":
    from waitress import serve
    signal.signal(signal.SIGTERM, lambda *_: (stop_preview(), sys.exit(0)))
    boot()
    try:
        serve(app, host="127.0.0.1", port=int(os.environ.get("PORT", 5056)), threads=12)
    finally:
        stop_preview()
