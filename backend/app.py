# backend/app.py
from fastapi import FastAPI, UploadFile, File, HTTPException, Body, Request
from fastapi.responses import FileResponse, StreamingResponse
from typing import Literal, Dict, Any, Optional, List
from pydantic import BaseModel
from collections import defaultdict
from datetime import date, datetime
import pandas as pd
import os, shutil, json, math
import hashlib
import base64


app = FastAPI(title="TaskFlow Pro API")

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # for local dev; tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# DEMO AUTH + RBAC (lightweight)
# Search: "DEMO AUTH + RBAC"
# ============================================================

AUTH_ENABLED = os.getenv("TF_AUTH", "1") == "1"
TF_API_KEYS = os.getenv("TF_API_KEYS", "")

try:
    API_KEYS = json.loads(TF_API_KEYS) if TF_API_KEYS.strip() else {
        "admin": "dev-admin-key",
        "user": "dev-user-key",
        "viewer": "dev-viewer-key",
    }
except Exception:
    API_KEYS = {"admin": "dev-admin-key"}

ROLE_ORDER = {"viewer": 0, "user": 1, "admin": 2}

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------- paths ----------
# ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# DATA_DIR = os.path.join(ROOT, "data")
# os.makedirs(DATA_DIR, exist_ok=True)

AllowedType = Literal["tasks", "members", "inventory", "reservations", "projects"]

# def _path_for(dtype: str) -> str:
#     ext = "json" if dtype == "projects" else "csv"
#     return os.path.join(DATA_DIR, f"{dtype}.{ext}")


def _path_for(dtype: str) -> str:
    ext = "json" if dtype == "projects" else "csv"
    return os.path.join(DATA_DIR, f"{dtype}.{ext}")

def _read_csv_safe(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)

    # normalize email column if present
    if "email" in df.columns:
        df["email"] = df["email"].astype(str).str.strip().str.lower()

    return df


def _role_from_members(email: str) -> str:
    df = _read_csv_safe(_path_for("members"))
    if df.empty or "email" not in df.columns:
        return "viewer"

    df["email"] = df["email"].astype(str).str.strip().str.lower()
    row = df[df["email"] == (email or "").strip().lower()]
    if row.empty:
        return "viewer"

    # Option C supported:
    # - Prefer rbac_role if present
    # - Fallback to role (legacy)
    # - If role contains non-RBAC values (Backend/Ops/etc), treat as viewer
    role = (
        str(row.iloc[0].get("rbac_role", "")).strip().lower()
        or str(row.iloc[0].get("role", "")).strip().lower()
        or "viewer"
    )

    return role if role in ROLE_ORDER else "viewer"



def _audit_line(**kv):
    kv["ts"] = datetime.utcnow().isoformat()
    try:
        with open(os.path.join(DATA_DIR, "audit.log"), "a", encoding="utf-8") as f:
            f.write(json.dumps(kv, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _require(request: Request, min_role: str = "viewer"):
    if not AUTH_ENABLED:
        return {"email": "demo@local", "role": "admin"}

    api_key = request.headers.get("x-api-key", "").strip()
    email = request.headers.get("x-user-email", "").strip().lower()
    project = request.headers.get("x-project-id", "").strip()

    if not api_key or api_key not in set(API_KEYS.values()):
        _audit_line(email=email, project=project, allow=False, why="bad_key", path=request.url.path)
        raise HTTPException(401, "Unauthorized")

    role = _role_from_members(email) if email else "viewer"

    if ROLE_ORDER.get(role, 0) < ROLE_ORDER.get(min_role, 0):
        _audit_line(email=email, role=role, project=project, allow=False, why="insufficient_role", path=request.url.path)
        raise HTTPException(403, f"Requires role '{min_role}'")

    _audit_line(email=email, role=role, project=project, allow=True, path=request.url.path)
    return {"email": email, "role": role, "project": project}

@app.middleware("http")
async def rbac_middleware(request: Request, call_next):
    is_write = request.method.upper() in ("POST", "PUT", "PATCH", "DELETE")

    if not AUTH_ENABLED:
        return await call_next(request)

    path = request.url.path or ""

    if is_write:
        if path.startswith("/import") or path.startswith("/members") or path.startswith("/admin"):
            _require(request, "admin")
        else:
            _require(request, "user")
    else:
        if path in ("/health", "/api/health", "/ai/health"):
            return await call_next(request)
        _require(request, "viewer")

    return await call_next(request)

@app.get("/auth/whoami")
def whoami(request: Request):
    email = request.headers.get("x-user-email", "")
    return {"email": email, "role": _role_from_members(email)}

# ============================================================
# BASIC HEALTH
# ============================================================

# @app.get("/health")
# def health():
#     return {"status": "ok"}

# @app.get("/files")
# def list_files():
#     files = []
#     for name in ["tasks.csv","members.csv","inventory.csv","reservations.csv","projects.json","weights.json","audit.log"]:
#         p = os.path.join(DATA_DIR, name)
#         files.append({"name": name, "exists": os.path.exists(p)})
#     return {"files": files}

# ---------- Encrypted Collaboration Notes ----------
ENC_KEY = os.getenv("TF_ENC_KEY", "secret").encode()

def _crypt(text: str) -> str:
    h = hashlib.sha256(ENC_KEY).digest()
    data = text.encode("utf-8")
    out = bytes([b ^ h[i % len(h)] for i, b in enumerate(data)])
    return base64.b64encode(out).decode()

def _decrypt(token: str) -> str:
    h = hashlib.sha256(ENC_KEY).digest()
    data = base64.b64decode(token.encode())
    out = bytes([b ^ h[i % len(h)] for i, b in enumerate(data)])
    return out.decode("utf-8", errors="ignore")

def _notes_path(project: str) -> str:
    safe = project or "default"
    return os.path.join(DATA_DIR, f"notes_{safe}.json")

@app.get("/collab/notes")
def get_notes(request: Request):
    user = _require(request, "viewer")
    path = _notes_path(user["project"])
    if not os.path.exists(path):
        return {"items": []}

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    items = []
    for r in raw:
        items.append({
            "ts": r["ts"],
            "author": r["author"],
            "text": _decrypt(r["text"])
        })
    return {"items": items}

@app.post("/collab/notes")
def add_note(request: Request, body: Dict[str, Any] = Body(...)):
    user = _require(request, "user")
    text = str(body.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "Empty note")

    path = _notes_path(user["project"])
    items = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)

    items.append({
        "ts": datetime.utcnow().isoformat(),
        "author": user["email"],
        "text": _crypt(text)
    })

    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    return {"ok": True}

# ---------- Admin Audit Log ----------
@app.get("/admin/audit_log")
def download_audit(request: Request):
    _require(request, "admin")
    path = os.path.join(DATA_DIR, "audit.log")
    if not os.path.exists(path):
        raise HTTPException(404, "No audit log")
    return FileResponse(path, filename="audit.log", media_type="text/plain")



# ---------- basics ----------
@app.get("/health")
def health():
    return {"status": "ok", "errors": 0}

@app.get("/ai/health")
def ai_health():
    model_path = os.path.join(DATA_DIR, "task_risk_model.joblib")
    meta_path = os.path.join(DATA_DIR, "task_risk_model.meta.json")
    return {
        "ok": True,
        "has_model": os.path.exists(model_path),
        "has_meta": os.path.exists(meta_path),
        "has_events": os.path.exists(os.path.join(DATA_DIR, "events.csv")),
    }


@app.post("/import/{dtype}")
async def import_file(dtype: AllowedType, file: UploadFile = File(...)):
    # ✅ Prevent accidental wipe of members.csv
    # Use /members/upsert_many for members edits
    if dtype == "members":
        raise HTTPException(
            400,
            "Do not use /import/members (it overwrites the file). Use /members/upsert_many instead."
        )

    want_ext = "json" if dtype == "projects" else "csv"
    given_ext = (file.filename.split(".")[-1] or "").lower()
    if given_ext != want_ext:
        raise HTTPException(400, f"Expected .{want_ext} for '{dtype}', got .{given_ext or 'unknown'}")

    dest = _path_for(dtype)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    return {"saved": os.path.abspath(dest)}


@app.get("/export/{dtype}")
def export_file(dtype: AllowedType):
    path = _path_for(dtype)
    if not os.path.exists(path):
        raise HTTPException(404, f"No data found for '{dtype}'")

    media = "application/json" if dtype == "projects" else "text/csv"
    return FileResponse(
        path=path,
        media_type=media,
        filename=os.path.basename(path),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )
# ---------- Learning loop: proof bundle ----------
import io, zipfile
from fastapi.responses import StreamingResponse

@app.get("/proof_bundle")
def export_proof_bundle():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # events
        events_path = os.path.join(DATA_DIR, "events.csv")
        if os.path.exists(events_path):
            z.write(events_path, arcname="events.csv")

        # model + meta
        model_path = os.path.join(DATA_DIR, "task_risk_model.joblib")
        meta_path = os.path.join(DATA_DIR, "task_risk_model.meta.json")
        if os.path.exists(model_path):
            z.write(model_path, arcname="task_risk_model.joblib")
        if os.path.exists(meta_path):
            z.write(meta_path, arcname="task_risk_model.meta.json")

        # tasks snapshot (optional but very good proof)
        tasks_path = _path_for("tasks")
        if os.path.exists(tasks_path):
            z.write(tasks_path, arcname="tasks.csv")

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=learning_loop_proof.zip"}
    )


