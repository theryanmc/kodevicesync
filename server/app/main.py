"""KODeviceSync server.

Stores a per-user "profile" of KOReader add-ons (plugins, patches, fonts,
style tweaks) so a fresh device can pull them all down with the
kodevicesync KOReader plugin.

Model:
  item  = one top-level entry inside a category folder on the device,
          e.g. category "plugins", name "statistics2.koplugin"
  file  = a file belonging to an item. Its path is relative to the
          category folder and always starts with the item name,
          e.g. "statistics2.koplugin/main.lua" or (single-file item) "2-my-patch.lua"
  blob  = file contents, stored per user and addressed by md5
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import secrets
import sqlite3
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel


def env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("KDS_DATA_DIR", "/data"))
# /plugin.zip redirects here, so devices always get the newest plugin release.
PLUGIN_URL = os.environ.get(
    "KDS_PLUGIN_URL",
    "https://github.com/theryanmc/kodevicesync.koplugin/releases/latest/download/kodevicesync.koplugin.zip",
)
ALLOW_SIGNUP = env_bool("KDS_ALLOW_SIGNUP", True)
SECURE_COOKIES = env_bool("KDS_SECURE_COOKIES", False)
MAX_UPLOAD_BYTES = int(os.environ.get("KDS_MAX_UPLOAD_MB", "200")) * 1024 * 1024
MAX_UNZIPPED_BYTES = MAX_UPLOAD_BYTES * 4
SESSION_COOKIE = "kds_session"
BLOB_GC_GRACE_SECONDS = 3600

KNOWN_CATEGORIES = {
    "plugins": "Plugins",
    "patches": "User patches",
    "fonts": "Fonts",
    "styletweaks": "Style tweaks",
}
CATEGORY_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens(
    id INTEGER PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    created INTEGER NOT NULL,
    last_used INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    updated INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    UNIQUE(user_id, category, name)
);
CREATE TABLE IF NOT EXISTS files(
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    md5 TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY(item_id, path)
);
"""


# --------------------------------------------------------------------------- db

def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATA_DIR / "kodevicesync.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="KODeviceSync", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def human_size(n: int | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def human_time(ts: int | None) -> str:
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


templates.env.filters["size"] = human_size
templates.env.filters["when"] = human_time


# --------------------------------------------------------------------- security

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt_hex, h_hex = stored.split("$")
        h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


def issue_token(conn: sqlite3.Connection, user_id: int, label: str, kind: str) -> str:
    tok = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute(
        "INSERT INTO tokens(token_hash, user_id, label, kind, created, last_used) VALUES(?,?,?,?,?,?)",
        (sha256_hex(tok), user_id, label[:100], kind, now, now),
    )
    return tok


def user_for_token(conn: sqlite3.Connection, tok: str | None):
    if not tok:
        return None
    row = conn.execute(
        "SELECT u.id, u.username, t.id AS token_id, t.last_used FROM tokens t "
        "JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?",
        (sha256_hex(tok),),
    ).fetchone()
    if row and time.time() - row["last_used"] > 60:
        conn.execute("UPDATE tokens SET last_used=? WHERE id=?", (int(time.time()), row["token_id"]))
    return row


def authenticate(conn: sqlite3.Connection, username: str, password: str):
    row = conn.execute("SELECT id, username, pw_hash FROM users WHERE username=?", (username.strip(),)).fetchone()
    if row and check_password(password, row["pw_hash"]):
        return row
    return None


def api_user(request: Request, conn: sqlite3.Connection):
    auth = request.headers.get("authorization", "")
    tok = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    user = user_for_token(conn, tok)
    if not user:
        raise HTTPException(401, "Not logged in, or this device was revoked. Log in again.")
    return user


def web_user(request: Request, conn: sqlite3.Connection):
    return user_for_token(conn, request.cookies.get(SESSION_COOKIE))


# ------------------------------------------------------------------- validation

def check_category(c: str) -> None:
    if not CATEGORY_RE.match(c or ""):
        raise HTTPException(400, f"Invalid category: {c!r}")


