# TaskFlow Pro

**AI‑Powered Task & Inventory Intelligence**

A lightweight, dockerized platform that blends AI‑assisted task prioritization, sprint planning, team insights, and inventory alerts — all on top of simple CSV/JSON files (no database).

---

## Features

* **Prioritized Tasks** — Weighted scoring by urgency, impact, dependency pressure, and owner load.
* **Sprint Planner** — Greedy (fast) and Knapsack (optimal per owner) planners respecting capacity & dependencies.
* **Team Insights** — Capacity, planned points, utilization %, blockers, and backlog heat.
* **Inventory Intelligence** — SMA baseline usage, optional Holt linear forecast, and at‑risk alerts.
* **CSV/JSON I/O** — Import/Export for tasks, members, inventory, reservations, and projects.
* **Self‑hosted Docs** — Swagger UI proxied at `/api/docs`.

> **Status:** MVP ✅ — core planning loop works. See **Roadmap (Remaining)** for two small items still open.

---

## Architecture

* **Frontend:** Single‑page app (`frontend/index.html`) — vanilla HTML/JS, fetch API.
* **Backend:** Python FastAPI (`backend/app.py`), Uvicorn.
* **Storage:** Flat files under `./data` (mounted into the API container).
* **Reverse Proxy:** NGINX serves the SPA and proxies `/api/*` → FastAPI.
* **Docker:** Two images (`api`, `web`) orchestrated by Compose.

```
Browser  ──►  NGINX (:8080)
              ├─ serves / (index.html)
              └─ proxies /api/* → FastAPI (:8000)
```

---

## Repository Layout

```
taskflow-pro/
├─ backend/
│  └─ app.py
├─ data/                       # live CSV/JSON used by the app
│  ├─ tasks.csv
│  ├─ members.csv
│  ├─ inventory.csv
│  ├─ reservations.csv
│  └─ projects.json
├─ data/data-samples/          # sample inputs to import
│  ├─ tasks.csv
│  ├─ members.csv
│  ├─ inventory.csv
│  ├─ reservations.csv
│  └─ projects.json
├─ frontend/
│  └─ index.html
├─ nginx.conf
├─ Dockerfile.api
├─ Dockerfile.web
└─ docker-compose.yml
```

---

## Setup & Run (Docker)

1. **From the project root**

```powershell
# Stop old containers, rebuild, and start fresh
docker compose down
docker compose up -d --build

# Verify
docker compose ps
```

2. **Open the app**