@app.get("/files")
def list_files():
    files = []
    for name in ["tasks.csv","members.csv","inventory.csv","reservations.csv","projects.json","weights.json"]:
        p = os.path.join(DATA_DIR, name)
        files.append({"name": name, "exists": os.path.exists(p), "size_bytes": os.path.getsize(p) if os.path.exists(p) else 0})
    return {"data_dir": os.path.abspath(DATA_DIR), "files": files}

# ---------- utils ----------
# def _read_csv_safe(path: str) -> pd.DataFrame:
    # if not os.path.exists(path):
    #     return pd.DataFrame()
    # return pd.read_csv(path)

def _parse_date_safe(s: Any):
    if pd.isna(s) or s == "": return None
    try: return datetime.strptime(str(s), "%Y-%m-%d").date()
    except Exception: return None

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

# ---------- scoring ----------
def _read_weights() -> Dict[str, float]:
    wpath = os.path.join(DATA_DIR, "weights.json")
    if not os.path.exists(wpath):
        return {"wU":0.35,"wI":0.25,"wD":0.20,"wL":0.15,"wH":0.05}
    with open(wpath, "r") as f:
        return json.load(f)

def _assignee_load(tasks: pd.DataFrame, members: pd.DataFrame) -> Dict[str, float]:
    if tasks.empty: return {}
    active = tasks[~tasks["status"].astype(str).str.lower().isin(["done","completed","closed","resolved"])].copy()
    by_owner = active.groupby("assignee_email")["story_points"].sum(min_count=1).to_dict()
    cap = {str(r["email"]).lower(): r["weekly_capacity"] for _, r in members.iterrows()} if not members.empty else {}
    out = {}
    for owner, pts in by_owner.items():
        capacity = float(cap.get(str(owner).lower(), 10))
        ratio = float(pd.to_numeric(pts, errors="coerce") or 0.0) / max(capacity, 1.0)
        out[str(owner).lower()] = _clamp(ratio * 5.0, 0, 5)
    return out

def _dependency_risk(task_row: pd.Series, status_by_id: Dict[str, str]) -> int:
    """Count unresolved blockers; treat NaN/blank as none; ignore self/unknown IDs."""
    task_id = str(task_row.get("id", "")).strip()
    val = task_row.get("dependencies")
    if pd.isna(val): return 0
    raw = str(val).strip()
    if not raw: return 0
    deps = [d.strip() for d in raw.split(",") if d.strip()]
    deps = [d for d in deps if d != task_id and d in status_by_id]
    count = 0
    for dep in deps:
        st = (status_by_id.get(dep, "") or "").lower()
        if st not in ["done","completed","closed","resolved"]:
            count += 1
    return int(_clamp(count, 0, 5))

def _urgency(due):
    if due is None: return 0.0
    days_to_due = (due - date.today()).days
    return _clamp(10 - days_to_due, 0, 10)