def check_name(n: str) -> None:
    if (not n or len(n) > 255 or n in (".", "..") or "/" in n or "\\" in n or "\0" in n):
        raise HTTPException(400, f"Invalid item name: {n!r}")


def check_path(p: str, name: str) -> None:
    parts = p.split("/")
    if (len(p) > 1024 or "\\" in p or "\0" in p or parts[0] != name
            or any(x in ("", ".", "..") for x in parts)):
        raise HTTPException(400, f"Invalid file path {p!r} for item {name!r}")


def check_md5(m: str) -> None:
    if not MD5_RE.match(m or ""):
        raise HTTPException(400, f"Invalid md5: {m!r}")


# ------------------------------------------------------------------------ blobs

def blob_dir(user_id: int) -> Path:
    return DATA_DIR / "users" / str(user_id) / "blobs"


def blob_path(user_id: int, md5: str) -> Path:
    return blob_dir(user_id) / md5[:2] / md5


def store_blob_bytes(user_id: int, data: bytes) -> str:
    md5 = hashlib.md5(data).hexdigest()
    dest = blob_path(user_id, md5)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".up-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    else:
        os.utime(dest)  # protect from GC grace window
    return md5


def gc_blobs(conn: sqlite3.Connection, user_id: int) -> None:
    """Delete blobs no item references any more (older than a grace period so
    in-flight pushes that uploaded blobs but haven't committed yet are safe)."""
    referenced = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT f.md5 FROM files f JOIN items i ON i.id=f.item_id WHERE i.user_id=?", (user_id,))
    }
    root = blob_dir(user_id)
    if not root.exists():
        return
    cutoff = time.time() - BLOB_GC_GRACE_SECONDS
    for p in root.glob("*/*"):
        try:
            if p.name not in referenced and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def commit_item(conn, user_id: int, category: str, name: str, files: list[tuple[str, str, int]], source: str) -> None:
    item_id = conn.execute(
        "INSERT INTO items(user_id, category, name, updated, source) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id, category, name) DO UPDATE SET updated=excluded.updated, source=excluded.source "
        "RETURNING id",
        (user_id, category, name, int(time.time()), source[:100]),
    ).fetchone()[0]
    conn.execute("DELETE FROM files WHERE item_id=?", (item_id,))
    conn.executemany(
        "INSERT INTO files(item_id, path, md5, size) VALUES(?,?,?,?)",
        [(item_id, p, m, s) for p, m, s in files],
    )


def load_items(conn, user_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT i.id, i.category, i.name, i.updated, i.source, f.path, f.md5, f.size "
        "FROM items i LEFT JOIN files f ON f.item_id=i.id WHERE i.user_id=? "
        "ORDER BY i.category, i.name COLLATE NOCASE, f.path",
        (user_id,),
    ).fetchall()
    items: dict[int, dict] = {}
    for r in rows:
        it = items.setdefault(r["id"], {
            "id": r["id"], "category": r["category"], "name": r["name"],
            "updated": r["updated"], "source": r["source"], "files": [],
        })
        if r["path"] is not None:
            it["files"].append({"path": r["path"], "md5": r["md5"], "size": r["size"]})
    return list(items.values())


# -------------------------------------------------------------------------- API

class LoginIn(BaseModel):
    username: str
    password: str
    device: str = "KOReader device"


class MissingIn(BaseModel):
    md5s: list[str]


class FileIn(BaseModel):
    path: str
    md5: str
    size: int


class ItemIn(BaseModel):
    category: str
    name: str
    files: list[FileIn]


class ItemRef(BaseModel):
    category: str
    name: str


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/login")
def api_login(body: LoginIn):
    with db() as conn:
        user = authenticate(conn, body.username, body.password)
        if not user:
            raise HTTPException(401, "Wrong username or password")
        tok = issue_token(conn, user["id"], body.device or "KOReader device", "device")
        return {"token": tok, "username": user["username"]}


@app.post("/api/logout")
def api_logout(request: Request):
    with db() as conn:
        user = api_user(request, conn)
        conn.execute("DELETE FROM tokens WHERE id=?", (user["token_id"],))
    return {"ok": True}