* Web UI: [http://localhost:8080](http://localhost:8080)
* API Health (proxied): [http://localhost:8080/api/health](http://localhost:8080/api/health)
* Swagger Docs (proxied): [http://localhost:8080/api/docs](http://localhost:8080/api/docs)

> If you open `/api/docs` directly via the proxy, FastAPI must be started with `--root-path /api` (see Compose below).

---

## docker-compose.yml (key bits)

```yaml\ nservices:
  api:
    build:
      context: .
      dockerfile: Dockerfile.api
    container_name: taskflow-api
    ports: ["8000:8000"]
    volumes:
      - ./data:/app/data
    command: uvicorn backend.app:app --host 0.0.0.0 --port 8000 --root-path /api
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 3s
      retries: 10

  web:
    build:
      context: .
      dockerfile: Dockerfile.web
    container_name: taskflow-web
    depends_on:
      api:
        condition: service_healthy
    ports: ["8080:80"]
```

> The `--root-path /api` ensures Swagger at `/api/docs` loads its spec from `/api/openapi.json` correctly.

---

## NGINX Proxy (nginx.conf)

```nginx
# Serve the SPA
location / {
  try_files $uri /index.html;
}

# Proxy API
location /api/ {
  proxy_pass http://api:8000/;           # 'api' is the Compose service name
  proxy_set_header Host $host;
  proxy_set_header X-Real-IP $remote_addr;
  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto $scheme;
}
```

---

## Frontend API base

In `frontend/index.html`:

```html
<script>
  const API = new URLSearchParams(location.search).get("api") || "/api";
</script>
```

All browser requests go to `/api/*` and are proxied to FastAPI — no CORS issues.

---

## Data Schemas

**tasks.csv**

```
id,title,project,assignee_email,status,due_date,story_points,impact,dependencies
T-1000,Design checkout flow,PORTAL,aarav@grapepay.com,In Progress,2025-10-15,3,4,
T-1001,Integrate UPI QR,PORTAL,aarav@grapepay.com,Todo,2025-10-20,5,5,T-1000
```

* `due_date`: `YYYY-MM-DD`
* `dependencies`: comma‑separated task IDs
* Planner includes statuses: `Todo`, `In Progress`, `Ready`, `Blocked` (case‑insensitive)

**members.csv**

```
email,full_name,role,weekly_capacity
aarav@grapepay.com,Aarav Sharma,Backend,20
isha@grapepay.com,Isha Kapoor,Ops,15
```

**inventory.csv**

```
sku,name,on_hand,reorder_point,lead_time_days
POS-TERMINAL,POS Terminal Devices,50,30,10
```

**reservations.csv**

```
reservation_id,task_id,sku,qty,planned_date,actual_date
R-001,T-1000,POS-TERMINAL,5,2025-10-10,2025-10-10
```

**projects.json**

```json
[
  {"key":"PORTAL","name":"Merchant Portal"},
  {"key":"OPS","name":"Operations"},
  {"key":"QA","name":"Quality Assurance"}
]
```

---

## API Endpoints (through the proxy)

* **Health**: `GET /api/health`
* **Docs**: `GET /api/docs` (OpenAPI at `/api/openapi.json`)

### Tasks

* **List (prioritized)**: `GET /api/tasks/prioritized`
* **Upsert in bulk**: `POST /api/tasks/upsert_many` (JSON array of rows)
* **Plan sprint**: `POST /api/tasks/plan_sprint`

  * Body example:

    ```json
    {"capacity_multiplier":1.0, "include_statuses":["todo","in progress","ready"], "planner":"greedy"}
    ```

### Data Import/Export

* **Import (overwrite)**: `POST /api/import/{dtype}` with **multipart/form‑data** `file` field

  * `dtype ∈ { tasks, members, inventory, reservations, projects }`
* **Export**: `GET /api/export/{dtype}` → CSV/JSON content
* **Sanity check**: `GET /api/data/validate`
* **Files info**: `GET /api/files`

### Inventory

* **Snapshot**: `GET /api/inventory`
* **Alerts**: `GET /api/inventory/alerts`

---

## Scoring & Planning (how it works)

* **Score** ≈ 0.35·Urgency + 0.25·Impact + 0.20·DepsOpen + 0.15·OwnerLoad (+ Heuristic 0.05)
* **Greedy planner**: iterate tasks by score; take if capacity allows; respects dependencies.
* **Knapsack planner**: per owner, maximize total score under capacity; ignores zero‑point tasks.

> Tip: Give every in‑scope task `story_points > 0`; the knapsack ignores 0‑point items.

---

## Demo Script (5–7 minutes)

1. Open **/api/docs** (via proxy), import **members**, **tasks**, **inventory**, **reservations** from `data/data-samples/`.
2. Show **/api/files** and **/api/export/tasks** to confirm live data.
3. Open the UI (/:8080), **Refresh Prioritized** and explain the score.
4. **Plan Sprint** (Greedy), then switch to **Knapsack**.
5. Change a task’s story points in the UI → **Save edits** → Re‑plan.
6. Show **Inventory Alerts** and how imports change it.
7. Run **/api/data/validate** → `ok: true`.

---

## Troubleshooting

* **/api/docs shows a parser error** → ensure API runs with `--root-path /api`.
* **Members save 404** → button must be `type="button"` or call `e.preventDefault()`; post **FormData** to `/api/import/members` (NOT JSON).
* **Edits don’t stick** → confirm `POST /api/tasks/upsert_many` returns 200 and `story_points` is numeric; hard‑refresh.
* **Proxy 404** → check `nginx.conf` has `proxy_pass http://api:8000/;` and Compose service is named `api`.

---

## Roadmap (Remaining)

1. **Members Save UX (tiny fix)**
   Ensure Save button is `type="button"` (or `e.preventDefault()`), and the handler posts `multipart/form-data` to **`/api/import/members`** so there’s no phantom GET 404 after a successful POST.

2. **UI Polish**

   * Success/error toasts on save actions.
   * Render `Dependencies` as clickable tokens; flag unknown IDs inline.
   * Warn when `story_points = 0` (knapsack ignores zero‑weight tasks).

---

## License

Private / demo use for evaluation.

---

## Credits

Built with FastAPI, pandas, Uvicorn, and NGINX; deployed via Docker Compose.
