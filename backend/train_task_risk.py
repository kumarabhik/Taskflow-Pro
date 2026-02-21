import os
import json
import pandas as pd
from datetime import date, datetime, timezone
from pathlib import Path

from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, classification_report
import joblib


# ---------------- Paths ----------------
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(ROOT, "data")
TASKS = os.path.join(DATA_DIR, "tasks.csv")
MODEL_OUT = os.path.join(DATA_DIR, "task_risk_model.joblib")


# ---------------- Helpers ----------------
def attach_done_at_from_events(tasks: pd.DataFrame, data_dir: str) -> pd.DataFrame:
    events_path = Path(data_dir) / "events.csv"
    tasks = tasks.copy()

    # Default column (always exists)
    tasks["done_at"] = ""

    if not events_path.exists():
        return tasks

    ev = pd.read_csv(events_path)
    if ev.empty or "event_name" not in ev.columns:
        return tasks

    ev["ts"] = pd.to_datetime(ev.get("ts", ""), errors="coerce")
    ev["task_id"] = ev.get("task_id", "").astype(str)

    done_events = ev[
        ev["event_name"].isin(["task_marked_done", "task_saved_done", "task_done"])
    ].copy()

    if done_events.empty:
        return tasks

    done_at = (
        done_events.groupby("task_id")["ts"]
        .min()
        .reset_index()
        .rename(columns={"ts": "done_at"})
    )

    tasks["id"] = tasks.get("id", "").astype(str)
    out = tasks.merge(done_at, left_on="id", right_on="task_id", how="left")
    out.drop(columns=["task_id"], inplace=True, errors="ignore")

    # 🔒 GUARANTEE done_at exists and is safe
    if "done_at" not in out.columns:
        out["done_at"] = ""

    out["done_at"] = (
        pd.to_datetime(out["done_at"], errors="coerce")
        .dt.strftime("%Y-%m-%dT%H:%M:%S")
        .fillna("")
    )
    return out


def make_delay_label(tasks_df: pd.DataFrame) -> pd.Series:
    """
    y = 1 → delayed
    y = 0 → not delayed
    """
    due = pd.to_datetime(tasks_df.get("due_date", ""), errors="coerce")
    done_at = pd.to_datetime(tasks_df.get("done_at", ""), errors="coerce")
    status = tasks_df.get("status", "").astype(str).str.lower()

    delayed_done = (done_at.notna()) & (due.notna()) & (done_at > due)
    overdue_open = (
        due.notna()
        & (pd.Timestamp.now() > due)  # tz-naive safe
        & (~status.isin(["done", "completed", "closed", "resolved"]))
    )

    return (delayed_done | overdue_open).astype(int)


def parse_date(s):
    if pd.isna(s) or str(s).strip() == "":
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def days_to_due(d):
    if d is None:
        return 999.0
    return float((d - date.today()).days)


def deps_open(raw):
    if pd.isna(raw) or str(raw).strip() == "":
        return 0.0
    return float(len([x for x in str(raw).split(",") if x.strip()]))


# ---------------- Main ----------------
def main():
    if not os.path.exists(TASKS):
        raise SystemExit("tasks.csv not found in data/")

    df = pd.read_csv(TASKS)

    # Ensure schema
    for c in ["id", "status", "due_date", "impact", "story_points", "dependencies"]:
        if c not in df.columns:
            df[c] = ""

    df["impact"] = pd.to_numeric(df["impact"], errors="coerce").fillna(0.0)
    df["story_points"] = pd.to_numeric(df["story_points"], errors="coerce").fillna(0.0)
    df["due_parsed"] = df["due_date"].apply(parse_date)

    # ✅ Attach done_at from events.csv (single source of truth)
    df = attach_done_at_from_events(df, DATA_DIR)

    # ✅ Strong labels
    y = make_delay_label(df)

    # -------- Features (must match API order) --------
    X = pd.DataFrame({
        "days_to_due": df["due_parsed"].apply(days_to_due),
        "deps_open": df["dependencies"].apply(deps_open).clip(0, 5),
        "story_points": df["story_points"].clip(0, 50),
        "impact": df["impact"].clip(0, 10),
        "owner_load": (df["story_points"] / 10.0).clip(0, 5),
    }).fillna(0.0)

    # -------- Tiny / imbalanced data safety --------
    if y.sum() < 3:
        y = ((X["days_to_due"] <= 2) | (y == 1)).astype(int)

    vc = y.value_counts()
    stratify = y if (len(vc) >= 2 and vc.min() >= 2) else None

    test_size = 0.25 if len(y) >= 8 else 0.0
    X_train, X_test, y_train, y_test = train_test_split(
        X, y,
        test_size=test_size,
        random_state=42,
        stratify=stratify,
    )

    if test_size == 0.0:
        X_train, y_train = X, y

    # -------- Train --------
    model = LogisticRegression(max_iter=200, class_weight="balanced")
    model.fit(X_train, y_train)

    # -------- Evaluate --------
    if test_size > 0 and y_test.nunique() > 1:
        pred = model.predict(X_test)
        print("F1:", round(f1_score(y_test, pred), 3))
        print(classification_report(y_test, pred))
    else:
        pred = model.predict(X_train)
        print("Trained on full dataset (tiny / imbalanced)")
        print("F1 (train):", round(f1_score(y_train, pred), 3))
        print("Positives:", int(y_train.sum()), "/", len(y_train))

    # -------- Save --------
    joblib.dump(model, MODEL_OUT)

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "num_tasks": int(len(df)),
        "num_done_events": int((df["done_at"].astype(str).str.strip() != "").sum()),
    }

    with open(os.path.join(DATA_DIR, "task_risk_model.meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    print("Saved:", MODEL_OUT)


if __name__ == "__main__":
    main()