@app.get("/api/me")
def api_me(request: Request):
    with db() as conn:
        user = api_user(request, conn)
        return {"username": user["username"]}


@app.get("/api/manifest")
def api_manifest(request: Request):
    with db() as conn:
        user = api_user(request, conn)
        items = load_items(conn, user["id"])
        for it in items:
            it.pop("id")
        return {"items": items}


@app.post("/api/blobs/missing")
def api_missing(body: MissingIn, request: Request):
    with db() as conn:
        user = api_user(request, conn)
    missing = []
    for m in body.md5s:
        check_md5(m)
        p = blob_path(user["id"], m)
        if p.exists():
            os.utime(p)  # keep it out of GC while the push is in progress
        else:
            missing.append(m)
    return {"missing": missing}


@app.put("/api/blobs/{md5}")
async def api_put_blob(md5: str, request: Request):
    check_md5(md5)
    with db() as conn:
        user = api_user(request, conn)
    dest = blob_path(user["id"], md5)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".up-")
    h = hashlib.md5()
    size = 0
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File too large")
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != md5:
            raise HTTPException(400, "Checksum mismatch, upload corrupted")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"ok": True, "size": size}


@app.get("/api/blobs/{md5}")
def api_get_blob(md5: str, request: Request):
    check_md5(md5)
    with db() as conn:
        user = api_user(request, conn)
    p = blob_path(user["id"], md5)
    if not p.exists():
        raise HTTPException(404, "Blob not found")
    return FileResponse(p, media_type="application/octet-stream")


@app.post("/api/items")
def api_put_item(body: ItemIn, request: Request):
    check_category(body.category)
    check_name(body.name)
    if not body.files:
        raise HTTPException(400, "Item has no files")
    paths = set()
    for f in body.files:
        check_path(f.path, body.name)
        check_md5(f.md5)
        paths.add(f.path)
    if len(paths) != len(body.files):
        raise HTTPException(400, "Duplicate file paths")
    if body.name in paths and len(paths) > 1:
        raise HTTPException(400, "An item can be a single file or a folder, not both")
    with db() as conn:
        user = api_user(request, conn)
        for f in body.files:
            p = blob_path(user["id"], f.md5)
            if not p.exists():
                raise HTTPException(409, f"Missing blob for {f.path}; upload it first")
        token_label = conn.execute("SELECT label FROM tokens WHERE id=?", (user["token_id"],)).fetchone()
        commit_item(conn, user["id"], body.category, body.name,
                    [(f.path, f.md5, blob_path(user["id"], f.md5).stat().st_size) for f in body.files],
                    token_label["label"] if token_label else "device")
        gc_blobs(conn, user["id"])
    return {"ok": True}


@app.post("/api/items/delete")
def api_delete_item(body: ItemRef, request: Request):
    with db() as conn:
        user = api_user(request, conn)
        conn.execute("DELETE FROM items WHERE user_id=? AND category=? AND name=?",
                     (user["id"], body.category, body.name))
        gc_blobs(conn, user["id"])
    return {"ok": True}


# -------------------------------------------------------------------------- web

def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    ctx.setdefault("allow_signup", ALLOW_SIGNUP)
    return templates.TemplateResponse(request, template, ctx)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def set_session(resp: Response, tok: str) -> None:
    resp.set_cookie(SESSION_COOKIE, tok, httponly=True, samesite="lax", secure=SECURE_COOKIES,
                    max_age=60 * 60 * 24 * 90)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", mode="login")


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    with db() as conn:
        user = authenticate(conn, username, password)
        if not user:
            return render(request, "login.html", mode="login", err="Wrong username or password", username=username)
        ua = request.headers.get("user-agent", "")[:60]
        tok = issue_token(conn, user["id"], f"Web browser ({ua})" if ua else "Web browser", "web")
    resp = redirect("/")
    set_session(resp, tok)
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if not ALLOW_SIGNUP:
        return redirect("/login?err=Sign-ups+are+disabled+on+this+server")
    return render(request, "login.html", mode="register")


