# TaskFlow Pro

AI-assisted task planning, team insights, inventory risk alerts, and learning-loop risk prediction using FastAPI + vanilla frontend + file-based storage (CSV/JSON).

## What Is Implemented

- Prioritized task scoring based on urgency, impact, dependency pressure, and owner load
- Sprint planning with `greedy` and `knapsack` planners
- Inline task and inventory editing from the UI
- Team insights and velocity forecasting
- Inventory stockout risk with SMA and Holt linear usage models
- Event logging, task delay-risk prediction, and model retraining
- Data quality validation + AI anomaly/follow-up suggestions
- Lightweight RBAC + API key auth (toggleable)
- Encrypted per-project collaboration notes

## Stack

- Frontend: `frontend/index.html` + `frontend/global.css` (vanilla JS)
- API: FastAPI (`backend/app.py`)
- Training script: `backend/train_task_risk.py`
- Storage: flat files in `data/`
- Web/proxy: NGINX (`nginx.conf`)
- Containers: Docker Compose (`docker-compose.yml`)

## Project Structure

```text
taskflow-pro/
+- backend/
¦  +- app.py
¦  +- train_task_risk.py
¦  +- requirements.txt
+- frontend/
¦  +- index.html
¦  +- global.css
+- data/
¦  +- tasks.csv
¦  +- members.csv
¦  +- inventory.csv
¦  +- reservations.csv
¦  +- projects.json
¦  +- events.csv                     # created as events are logged
¦  +- audit.log                      # created when auth is enabled
¦  +- data-samples/
+- Dockerfile.api
+- Dockerfile.web
+- docker-compose.yml
+- nginx.conf
+- requirements.txt
```

## Run With Docker

```powershell
docker compose down
docker compose up -d --build
docker compose ps
```

Open:

- UI: `http://localhost:8080`
- API health (via NGINX): `http://localhost:8080/api/health`
- API docs (via NGINX): `http://localhost:8080/api/docs`

Notes:

- Current `docker-compose.yml` sets `TF_AUTH=0` (auth disabled for easy local demo)
- API container mounts `./data` to persist edits and model artifacts

## Local Python Run (without Docker)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8000 --reload
```

If you want to use model retraining (`/ai/retrain_task_risk`), also ensure these are installed:

```powershell
pip install scikit-learn joblib
```

## Auth + RBAC

Auth is controlled by environment variable:

- `TF_AUTH=1` enables auth middleware (default in code)
- `TF_AUTH=0` disables auth middleware

Optional API keys JSON:

- `TF_API_KEYS={"admin":"...","user":"...","viewer":"..."}`

Headers when auth is enabled:

- `x-api-key`
- `x-user-email`
- `x-project-id` (used for project-scoped notes)

Role resolution comes from `members.csv`:

- Preferred column: `rbac_role` (`viewer | user | admin`)
- Fallback column: `role` (legacy)

## Core Data Files

### `tasks.csv`

Required fields for planner/scoring:

- `id,title,project,assignee_email,status,due_date,story_points,impact,dependencies`

Optional field used by velocity forecast:

- `completed_date` (format `YYYY-MM-DD`)

### `members.csv`

Current write schema:

- `email,full_name,job_role,rbac_role,weekly_capacity`

Backward-compatible input still accepted:

- `role` (mapped to `job_role`)

### `inventory.csv`

- `sku,name,on_hand,reorder_point,lead_time_days`

### `reservations.csv`

- `reservation_id,task_id,sku,qty,planned_date,actual_date`

### `projects.json`

Array of objects:

```json
[{"key":"PORTAL","name":"Merchant Portal"}]
```

## Main API Endpoints

### Health and meta

- `GET /health`
- `GET /files`
- `GET /ai/health`

### Auth and collaboration

- `GET /auth/whoami`
- `GET /collab/notes`
- `POST /collab/notes`
- `GET /admin/audit_log` (admin)

### Import / export

- `POST /import/{dtype}` where `dtype in {tasks, members, inventory, reservations, projects}`
  - `members` import is intentionally blocked to prevent overwrite; use `POST /members/upsert_many`
- `GET /export/{dtype}`
- `GET /proof_bundle` (zip with events/model/tasks snapshot)

### Tasks and planning

- `GET /tasks/prioritized`
- `POST /tasks/upsert_many`
- `POST /tasks/delete_many`
- `POST /tasks/dedupe`
- `POST /tasks/plan_sprint`
- `GET /debug/deps`

### Members

- `POST /members/upsert_many`

### Inventory

- `GET /inventory`
- `GET /inventory/alerts`
- `GET /inventory/list`
- `POST /inventory/upsert_many`

### Team analytics

- `POST /team/insights`
- `GET /team/velocity_forecast`

### Learning loop and AI helpers

- `POST /events/log`
- `GET /events/stats`
- `GET /export/events`
- `GET /ai/task_risk`
- `POST /ai/retrain_task_risk`
- `GET /ai/anomalies`
- `GET /ai/followups`
- `GET /ai/engagement`

## UI Sections

The SPA includes these panels:

- Home
- Prioritized Tasks
- Sprint Plan
- Inventory
- Inventory Alerts
- Team Insights
- Members
- Data Checks
- Collab Notes

## Learning Loop Flow

1. User edits tasks in UI and saves.
2. UI logs events to `POST /events/log`.
3. API stores events in `data/events.csv`.
4. Retrain endpoint runs `backend/train_task_risk.py`.
5. New model is written to `data/task_risk_model.joblib` (+ meta JSON).
6. `/ai/task_risk` uses model predictions; falls back to heuristic if model is missing.

## Inventory Risk Logic

- Reserved quantities are aggregated from `reservations.csv`
- Consumption is grouped by date and SKU
- `GET /inventory` supports:
  - SMA-based rate (default)
  - Holt linear rate (`use_ml=true`)
- `at_risk` is flagged when projected stockout is sooner than `lead_time_days + safety_buffer`

## Validation + Anomaly Checks

`GET /data/validate` checks:

- missing task columns
- duplicate task IDs
- invalid `due_date`
- unknown assignees
- unknown dependencies
- non-numeric or negative points/impact

AI helper endpoints:

- `/ai/anomalies`: overdue tasks, suspicious estimates, negative stock, stockout signals
- `/ai/followups`: task follow-up recommendations
- `/ai/engagement`: assignee bottleneck detection

## Notes and Known Gaps

- Root `requirements.txt` and `backend/requirements.txt` are not identical
- Retraining requires `scikit-learn` and `joblib` available in runtime environment
- `Start.txt` currently contains duplicate/unrelated lines and is not part of runtime logic

## License

Private/demo usage.
