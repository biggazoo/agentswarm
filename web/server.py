from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
import sqlite3, subprocess, os, signal
from auth import require_auth, login_user, make_session_token, SESSION_COOKIE

app = FastAPI()
templates = Jinja2Templates(directory="/home/gary/agentswarm/web/templates")
DB_PATH = "/home/gary/agentswarm/db/tasks.db"
WORKSPACE = "/home/gary/agentswarm/workspace"
LOG_DIR = "/home/gary/agentswarm/logs"


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ── AUTH ──────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if login_user(password):
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(SESSION_COOKIE, make_session_token(), httponly=True, max_age=86400)
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Wrong password"})


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── MAIN DASHBOARD ────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, auth=Depends(require_auth)):
    db = get_db()
    meta = db.execute("SELECT * FROM run_meta ORDER BY id DESC LIMIT 1").fetchone()
    stats = db.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done,
            SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) as running,
            SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN status='fix_needed' THEN 1 ELSE 0 END) as fix_needed
        FROM tasks
    """).fetchone()
    tokens = db.execute("SELECT COALESCE(SUM(tokens_used),0) as total FROM agent_log").fetchone()
    cost = round((tokens["total"] or 0) * 0.0000008, 4)

    try:
        commits = subprocess.check_output(
            ["git", "-C", WORKSPACE, "rev-list", "--count", "HEAD"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        commits = "0"

    try:
        output = subprocess.check_output(
            ["find", WORKSPACE, "-type", "f", "-not", "-path", "*/.git/*"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        file_count = len([f for f in output.split("\n") if f]) if output else 0
    except Exception:
        file_count = 0

    started = request.query_params.get("started")
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "meta": meta,
        "stats": stats,
        "cost": cost,
        "commits": commits,
        "file_count": file_count,
        "started": started,
    })


# ── HTMX PARTIALS ─────────────────────────────────────

@app.get("/partials/tasks", response_class=HTMLResponse)
async def partial_tasks(request: Request, auth=Depends(require_auth)):
    db = get_db()
    tasks = db.execute("""
        SELECT * FROM tasks ORDER BY
        CASE status WHEN 'running' THEN 1 WHEN 'pending' THEN 2
        WHEN 'fix_needed' THEN 3 WHEN 'failed' THEN 4 ELSE 5 END,
        priority ASC LIMIT 20
    """).fetchall()
    return templates.TemplateResponse("tasks.html", {"request": request, "tasks": tasks})


@app.get("/partials/agents", response_class=HTMLResponse)
async def partial_agents(request: Request, auth=Depends(require_auth)):
    db = get_db()
    agents = db.execute(
        "SELECT * FROM agent_log ORDER BY timestamp DESC LIMIT 15"
    ).fetchall()
    return templates.TemplateResponse("agents.html", {"request": request, "agents": agents})


# ── NEW RUN ───────────────────────────────────────────

@app.get("/run", response_class=HTMLResponse)
async def run_page(request: Request, auth=Depends(require_auth)):
    return templates.TemplateResponse("run.html", {"request": request})


@app.post("/run")
async def start_run(request: Request, spec: str = Form(...), auth=Depends(require_auth)):
    db = get_db()
    active = db.execute(
        "SELECT COUNT(*) as c FROM tasks WHERE status IN ('pending','running')"
    ).fetchone()
    if active["c"] > 0:
        return templates.TemplateResponse("run.html", {
            "request": request,
            "error": "A swarm is already active. Stop it first."
        })
    if len(spec.split()) < 5:
        return templates.TemplateResponse("run.html", {
            "request": request,
            "error": "Description too short. Be more specific."
        })
    os.makedirs(LOG_DIR, exist_ok=True)
    subprocess.Popen(
        ["python3", "/home/gary/agentswarm/main.py", spec],
        start_new_session=True,
        stdout=open(f"{LOG_DIR}/swarm_stdout.log", "a"),
        stderr=open(f"{LOG_DIR}/swarm_stderr.log", "a"),
    )
    return RedirectResponse("/?started=1", status_code=302)


# ── STOP SWARM ────────────────────────────────────────

@app.post("/stop")
async def stop_swarm(request: Request, auth=Depends(require_auth)):
    try:
        result = subprocess.check_output(["pgrep", "-f", "main.py"]).decode().strip()
        for pid in result.split("\n"):
            if pid:
                os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass
    try:
        db = get_db()
        db.execute("UPDATE tasks SET status='failed' WHERE status IN ('running','pending')")
        db.commit()
    except Exception:
        pass
    return RedirectResponse("/", status_code=302)


# ── LOGS ──────────────────────────────────────────────

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, auth=Depends(require_auth)):
    try:
        log_files = sorted(
            [f for f in os.listdir(LOG_DIR) if f.endswith(".log")],
            reverse=True
        )[:20]
    except Exception:
        log_files = []
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "log_files": log_files,
        "log_content": None,
        "active_file": None,
    })


@app.get("/logs/{filename}", response_class=HTMLResponse)
async def view_log(request: Request, filename: str, auth=Depends(require_auth)):
    safe_name = os.path.basename(filename)
    log_path = os.path.join(LOG_DIR, safe_name)
    try:
        with open(log_path, "r") as f:
            lines = f.readlines()[-100:]
        content = "".join(lines)
    except Exception:
        content = "Log not found."
    try:
        log_files = sorted(
            [f for f in os.listdir(LOG_DIR) if f.endswith(".log")],
            reverse=True
        )[:20]
    except Exception:
        log_files = []
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "log_files": log_files,
        "log_content": content,
        "active_file": safe_name,
    })


# ── FILES ─────────────────────────────────────────────

@app.get("/files", response_class=HTMLResponse)
async def files_page(request: Request, auth=Depends(require_auth)):
    try:
        output = subprocess.check_output(
            ["find", WORKSPACE, "-type", "f", "-not", "-path", "*/.git/*"],
            stderr=subprocess.DEVNULL
        ).decode().strip()
        files = [f.replace(WORKSPACE + "/", "") for f in output.split("\n") if f]
    except Exception:
        files = []
    return templates.TemplateResponse("files.html", {"request": request, "files": files})