@app.post("/register")
def register_submit(request: Request, username: str = Form(...), password: str = Form(...),
                    password2: str = Form(...)):
    if not ALLOW_SIGNUP:
        return redirect("/login?err=Sign-ups+are+disabled+on+this+server")
    username = username.strip()
    err = None
    if not USERNAME_RE.match(username):
        err = "Username may only contain letters, numbers, dot, dash and underscore."
    elif len(password) < 8:
        err = "Password must be at least 8 characters."
    elif password != password2:
        err = "Passwords don't match."
    if err:
        return render(request, "login.html", mode="register", err=err, username=username)
    with db() as conn:
        try:
            uid = conn.execute("INSERT INTO users(username, pw_hash, created) VALUES(?,?,?) RETURNING id",
                               (username, hash_password(password), int(time.time()))).fetchone()[0]
        except sqlite3.IntegrityError:
            return render(request, "login.html", mode="register", err="That username is taken.", username=username)
        tok = issue_token(conn, uid, "Web browser", "web")
    resp = redirect("/?msg=Profile+created.+Install+the+KOReader+plugin+to+get+started.")
    set_session(resp, tok)
    return resp


@app.post("/logout")
def logout(request: Request):
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        with db() as conn:
            conn.execute("DELETE FROM tokens WHERE token_hash=?", (sha256_hex(tok),))
    resp = redirect("/login")
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db() as conn:
        user = web_user(request, conn)
        if not user:
            return redirect("/login")
        items = load_items(conn, user["id"])
        devices = conn.execute(
            "SELECT id, label, kind, created, last_used FROM tokens WHERE user_id=? ORDER BY last_used DESC",
            (user["id"],),
        ).fetchall()
    order = list(KNOWN_CATEGORIES) + sorted({i["category"] for i in items} - set(KNOWN_CATEGORIES))
    groups = []
    for cat in order:
        cat_items = [i for i in items if i["category"] == cat]
        for i in cat_items:
            i["size"] = sum(f["size"] for f in i["files"])
            i["count"] = len(i["files"])
        groups.append({"id": cat, "label": KNOWN_CATEGORIES.get(cat, cat), "entries": cat_items})
    total = sum(i["size"] for i in items)
    return render(request, "dashboard.html", user=user, groups=groups, devices=devices,
                  categories=KNOWN_CATEGORIES, total_size=total, item_count=len(items),
                  current_token_id=user["token_id"], server_url=str(request.base_url).rstrip("/"))


class UploadError(Exception):
    pass


def default_plugin_name(base: str) -> str:
    base = re.sub(r"[-_.](main|master)$", "", base) or "plugin"
    return base if base.endswith(".koplugin") else base + ".koplugin"