@app.get("/tasks/prioritized")
def prioritized():
    tasks = _read_csv_safe(_path_for("tasks"))
    members = _read_csv_safe(_path_for("members"))

    if tasks.empty:
        raise HTTPException(404, "No tasks found. Import tasks.csv first.")

    # --- normalize / compatibility ---
    # Accept either due_date or due (legacy). Prefer due_date.
    if "due_date" not in tasks.columns and "due" in tasks.columns:
        tasks["due_date"] = tasks["due"]
    if "due" not in tasks.columns and "due_date" in tasks.columns:
        tasks["due"] = tasks["due_date"]

    # Ensure required columns exist (for UI + scoring)
    for col, default in [
        ("id", ""),
        ("title", ""),
        ("project", ""),
        ("assignee_email", ""),
        ("status", "Todo"),
        ("due_date", ""),
        ("due", ""),
        ("impact", 0.0),
        ("story_points", 0.0),
        ("dependencies", ""),
    ]:
        if col not in tasks.columns:
            tasks[col] = default

    # Clean text columns (avoid NaN in UI inputs)
    for col in ["id", "title", "project", "assignee_email", "status", "due_date", "due", "dependencies"]:
        tasks[col] = tasks[col].astype(str).fillna("").replace("nan", "").str.strip()

    # Numeric columns
    tasks["impact"] = pd.to_numeric(tasks["impact"], errors="coerce").fillna(0.0)
    tasks["story_points"] = pd.to_numeric(tasks["story_points"], errors="coerce").fillna(0.0)

    # Precompute helpers
    weights = _read_weights()
    status_by_id = {str(r.get("id", "")).strip(): str(r.get("status", "")).strip() for _, r in tasks.iterrows()}
    load_by_owner = _assignee_load(tasks, members)

    scored = []
    for _, r in tasks.iterrows():
        tid = str(r.get("id", "")).strip()
        if not tid:
            continue

        due_str = str(r.get("due_date", "") or "").strip()
        due = _parse_date_safe(due_str)

        U = _urgency(due)
        I = float(r.get("impact", 0.0) or 0.0)
        D = float(_dependency_risk(r, status_by_id))
        owner = str(r.get("assignee_email", "") or "").strip().lower()
        L = float(load_by_owner.get(owner, 0.0))
        H = 0.0

        S = (
            weights.get("wU", 0.35) * U +
            weights.get("wI", 0.25) * I +
            weights.get("wD", 0.20) * D +
            weights.get("wL", 0.15) * L +
            weights.get("wH", 0.05) * H
        )

        scored.append({
            "id": tid,
            "title": str(r.get("title", "") or ""),
            "project": str(r.get("project", "") or ""),
            "assignee_email": str(r.get("assignee_email", "") or ""),
            "status": str(r.get("status", "Todo") or "Todo"),

            # Keep both for compatibility
            "due_date": due_str,
            "due": due_str,

            # Editable fields for UI
            "impact": I,
            "story_points": float(r.get("story_points", 0.0) or 0.0),
            "dependencies": str(r.get("dependencies", "") or ""),

            # Computed fields
            "deps_open": int(D),
            "owner_load_0_5": round(L, 2),
            "urgency_0_10": round(U, 2),
            "score": round(float(S), 2),
            "reason": f"U:{round(U,2)} I:{I} D:{int(D)} L:{round(L,2)}"
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"count": len(scored), "items": scored}

# ---------- Adding Members ----------
from pydantic import BaseModel
from typing import List, Optional

class MemberUpsert(BaseModel):
    email: str
    full_name: Optional[str] = ""

    # Legacy column (your UI currently sends this)
    role: Optional[str] = ""

    # Option C columns (RBAC-aware)
    job_role: Optional[str] = ""
    rbac_role: Optional[str] = "viewer"

    weekly_capacity: Optional[float] = 0.0

@app.post("/members/upsert_many")
def members_upsert_many(rows: List[MemberUpsert]):
    path = _path_for("members")
    df = _read_csv_safe(path)

    # Ensure dataframe has the new schema
    wanted_cols = ["email", "full_name", "job_role", "rbac_role", "weekly_capacity"]

    if df.empty:
        df = pd.DataFrame(columns=wanted_cols)
    else:
        # Backward compat: if old CSV has "role" but not "job_role", treat it as job_role
        if "job_role" not in df.columns and "role" in df.columns:
            df["job_role"] = df["role"]

        # Ensure columns exist
        for c in wanted_cols:
            if c not in df.columns:
                df[c] = ""

        # Optional: drop legacy "role" column to stop confusion
        if "role" in df.columns:
            df = df.drop(columns=["role"])

        df = df[wanted_cols]

    # index by email
    df["email"] = df["email"].astype(str).str.strip().str.lower()
    idx = {str(r["email"]).strip().lower(): i for i, r in df.iterrows() if pd.notna(r.get("email"))}

    updated = 0
    for r in rows:
        email = (r.email or "").strip().lower()
        if not email:
            continue

        full_name = (r.full_name or "").strip()
        job_role = (r.job_role or "").strip() or (r.role or "").strip()
        rbac_role_in = (r.rbac_role or "").strip().lower()
        cap = float(r.weekly_capacity or 0.0)
        if rbac_role_in and (rbac_role_in not in ROLE_ORDER):
            rbac_role_in = ""

        # cap = float(r.weekly_capacity or 0.0)

        if email in idx:
            
            i = idx[email]
            
            df.at[i, "full_name"] = full_name
            if job_role:
                df.at[i, "job_role"] = job_role
            if rbac_role_in in ROLE_ORDER:
                df.at[i, "rbac_role"] = rbac_role_in
            else:
                cur = str(df.at[i, "rbac_role"]) if "rbac_role" in df.columns else ""
                cur = (cur or "").strip().lower()
                df.at[i, "rbac_role"] = cur if cur in ROLE_ORDER else "viewer"
            df.at[i, "weekly_capacity"] = cap
        else:
            rr = rbac_role_in if rbac_role_in in ROLE_ORDER else "viewer"
            df = pd.concat([df, pd.DataFrame([{
                "email": email,
                "full_name": full_name,
                "job_role": job_role,
                "rbac_role": rr,
                "weekly_capacity": cap,
            }])], ignore_index=True)
            idx[email] = len(df) - 1

        updated += 1

    df["weekly_capacity"] = pd.to_numeric(df["weekly_capacity"], errors="coerce").fillna(0.0)
    df.to_csv(path, index=False)
    return {"updated": updated, "count": int(len(df))}


# ---------- inventory ----------
def _read_inventory():
    inv = _read_csv_safe(_path_for("inventory"))
    if inv.empty:
        raise HTTPException(404, "No inventory found. Import inventory.csv first.")
    required = {"sku","name","on_hand","reorder_point","lead_time_days"}
    missing = required - set(inv.columns)
    if missing:
        raise HTTPException(400, f"Missing columns in inventory.csv: {sorted(missing)}")
    inv["on_hand"] = pd.to_numeric(inv["on_hand"], errors="coerce").fillna(0).astype(float)
    inv["reorder_point"] = pd.to_numeric(inv["reorder_point"], errors="coerce").fillna(0).astype(float)
    inv["lead_time_days"] = pd.to_numeric(inv["lead_time_days"], errors="coerce").fillna(0).astype(int)
    return inv

def _read_reservations():
    res = _read_csv_safe(_path_for("reservations"))
    if res.empty:
        return pd.DataFrame(columns=["reservation_id","task_id","sku","qty","planned_date","actual_date"])
    required = {"reservation_id","task_id","sku","qty","planned_date"}
    missing = required - set(res.columns)
    if missing:
        raise HTTPException(400, f"Missing columns in reservations.csv: {sorted(missing)}")
    res["qty"] = pd.to_numeric(res["qty"], errors="coerce").fillna(0).astype(float)
    return res

def _date_or_none(s):
    if pd.isna(s) or str(s).strip()=="":
        return None
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date()
    except Exception:
        return None

def _group_daily_consumption(reservations: pd.DataFrame):
    daily = defaultdict(lambda: defaultdict(float))
    if reservations.empty: return daily
    for _, r in reservations.iterrows():
        sku = str(r["sku"])
        day = _date_or_none(r.get("actual_date")) or _date_or_none(r.get("planned_date"))
        if not day:
            continue
        qty = float(r.get("qty", 0) or 0)
        daily[sku][day] += qty
    return daily

def _sma_days_to_stockout(on_hand: float, daily_series: list[tuple[date, float]], window_days: int = 28):
    if not daily_series:
        return math.inf, 0.0
    daily_series.sort(key=lambda x: x[0])
    cutoff = date.today() - pd.Timedelta(days=window_days).to_pytimedelta()
    recent = [v for d, v in daily_series if d >= cutoff]
    if not recent:
        recent = [v for _, v in daily_series]
    rate = sum(recent) / max(1, len(recent))
    if rate <= 0:
        return math.inf, 0.0
    dts = on_hand / rate
    return float(dts), float(rate)

# --- ML forecast helper (Holt's linear) ---
def _holt_linear(series: list[tuple[date, float]], alpha=0.5, beta=0.3, horizon=14):
    """
    Pure-Python Holt's linear method.
    series: [(date, qty_used_that_day), ...]
    Returns: forecasted average daily usage over the next `horizon` days.
    """
    if not series:
        return 0.0

    # sort and fill missing days with 0 usage
    series = sorted(series, key=lambda x: x[0])
    start = series[0][0]
    end = series[-1][0]
    days_map = {d: float(v) for d, v in series}
    filled = []
    total_days = (end - start).days + 1
    for i in range(total_days):
        di = start + pd.Timedelta(days=i).to_pytimedelta()
        filled.append((di, days_map.get(di, 0.0)))

    # init level (l) and trend (b)
    l = filled[0][1]
    b = (filled[1][1] - filled[0][1]) if len(filled) > 1 else 0.0

    # recursive updates
    for t in range(1, len(filled)):
        y = filled[t][1]
        l_next = alpha * y + (1 - alpha) * (l + b)
        b_next = beta * (l_next - l) + (1 - beta) * b
        l, b = l_next, b_next

    # forecast next horizon days, average them
    preds = [max(0.0, l + k * b) for k in range(1, horizon + 1)]
    rate = sum(preds) / max(1, len(preds))
    return float(rate)

# ---------- Learning loop: event logging ----------
EVENTS_PATH = os.path.join(DATA_DIR, "events.csv")

class UIEvent(BaseModel):
    event_name: str
    task_id: Optional[str] = ""
    meta: Dict[str, Any] = {}
    ts: Optional[str] = None

def _append_event(row: dict):
    import csv
    os.makedirs(DATA_DIR, exist_ok=True)
    header = ["ts", "event_name", "task_id", "meta_json"]
    exists = os.path.exists(EVENTS_PATH)
    with open(EVENTS_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not exists:
            w.writeheader()
        w.writerow(row)

@app.post("/events/log")
def log_event(e: UIEvent):
    ts = e.ts or datetime.utcnow().isoformat()
    _append_event({
        "ts": ts,
        "event_name": (e.event_name or "").strip(),
        "task_id": (e.task_id or "").strip(),
        "meta_json": json.dumps(e.meta or {}, ensure_ascii=False),
    })
    return {"ok": True}

@app.get("/export/events")
def export_events():
    if not os.path.exists(EVENTS_PATH):
        raise HTTPException(404, "No events logged yet.")
    return FileResponse(
        path=EVENTS_PATH,
        media_type="text/csv",
        filename="events.csv",
        headers={"Cache-Control": "no-store"},
    )


# ---------- AI: Task delay-risk prediction ----------
from pathlib import Path

MODEL_PATH = os.path.join(DATA_DIR, "task_risk_model.joblib")
_model_cache = {"model": None}

def _load_risk_model():
    if _model_cache["model"] is not None:
        return _model_cache["model"]
    if not os.path.exists(MODEL_PATH):
        return None
    try:
        import joblib
        _model_cache["model"] = joblib.load(MODEL_PATH)
        return _model_cache["model"]
    except Exception:
        return None

def _days_to_due(due_date_str: str) -> float:
    d = _parse_date_safe(due_date_str)
    if d is None:
        return 999.0
    return float((d - date.today()).days)


@app.get("/ai/task_risk")
def task_risk():
    """
    Returns delay risk per task id in [0,1].
    Requires a trained model saved at data/task_risk_model.joblib
    """
    tasks = _read_csv_safe(_path_for("tasks"))
    members = _read_csv_safe(_path_for("members"))

    if tasks.empty:
        raise HTTPException(404, "No tasks found. Import tasks.csv first.")

    DONE_STATUSES = {"done", "completed", "closed", "resolved"}

    # ensure columns exist (match your CSV schema)
    for col in ["id","status","due_date","impact","story_points","dependencies","assignee_email"]:
        if col not in tasks.columns:
            tasks[col] = ""

    tasks["impact"] = pd.to_numeric(tasks["impact"], errors="coerce").fillna(0.0)
    tasks["story_points"] = pd.to_numeric(tasks["story_points"], errors="coerce").fillna(0.0)

    status_by_id = {str(r["id"]): str(r.get("status","")) for _, r in tasks.iterrows()}
    load_by_owner = _assignee_load(tasks, members)

    feat_rows = []
    ids = []
    done_ids = set()

    for _, r in tasks.iterrows():
        tid = str(r.get("id","")).strip()
        if not tid:
            continue

        st = str(r.get("status", "") or "").lower().strip()
        if st in DONE_STATUSES:
            # include task in response but force 0 risk later
            ids.append(tid)
            feat_rows.append([999.0, 0.0, 0.0, 0.0, 0.0])  # dummy features
            done_ids.add(tid)
            continue

        due = str(r.get("due_date","") or "")
        days_to_due = _days_to_due(due)
        deps_open = float(_dependency_risk(r, status_by_id))
        points = float(r.get("story_points", 0.0) or 0.0)
        impact = float(r.get("impact", 0.0) or 0.0)
        owner = str(r.get("assignee_email","") or "").strip().lower()
        owner_load = float(load_by_owner.get(owner, 0.0))

        ids.append(tid)
        feat_rows.append([days_to_due, deps_open, points, impact, owner_load])

    model = _load_risk_model()

    # --- Heuristic fallback ---
    if model is None:
        risks = {}
        for tid, (dtd, deps, pts, imp, load) in zip(ids, feat_rows):
            score = (
                0.35 * (max(0.0, 10.0 - dtd) / 10.0)
                + 0.2  * (deps / 5.0)
                + 0.2  * min(1.0, pts / 8.0)
                + 0.15 * min(1.0, imp / 10.0)
                + 0.1  * (load / 5.0)
            )
            risks[tid] = float(_clamp(score, 0.0, 1.0))

        # hard override done-like statuses to 0
        for tid in done_ids:
            risks[tid] = 0.0

        return {"ok": True, "model": "heuristic_fallback", "risks": risks}

    # --- Model prediction ---
    import numpy as np
    X = np.array(feat_rows, dtype=float)

    if hasattr(model, "predict_proba"):
        p = model.predict_proba(X)[:, 1]
    else:
        p = model.predict(X)

    risks = {tid: float(_clamp(val, 0.0, 1.0)) for tid, val in zip(ids, p)}

    # hard override done-like statuses to 0 (safety net)
    for tid in done_ids:
        risks[tid] = 0.0

    return {"ok": True, "model": "task_risk_model", "risks": risks}

# ---------- Learning loop: event stats ----------
# EVENTS_PATH = os.path.join(DATA_DIR, "events.csv")

@app.get("/events/stats")
def events_stats():
    if not os.path.exists(EVENTS_PATH):
        return {"ok": True, "total": 0, "by_event": {}, "last_ts": None}

    ev = pd.read_csv(EVENTS_PATH)
    if ev.empty or "event_name" not in ev.columns:
        return {"ok": True, "total": 0, "by_event": {}, "last_ts": None}

    by_event = ev["event_name"].value_counts().to_dict()
    last_ts = None
    try:
        last_ts = ev["ts"].dropna().iloc[-1]
    except Exception:
        pass

    return {"ok": True, "total": int(len(ev)), "by_event": by_event, "last_ts": last_ts}


import subprocess

@app.post("/ai/retrain_task_risk")
def retrain_task_risk():
    """
    Runs train_task_risk.py to regenerate data/task_risk_model.joblib
    Then clears the in-memory model cache so /ai/task_risk uses the new model.
    """
    script = os.path.join(os.path.dirname(__file__), "train_task_risk.py")
    if not os.path.exists(script):
        raise HTTPException(500, "train_task_risk.py not found")

    try:
        # run training in the same env as the server
        out = subprocess.run(
            ["python", script],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"Retrain failed:\n{e.stderr or e.stdout}")

    # clear cached model so it reloads freshly next request
    global _model_cache
    _model_cache = {"model": None}

    return {"ok": True, "stdout": out.stdout[-2000:]}  # keep response small


@app.get("/inventory")
def inventory_view(safety_buffer: int = 2, use_ml: bool = False, horizon: int = 14):
    """
    Inventory snapshot.
    - By default uses SMA to compute avg_daily_use and days_to_stockout.
    - If use_ml=true, uses Holt's linear forecast (_holt_linear) for avg_daily_use.
    """
    inv = _read_inventory()
    res = _read_reservations()
    reserved_by_sku = res.groupby("sku")["qty"].sum(min_count=1).to_dict() if not res.empty else {}
    daily = _group_daily_consumption(res)

    rows = []
    for _, r in inv.iterrows():
        sku = str(r["sku"])
        on_hand = float(r["on_hand"])
        reserved = float(reserved_by_sku.get(sku, 0.0))
        available = on_hand - reserved

        # choose rate
        if use_ml:
            series = list(daily.get(sku, {}).items())
            rate = _holt_linear(series, alpha=0.5, beta=0.3, horizon=horizon) if series else 0.0
            rate_src = "holt_linear"
        else:
            series = list(daily.get(sku, {}).items())
            dts_sma, rate = _sma_days_to_stockout(on_hand, series, window_days=28)
            rate_src = "sma"

        # compute days to stockout with the selected rate
        dts = math.inf if rate <= 0 else (available / rate)
        at_risk = dts < (int(r["lead_time_days"]) + safety_buffer)

        rows.append({
            "sku": sku,
            "name": r["name"],
            "on_hand": on_hand,
            "reserved": reserved,
            "available": available,
            "reorder_point": float(r["reorder_point"]),
            "lead_time_days": int(r["lead_time_days"]),
            "avg_daily_use": round(rate, 2),
            "avg_daily_use_source": rate_src,  # NEW: "sma" or "holt_linear"
            "days_to_stockout": math.inf if math.isinf(dts) else round(dts, 1),
            "at_risk": bool(at_risk)
        })

    return {
        "count": len(rows),
        "items": rows,
        "used_model": "holt_linear" if use_ml else "sma"
    }

@app.get("/inventory/alerts")
def inventory_alerts(safety_buffer: int = 2):
    data = inventory_view(safety_buffer=safety_buffer)
    alerts = [item for item in data["items"]
              if (item["days_to_stockout"] != math.inf) and item["at_risk"]]
    for a in alerts:
        eta = date.today() + pd.Timedelta(days=a["lead_time_days"]).to_pytimedelta()
        a["action_by"] = eta.isoformat()
        a["reason"] = f"Projected stockout in {a['days_to_stockout']}d < lead time ({a['lead_time_days']}d) + buffer({safety_buffer}d)"
    return {"count": len(alerts), "items": alerts}

@app.get("/inventory/list")
def inventory_list():
    # keep the same payload shape as /inventory
    return inventory_view()

class InvUpsert(BaseModel):
    sku: str
    name: str | None = None
    on_hand: float | None = None
    reorder_point: float | None = None
    lead_time_days: int | None = None

@app.post("/inventory/upsert_many")
def inventory_upsert_many(rows: list[InvUpsert]):
    """
    Batch upsert for inventory.csv.
    Expects columns: sku, name, on_hand, reorder_point, lead_time_days
    """
    path = _path_for("inventory")
    df = _read_csv_safe(path)

    if df.empty:
        df = pd.DataFrame(columns=["sku","name","on_hand","reorder_point","lead_time_days"])

    # index for quick updates
    idx = {str(r["sku"]): i for i, r in df.iterrows() if pd.notna(r.get("sku"))}

    def _num(x, cast=float):
        if x is None:
            return None
        v = pd.to_numeric(x, errors="coerce")
        return None if pd.isna(v) else cast(v)

    updated = 0
    for r in rows:
        sku = (r.sku or "").strip()
        if not sku:
            continue

        if sku in idx:
            i = idx[sku]
            if r.name is not None:
                df.at[i, "name"] = r.name
            if (v := _num(r.on_hand)) is not None:
                df.at[i, "on_hand"] = v
            if (v := _num(r.reorder_point)) is not None:
                df.at[i, "reorder_point"] = v
            if (v := _num(r.lead_time_days, int)) is not None:
                df.at[i, "lead_time_days"] = v
        else:
            df = pd.concat([df, pd.DataFrame([{
                "sku": sku,
                "name": r.name or "",
                "on_hand": _num(r.on_hand) or 0.0,
                "reorder_point": _num(r.reorder_point) or 0.0,
                "lead_time_days": _num(r.lead_time_days, int) or 0
            }])], ignore_index=True)
            idx[sku] = len(df) - 1

        updated += 1

    # normalize + persist
    df["on_hand"] = pd.to_numeric(df["on_hand"], errors="coerce").fillna(0.0)
    df["reorder_point"] = pd.to_numeric(df["reorder_point"], errors="coerce").fillna(0.0)
    df["lead_time_days"] = pd.to_numeric(df["lead_time_days"], errors="coerce").fillna(0).astype(int)

    df.to_csv(path, index=False)
    return {"updated": updated, "count": int(len(df))}

# ---------- sprint planner ----------
class PlanParams(BaseModel):
    capacity_multiplier: float = 1.0
    include_statuses: list[str] = ["todo", "in progress", "blocked", "ready"]
    max_days: int = 7
    planner: str = "greedy"   # NEW: "greedy" or "knapsack"

def _load_members_capacity(members_df: pd.DataFrame) -> dict[str, float]:
    if members_df.empty: return {}
    caps = {}
    for _, r in members_df.iterrows():
        email = str(r.get("email", "")).strip().lower()
        cap = float(pd.to_numeric(r.get("weekly_capacity", 0), errors="coerce") or 0.0)
        if email:
            caps[email] = max(cap, 0.0)
    return caps

def _eligible_tasks(tasks_df: pd.DataFrame, include_statuses: list[str]) -> pd.DataFrame:
    st = tasks_df["status"].astype(str).str.lower().str.strip()
    mask = st.isin([s.lower() for s in include_statuses])
    return tasks_df[mask].copy()

def _normalize_points(x) -> float:
    v = float(pd.to_numeric(x, errors="coerce") or 0.0)
    return max(v, 0.0)

def knapsack_select(candidates: list[dict], capacity_int: int) -> set[str]:
    """
    candidates: [{'id': str, 'score': float, 'story_points': float}]
    capacity_int: int (capacity in points)
    returns: set of selected task ids maximizing total score
    """
    items = [(c["id"], int(max(0, round(c.get("story_points", 0) or 0))), float(c.get("score", 0))) for c in candidates]
    items = [x for x in items if x[1] > 0]  # weight > 0
    n = len(items); C = max(0, int(capacity_int))
    dp = [[0.0]*(C+1) for _ in range(n+1)]
    keep = [[False]*(C+1) for _ in range(n+1)]
    for i in range(1, n+1):
        tid, w, v = items[i-1]
        for c in range(C+1):
            best = dp[i-1][c]
            take = dp[i-1][c-w] + v if w <= c else -1
            if take > best:
                dp[i][c] = take; keep[i][c] = True
            else:
                dp[i][c] = best
    sel = set(); c = C
    for i in range(n, 0, -1):
        if keep[i][c]:
            tid, w, v = items[i-1]; sel.add(tid); c -= w
    return sel

@app.post("/tasks/plan_sprint")
def plan_sprint(params: PlanParams = Body(default=PlanParams())):
    tasks_df = _read_csv_safe(_path_for("tasks"))
    members_df = _read_csv_safe(_path_for("members"))
    if tasks_df.empty:
        raise HTTPException(404, "No tasks found. Import tasks.csv first.")
    if members_df.empty:
        raise HTTPException(404, "No members found. Import members.csv first.")

    # prioritized list
    scored = prioritized()["items"]  # list of dicts (already scored)

    # indexes
    status_by_id = {str(r["id"]): str(r["status"]) for _, r in tasks_df.iterrows()}

    def _parse_deps(val):
        if pd.isna(val): return []
        raw = str(val).strip()
        if not raw: return []
        return [d.strip() for d in raw.split(",") if d.strip()]

    deps_by_id = {str(r["id"]): _parse_deps(r.get("dependencies")) for _, r in tasks_df.iterrows()}
    points_by_id = {str(r["id"]): _normalize_points(r.get("story_points")) for _, r in tasks_df.iterrows()}
    assignee_by_id = {str(r["id"]): str(r.get("assignee_email") or "").lower().strip() for _, r in tasks_df.iterrows()}

    # eligible & capacity
    eligible_set = set(_eligible_tasks(tasks_df, params.include_statuses)["id"].astype(str))
    caps = _load_members_capacity(members_df)
    caps = {k: v * float(params.capacity_multiplier) for k, v in caps.items()}
    used = {k: 0.0 for k in caps.keys()}
    plan: dict[str, list[dict]] = {k: [] for k in caps.keys()}
    backlog: list[dict] = []

    # --- deps_ok helper ---
    def deps_ok(task_id: str) -> bool:
        raw_list = deps_by_id.get(task_id, [])
        deps = [d for d in raw_list if d != task_id and d in status_by_id]
        if not deps:
            return True
        for d in deps:
            st = (status_by_id.get(d, "") or "").lower()
            if st not in ["done", "completed", "closed", "resolved"]:
                # allow if dep already scheduled in this sprint
                scheduled = any(any(x["id"] == d for x in owner_tasks) for owner_tasks in plan.values())
                if not scheduled:
                    return False
        return True

    planner_mode = (params.planner or "greedy").lower()

    if planner_mode == "knapsack":
        score_by_id = {str(x["id"]): float(x.get("score", 0.0)) for x in scored}
        already_planned = set()
        changed = True

        # Iterate owners until no new tasks can be added (lets deps become satisfied as we go)
        while changed:
            changed = False
            for owner in caps.keys():
                rem_cap = int(round(max(caps[owner] - used[owner], 0.0)))
                if rem_cap <= 0:
                    continue

                # Owner’s eligible, unscheduled, deps-ok candidates
                cands = []
                for item in scored:
                    tid = str(item["id"])
                    if tid in already_planned or tid not in eligible_set:
                        continue
                    if assignee_by_id.get(tid) != owner:
                        continue
                    if not deps_ok(tid):
                        continue
                    pts = points_by_id.get(tid, 0.0)
                    cands.append({"id": tid, "story_points": pts, "score": score_by_id.get(tid, 0.0)})

                if not cands or rem_cap <= 0:
                    continue

                selected = knapsack_select(cands, rem_cap)
                if not selected:
                    continue

                # Add selected in priority order for nicer presentation
                for item in scored:
                    tid = str(item["id"])
                    if tid in selected:
                        plan[owner].append(item | {"story_points": points_by_id.get(tid, 0.0)})
                        used[owner] += points_by_id.get(tid, 0.0)
                        already_planned.add(tid)
                        changed = True

        # Anything left over becomes backlog with reason
        for item in scored:
            tid = str(item["id"])
            if tid in already_planned or tid not in eligible_set:
                continue
            owner = assignee_by_id.get(tid, "") or "__unassigned__"
            pts = points_by_id.get(tid, 0.0)
            if assignee_by_id.get(tid, "") == "" or owner not in caps:
                backlog.append({**item, "story_points": pts, "intended_owner": owner, "reason_unassigned": "No assignee or not in members"})
            elif not deps_ok(tid):
                backlog.append({**item, "story_points": pts, "intended_owner": owner, "reason_unassigned": "Dependencies not satisfied"})
            else:
                backlog.append({**item, "story_points": pts, "intended_owner": owner,
                                "reason_unassigned": f"Capacity exceeded for {owner} ({used.get(owner,0.0):.1f}/{caps.get(owner,0.0):.1f})"})
    else:
        # GREEDY loop (single pass)
        for item in scored:
            tid = str(item["id"])
            if tid not in eligible_set:
                continue
            owner = assignee_by_id.get(tid, "")
            pts = points_by_id.get(tid, 0.0)

            if not owner or owner not in caps:
                backlog.append({**item, "story_points": pts, "intended_owner": owner or "__unassigned__", "reason_unassigned": "No assignee or not in members"})
                continue

            if not deps_ok(tid):
                backlog.append({**item, "story_points": pts, "intended_owner": owner, "reason_unassigned": "Dependencies not satisfied"})
                continue

            if used.get(owner, 0.0) + pts <= caps.get(owner, 0.0):
                plan[owner].append(item | {"story_points": pts})
                used[owner] = used.get(owner, 0.0) + pts
            else:
                backlog.append({
                    **item,
                    "story_points": pts,
                    "intended_owner": owner or "__unassigned__",
                    "reason_unassigned": f"Capacity exceeded for {owner} ({used.get(owner,0.0):.1f}/{caps.get(owner,0.0):.1f})"
                })

    # vectors & summary
    plan_tasks_vector = {owner: [t["id"] for t in tasks] for owner, tasks in plan.items()}
    plan_points_vector = {owner: [t.get("story_points", 0.0) for t in tasks] for owner, tasks in plan.items()}

    backlog_by_member = {}
    backlog_points_by_member = {}
    for b in backlog:
        owner_key = b.get("intended_owner") or "__unassigned__"
        backlog_by_member.setdefault(owner_key, []).append(b["id"])
        backlog_points_by_member[owner_key] = backlog_points_by_member.get(owner_key, 0.0) + float(b.get("story_points", 0.0))

    summary = []
    for owner in plan.keys():
        summary.append({
            "assignee_email": owner,
            "capacity": round(caps.get(owner, 0.0), 1),
            "planned_points": round(used.get(owner, 0.0), 1),
            "remaining": round(max(caps.get(owner, 0.0) - used.get(owner, 0.0), 0.0), 1),
            "num_tasks": len(plan[owner]),
        })
    summary.sort(key=lambda x: x["remaining"], reverse=True)

    return {
        "params": params.model_dump(),
        "summary": summary,
        "plan": plan,
        "plan_tasks_vector": plan_tasks_vector,
        "plan_points_vector": plan_points_vector,
        "backlog": backlog,
        "backlog_by_member": backlog_by_member,
        "backlog_points_by_member": {k: round(v, 1) for k, v in backlog_points_by_member.items()}
    }

@app.get("/debug/deps")
def debug_deps():
    tasks_df = _read_csv_safe(_path_for("tasks"))
    if tasks_df.empty:
        raise HTTPException(404, "No tasks found")

    # what the planner/scorer would see
    status_by_id = {str(r["id"]): str(r["status"]) for _, r in tasks_df.iterrows()}

    def _parse_deps(val):
        if pd.isna(val): return []
        raw = str(val).strip()
        if not raw: return []
        return [d.strip() for d in raw.split(",") if d.strip()]

    out = []
    for _, r in tasks_df.iterrows():
        tid = str(r["id"])
        raw = r.get("dependencies")
        parsed = _parse_deps(raw)
        # compute deps_open exactly like prioritized()
        deps_open = _dependency_risk(r, status_by_id)
        out.append({
            "id": tid,
            "title": r.get("title"),
            "raw_dependencies_cell": None if pd.isna(raw) else str(raw),
            "parsed_dependencies": parsed,
            "deps_open": int(deps_open)
        })
    return {"items": out}

@app.post("/team/insights")
def team_insights(params: PlanParams = Body(default=PlanParams())):
    # Use the same planner to get a plan/backlog snapshot
    plan_resp = plan_sprint(params)
    summary = plan_resp["summary"]
    plan = plan_resp["plan"]
    backlog = plan_resp["backlog"]

    # Per-member metrics
    per_member = []
    cap_total = 0.0
    planned_total = 0.0
    for row in summary:
        cap = float(row["capacity"])
        planned = float(row["planned_points"])
        cap_total += cap
        planned_total += planned
        util = 0.0 if cap <= 0 else round((planned / cap) * 100.0, 1)
        per_member.append({
            "assignee_email": row["assignee_email"],
            "capacity": cap,
            "planned_points": planned,
            "remaining": row["remaining"],
            "utilization_pct": util,
            "num_tasks": row["num_tasks"]
        })

    team_util = 0.0 if cap_total <= 0 else round((planned_total / cap_total) * 100.0, 1)

    # Backlog pressure per intended owner
    backlog_by_member = {}
    for b in backlog:
        owner = b.get("intended_owner") or "__unassigned__"
        backlog_by_member.setdefault(owner, {"count": 0, "points": 0.0})
        backlog_by_member[owner]["count"] += 1
        backlog_by_member[owner]["points"] += float(b.get("story_points", 0.0))

    # Simple dependency bottlenecks: which tasks are blocking others?
    tasks_df = _read_csv_safe(_path_for("tasks"))
    if tasks_df.empty:
        raise HTTPException(404, "No tasks found. Import tasks.csv first.")
    owner_by_id = {str(r["id"]): str(r.get("assignee_email") or "").lower().strip() for _, r in tasks_df.iterrows()}

    def _parse_deps(val):
        if pd.isna(val): return []
        raw = str(val).strip()
        if not raw: return []
        return [d.strip() for d in raw.split(",") if d.strip()]

    deps_by_id = {str(r["id"]): _parse_deps(r.get("dependencies")) for _, r in tasks_df.iterrows()}
    # count blockers for tasks not in Done/Closed
    status_by_id = {str(r["id"]): str(r["status"]) for _, r in tasks_df.iterrows()}
    open_status = lambda s: (s or "").lower() not in ["done","completed","closed","resolved"]

    blocked_by = {}  # blocker_id -> list of blocked task ids
    for tid, deps in deps_by_id.items():
        if open_status(status_by_id.get(tid)):
            for d in deps:
                # consider only blockers that are also not done
                if d in status_by_id and open_status(status_by_id.get(d)):
                    blocked_by.setdefault(d, []).append(tid)

    # Convert to owner-level view
    bottlenecks = []
    for blocker_id, blocked_list in blocked_by.items():
        owner = owner_by_id.get(blocker_id, "__unassigned__")
        bottlenecks.append({
            "blocker_task": blocker_id,
            "blocker_owner": owner,
            "blocks_count": len(blocked_list),
            "blocks": blocked_list
        })

    # Sort outputs
    per_member.sort(key=lambda x: x["utilization_pct"], reverse=True)
    bottlenecks.sort(key=lambda x: x["blocks_count"], reverse=True)

    return {
        "params": params.model_dump(),
        "team": {
            "capacity_total": round(cap_total, 1),
            "planned_total": round(planned_total, 1),
            "utilization_team_pct": team_util
        },
        "per_member": per_member,
        "backlog_pressure": backlog_by_member,
        "bottlenecks": bottlenecks,
        "plan_echo": {k: [t["id"] for t in v] for k, v in plan.items()}  # quick glance vector
    }

# ---------- Team Velocity Forecast (Holt) ----------
# Place this block right below /team/insights

def _parse_date_safe_str(s):
    """Accept YYYY-MM-DD (preferred) and DD-MM-YYYY as a fallback."""
    if pd.isna(s) or s == "": 
        return None
    txt = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(txt, fmt).date()
        except Exception:
            continue
    return None

def _weekly_points(df: pd.DataFrame) -> pd.DataFrame:
    """
    Input: tasks with columns: assignee_email, story_points, status, completed_date (optional)
    Output: rows of (assignee_email, week_start_date, points_done)
    """
    if df.empty:
        return pd.DataFrame(columns=["assignee_email","week_start","points"])
    dd = df.copy()
    dd["status_lc"] = dd["status"].astype(str).str.lower().str.strip()
    dd = dd[dd["status_lc"].isin(["done","completed","closed","resolved"])].copy()
    if dd.empty or "completed_date" not in dd.columns:
        return pd.DataFrame(columns=["assignee_email","week_start","points"])
    dd["completed_date"] = dd["completed_date"].apply(_parse_date_safe_str)
    dd = dd[dd["completed_date"].notna()].copy()
    if dd.empty:
        return pd.DataFrame(columns=["assignee_email","week_start","points"])

    dd["assignee_email"] = dd["assignee_email"].astype(str).str.strip().str.lower()
    dd["story_points"] = pd.to_numeric(dd["story_points"], errors="coerce").fillna(0.0)
    # Normalize to Monday week-start
    dd["week_start"] = dd["completed_date"].apply(lambda d: d - pd.Timedelta(days=d.weekday()).to_pytimedelta())
    agg = dd.groupby(["assignee_email","week_start"])["story_points"].sum(min_count=1).reset_index()
    agg.rename(columns={"story_points":"points"}, inplace=True)
    return agg

def _holt_linear_points(series: list[tuple[date, float]], alpha=0.5, beta=0.3, horizon_weeks=1) -> float:
    """Holt smoothing for weekly completed points; returns next-week forecast (or avg over horizon)."""
    if not series:
        return 0.0
    series = sorted(series, key=lambda x: x[0])
    start, end = series[0][0], series[-1][0]
    m = {d: float(v) for d, v in series}
    filled = []
    weeks = ((end - start).days // 7) + 1
    for i in range(weeks):
        di = start + pd.Timedelta(weeks=i).to_pytimedelta()
        filled.append((di, m.get(di, 0.0)))

    l = filled[0][1]
    b = (filled[1][1] - filled[0][1]) if len(filled) > 1 else 0.0
    for t in range(1, len(filled)):
        y = filled[t][1]
        l_next = alpha * y + (1 - alpha) * (l + b)
        b_next = beta * (l_next - l) + (1 - beta) * b
        l, b = l_next, b_next

    preds = [max(0.0, l + k * b) for k in range(1, horizon_weeks + 1)]
    return float(sum(preds) / max(1, len(preds)))

@app.get("/team/velocity_forecast")
def team_velocity_forecast(alpha: float = 0.5, beta: float = 0.3, horizon_weeks: int = 1):
    tasks_df = _read_csv_safe(_path_for("tasks"))
    members_df = _read_csv_safe(_path_for("members"))
    if tasks_df.empty or members_df.empty:
        return {"ok": False, "reason": "Need tasks.csv and members.csv"}

    weekly = _weekly_points(tasks_df)
    if weekly.empty:
        return {"ok": False, "reason": "No completed tasks with completed_date; add completed_date in YYYY-MM-DD for Done items"}

    # member capacities
    caps = {}
    for _, r in members_df.iterrows():
        email = str(r.get("email","")).strip().lower()
        cap = float(pd.to_numeric(r.get("weekly_capacity", 0), errors="coerce") or 0)
        if email:
            caps[email] = max(cap, 0.0)

    out = []
    for owner, g in weekly.groupby("assignee_email"):
        ser = [(d, float(p)) for d, p in zip(g["week_start"], g["points"])]
        forecast = _holt_linear_points(ser, alpha=alpha, beta=beta, horizon_weeks=horizon_weeks)
        cap = caps.get(owner, 0.0)
        suggested_mul = 1.0
        if cap > 0:
            suggested_mul = max(0.6, min(1.25, forecast / cap))
        out.append({
            "assignee_email": owner,
            "weekly_capacity": cap,
            "forecast_points_next_week": round(forecast, 1),
            "suggested_capacity_multiplier": round(suggested_mul, 2)
        })

    team_cap = sum(caps.values())
    team_forecast = sum(x["forecast_points_next_week"] for x in out)
    team_mul = round(max(0.6, min(1.25, (team_forecast / team_cap) if team_cap > 0 else 1.0)), 2)

    return {
        "ok": True,
        "per_member": out,
        "team": {
            "capacity_total": round(team_cap, 1),
            "forecast_points_next_week": round(team_forecast, 1),
            "suggested_capacity_multiplier": team_mul
        }
    }

# ---------- tasks upsert / dedupe / delete ----------
class TaskUpsert(BaseModel):
    id: str
    title: Optional[str] = None
    project: Optional[str] = None
    assignee_email: Optional[str] = None
    status: Optional[str] = None
    due_date: Optional[str] = None        # "YYYY-MM-DD"
    story_points: Optional[float] = None
    impact: Optional[float] = None
    dependencies: Optional[str] = None    # comma-separated ids

@app.post("/tasks/upsert_many")
def tasks_upsert_many(rows: List[TaskUpsert]):
    if not rows:
        raise HTTPException(400, "No rows")
    path = _path_for("tasks")
    df = _read_csv_safe(path)

    # If file missing, create with correct columns
    if df.empty:
        df = pd.DataFrame(columns=[
            "id","title","project","assignee_email","status",
            "due_date","story_points","impact","dependencies"
        ])

    id_index = {str(r["id"]): i for i, r in df.iterrows()}

    def _norm(v):  # helper to strip strings
        if v is None: return None
        if isinstance(v, str): return v.strip()
        return v

    for r in rows:
        rid = _norm(r.id)
        if not rid or rid.lower() == "string":
            continue
        if rid in id_index:
            i = id_index[rid]
            for field in ["title","project","assignee_email","status","due_date","story_points","impact","dependencies"]:
                val = getattr(r, field)
                if val is not None:
                    df.at[i, field] = _norm(val)
        else:
            df = pd.concat([df, pd.DataFrame([{
                "id": rid,
                "title": _norm(r.title) or "",
                "project": _norm(r.project) or "",
                "assignee_email": _norm(r.assignee_email) or "",
                "status": _norm(r.status) or "Todo",
                "due_date": _norm(r.due_date) or "",
                "story_points": r.story_points if r.story_points is not None else 0,
                "impact": r.impact if r.impact is not None else 3,
                "dependencies": _norm(r.dependencies) or "",
            }])], ignore_index=True)
            id_index[rid] = len(df) - 1

    # Normalize numeric columns
    for col in ["story_points","impact"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Save
    df.to_csv(path, index=False)
    return {"updated": len(rows), "rows": [r.id for r in rows]}

@app.post("/tasks/delete_many")
def tasks_delete_many(ids: List[str]):
    if not ids:
        raise HTTPException(400, "No ids provided")
    path = _path_for("tasks")
    df = _read_csv_safe(path)
    if df.empty:
        return {"deleted": 0, "ids": []}

    before = len(df)
    df = df[~df["id"].astype(str).isin([str(x) for x in ids])]
    df.to_csv(path, index=False)
    return {"deleted": before - len(df), "ids": ids}

@app.post("/tasks/dedupe")
def tasks_dedupe(keep: str = "last"):
    """
    Remove duplicate task rows by id. keep: "first" or "last" (default)
    """
    path = _path_for("tasks")
    df = _read_csv_safe(path)
    if df.empty:
        return {"removed": 0, "kept": keep}

    before = len(df)
    if "id" in df.columns:
        df = df.drop_duplicates(subset=["id"], keep="last" if keep not in ("first","last") else keep)
    df.to_csv(path, index=False)
    return {"removed": before - len(df), "kept": keep, "rows": len(df)}

def _dedupe_by_id(df: pd.DataFrame, keep: str = "last") -> pd.DataFrame:
    """Drop duplicate rows by 'id'.
    keep: 'first' or 'last' (default 'last'). If another value provided, default to 'last'.
    """
    if df.empty or "id" not in df.columns:
        return df
    k = "last" if keep not in ("first", "last") else keep
    return df.drop_duplicates(subset=["id"], keep=k)

# ---------- Data Validator ----------
@app.get("/data/validate")
def data_validate():
    problems = {
        "missing_columns": [],
        "duplicate_task_ids": [],
        "bad_due_dates": [],
        "unknown_assignees": [],
        "unknown_dependencies": [],
        "negative_or_non_numeric_points": [],
    }

    # load
    tasks = _read_csv_safe(_path_for("tasks"))
    members = _read_csv_safe(_path_for("members"))

    # required columns
    required = ["id","title","project","assignee_email","status",
                "due_date","story_points","impact","dependencies"]
    missing = [c for c in required if c not in tasks.columns]
    if missing:
        problems["missing_columns"] = missing
        # early exit keeps response small
        return {"ok": False, "problems": problems}

    # normalize
    tasks["id"] = tasks["id"].astype(str).str.strip()
    tasks["assignee_email"] = tasks["assignee_email"].astype(str).str.strip().str.lower()

    # duplicates
    dups = tasks["id"][tasks["id"].duplicated(keep=False)].unique().tolist()
    if dups:
        problems["duplicate_task_ids"] = dups

    # bad dates (expect YYYY-MM-DD)
    for _, r in tasks.iterrows():
        s = str(r.get("due_date", "")).strip()
        if s == "":
            continue
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            problems["bad_due_dates"].append({"id": r["id"], "due_date": s})

    # assignees not in members
    member_set = set()
    if not members.empty and "email" in members.columns:
        member_set = set(members["email"].astype(str).str.strip().str.lower().tolist())
    for _, r in tasks.iterrows():
        em = r.get("assignee_email", "")
        if em and member_set and em not in member_set:
            problems["unknown_assignees"].append({"id": r["id"], "assignee_email": em})

    # dependencies that point to unknown IDs
    known_ids = set(tasks["id"].astype(str))
    def _parse_deps(val):
        if pd.isna(val): return []
        raw = str(val).strip()
        if not raw: return []
        return [d.strip() for d in raw.split(",") if d.strip()]
    for _, r in tasks.iterrows():
        tid = r["id"]
        for d in _parse_deps(r.get("dependencies")):
            if d not in known_ids:
                problems["unknown_dependencies"].append({"id": tid, "missing_dep": d})

    # points/impact sanity
    for _, r in tasks.iterrows():
        sp = pd.to_numeric(r.get("story_points"), errors="coerce")
        im = pd.to_numeric(r.get("impact"), errors="coerce")
        if pd.isna(sp) or sp < 0 or pd.isna(im) or im < 0:
            problems["negative_or_non_numeric_points"].append({
                "id": r["id"], "story_points": r.get("story_points"), "impact": r.get("impact")
            })

    ok = all(len(v) == 0 for v in problems.values())
    return {"ok": ok, "problems": problems}

@app.get("/ai/anomalies")
def anomalies():
    tasks = _read_csv_safe(_path_for("tasks"))
    inv = _read_csv_safe(_path_for("inventory"))

    out = {"tasks": [], "inventory": []}

    if not tasks.empty:
        for col in ["id","status","due_date","impact","story_points","assignee_email"]:
            if col not in tasks.columns:
                tasks[col] = ""
        tasks["impact"] = pd.to_numeric(tasks["impact"], errors="coerce").fillna(0.0)
        tasks["story_points"] = pd.to_numeric(tasks["story_points"], errors="coerce").fillna(0.0)

        for _, r in tasks.iterrows():
            tid = str(r.get("id",""))
            due = _parse_date_safe(r.get("due_date",""))
            st = str(r.get("status","")).lower().strip()
            imp = float(r.get("impact",0))
            pts = float(r.get("story_points",0))

            if due and due < date.today() and st not in ["done","completed","closed","resolved"]:
                out["tasks"].append({
                    "id": tid,
                    "type": "overdue",
                    "reason": "Due date passed but not done"
                })

            if imp >= 8 and pts == 0:
                out["tasks"].append({
                    "id": tid,
                    "type": "suspicious_estimate",
                    "reason": "High impact with 0 story points"
                })

    if not inv.empty:
        for col in ["sku","name","on_hand","reorder_point"]:
            if col not in inv.columns:
                inv[col] = ""
        inv["on_hand"] = pd.to_numeric(inv["on_hand"], errors="coerce").fillna(0.0)
        inv["reorder_point"] = pd.to_numeric(inv["reorder_point"], errors="coerce").fillna(0.0)

        for _, r in inv.iterrows():
            sku = str(r.get("sku",""))
            if float(r["on_hand"]) < 0:
                out["inventory"].append({
                    "sku": sku,
                    "type": "negative_stock",
                    "reason": "On-hand is negative"
                })
            if float(r["on_hand"]) == 0 and float(r["reorder_point"]) > 0:
                out["inventory"].append({
                    "sku": sku,
                    "type": "stockout",
                    "reason": "On-hand is 0 and reorder_point > 0"
                })

    return {"ok": True, "anomalies": out}

@app.get("/ai/followups")
def ai_followups():
    tasks = _read_csv_safe(_path_for("tasks"))
    risks = {}

    # load risk model output if available
    try:
        r = task_risk()
        risks = r.get("risks", {})
    except:
        risks = {}

    followups = []

    if not tasks.empty:
        for _, r in tasks.iterrows():
            tid = str(r.get("id",""))
            st = str(r.get("status","")).lower()
            due = _parse_date_safe(r.get("due_date",""))
            risk = float(risks.get(tid, 0))

            if st not in ["done","completed","closed"]:
                if (due and due < date.today()) or risk >= 0.7:
                    followups.append({
                        "id": tid,
                        "reason": "Overdue" if (due and due < date.today()) else "High delay risk",
                        "risk": round(risk*100)
                    })

    return {"ok": True, "followups": followups}

@app.get("/ai/engagement")
def ai_engagement():
    tasks = _read_csv_safe(_path_for("tasks"))
    risks = {}

    try:
        r = task_risk()
        risks = r.get("risks", {})
    except:
        risks = {}

    load = {}

    if not tasks.empty:
        for _, r in tasks.iterrows():
            user = str(r.get("assignee_email","")).strip()
            if not user:
                continue

            st = str(r.get("status","")).lower()
            if st in ["done","completed"]:
                continue

            tid = str(r.get("id",""))
            risk = float(risks.get(tid, 0))

            load.setdefault(user, {"tasks":0,"high_risk":0})
            load[user]["tasks"] += 1
            if risk >= 0.7:
                load[user]["high_risk"] += 1

    bottlenecks = [
        {"assignee": u, **v}
        for u,v in load.items()
        if v["high_risk"] >= 2 or v["tasks"] >= 5
    ]

    return {"ok": True, "bottlenecks": bottlenecks}


# uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