def parse_upload(category: str, filename: str, data: bytes, name_override: str) -> dict[str, dict[str, bytes]]:
    """Turn an uploaded file into {item_name: {path_under_category: bytes}}."""
    filename = PurePosixPath(filename.replace("\\", "/")).name or "upload"
    name_override = name_override.strip()
    if not filename.lower().endswith(".zip"):
        if category == "plugins":
            raise UploadError("Upload plugins as a .zip (e.g. the release zip or GitHub's “Download ZIP”).")
        name = name_override or filename
        return {name: {name: data}}

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UploadError("That isn't a valid zip file.")
    entries: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = [x for x in info.filename.replace("\\", "/").split("/") if x not in ("", ".")]
        if not parts or ".." in parts or parts[0] == "__MACOSX" or parts[-1] in (".DS_Store", "Thumbs.db"):
            continue
        total += info.file_size
        if total > MAX_UNZIPPED_BYTES:
            raise UploadError("Zip contents are too large.")
        entries["/".join(parts)] = info
    if not entries:
        raise UploadError("The zip is empty.")

    if category == "plugins":
        roots = [str(PurePosixPath(p).parent) for p in entries if PurePosixPath(p).name == "main.lua"]
        roots = ["" if r == "." else r for r in roots]
        if not roots:
            raise UploadError("That zip doesn't look like a KOReader plugin (no main.lua found).")
        root = min(roots, key=lambda r: (r.count("/") + (1 if r else 0), r))
        base = PurePosixPath(root).name if root else PurePosixPath(filename).stem
        name = default_plugin_name(name_override or base)
        prefix = root + "/" if root else ""
        return {name: {f"{name}/{p[len(prefix):]}": zf.read(i) for p, i in entries.items() if p.startswith(prefix)}}

    # Strip a single wrapping folder, e.g. "MyFonts/..."
    tops = {p.split("/")[0] for p in entries}
    wrapper = next(iter(tops)) if len(tops) == 1 and all("/" in p for p in entries) else None

    if category == "fonts":
        # Keep a font upload together as one folder item.
        name = name_override or wrapper or PurePosixPath(filename).stem
        strip = len(wrapper) + 1 if wrapper else 0
        return {name: {f"{name}/{p[strip:]}": zf.read(i) for p, i in entries.items()}}

    # patches / styletweaks / others: each top-level entry becomes its own item.
    out: dict[str, dict[str, bytes]] = {}
    strip = len(wrapper) + 1 if wrapper else 0
    for p, i in entries.items():
        rel = p[strip:]
        out.setdefault(rel.split("/")[0], {})[rel] = zf.read(i)
    return out


@app.post("/upload")
async def upload(request: Request, category: str = Form(...), name: str = Form(""),
                 file: UploadFile = File(...)):
    with db() as conn:
        user = web_user(request, conn)
    if not user:
        return redirect("/login")
    if category not in KNOWN_CATEGORIES:
        return redirect("/?err=Unknown+category")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return redirect("/?err=File+too+large")
    try:
        parsed = parse_upload(category, file.filename or "upload", data, name)
        for item_name in parsed:
            check_name(item_name)
            for p in parsed[item_name]:
                check_path(p, item_name)
    except UploadError as e:
        return redirect("/?" + _q(err=str(e)))
    except HTTPException as e:
        return redirect("/?" + _q(err=str(e.detail)))
    with db() as conn:
        for item_name, files in parsed.items():
            rows = [(p, store_blob_bytes(user["id"], b), len(b)) for p, b in files.items()]
            commit_item(conn, user["id"], category, item_name, rows, "web upload")
        gc_blobs(conn, user["id"])
    names = ", ".join(parsed)
    return redirect("/?" + _q(msg=f"Added to {KNOWN_CATEGORIES[category]}: {names}"))


def _q(**kw) -> str:
    from urllib.parse import urlencode
    return urlencode(kw)


@app.post("/items/{item_id}/delete")
def delete_item(item_id: int, request: Request):
    with db() as conn:
        user = web_user(request, conn)
        if not user:
            return redirect("/login")
        conn.execute("DELETE FROM items WHERE id=? AND user_id=?", (item_id, user["id"]))
        gc_blobs(conn, user["id"])
    return redirect("/?msg=Removed+from+profile")


@app.get("/items/{item_id}/download")
def download_item(item_id: int, request: Request):
    with db() as conn:
        user = web_user(request, conn)
        if not user:
            return redirect("/login")
        item = next((i for i in load_items(conn, user["id"]) if i["id"] == item_id), None)
    if not item:
        raise HTTPException(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in item["files"]:
            zf.write(blob_path(user["id"], f["md5"]), f["path"])
    fname = re.sub(r"[^A-Za-z0-9_.-]", "_", item["name"]) + ".zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/devices/{token_id}/revoke")
def revoke_device(token_id: int, request: Request):
    with db() as conn:
        user = web_user(request, conn)
        if not user:
            return redirect("/login")
        conn.execute("DELETE FROM tokens WHERE id=? AND user_id=?", (token_id, user["id"]))
    return redirect("/?msg=Device+signed+out")


@app.get("/plugin.zip")
def plugin_zip():
    """Short link for installing the KOReader plugin: redirects to its latest GitHub release."""
    return RedirectResponse(PLUGIN_URL, status_code=302)
