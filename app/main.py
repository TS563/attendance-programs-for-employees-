"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded, but real environment variables win)
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # load_dotenv() never overrides variables that are already set

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
WEEKDAYS = [2, 3, 4, 5, 6]  # MongoDB $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]

# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
_indexes_ready = False


def ensure_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    e, a = db.employees, db.attendance_logs
    e.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    e.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    e.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    e.create_index([("joined_on", ASCENDING)], name="joined")
    a.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    a.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    a.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp")
    a.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punchin")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # /health retries, so a slow MongoDB start is not fatal
        print(f"index creation deferred: {exc}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    dt = aware(dt)
    return None if dt is None else (dt - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)  # BSON dates keep milliseconds


def valid_date_str(s: str) -> bool:
    if not DATE_RE.fullmatch(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def parse_date(s: str, name: str = "date") -> date_cls:
    if not valid_date_str(s):
        raise HTTPException(422, f"{name} must be a valid YYYY-MM-DD date")
    return datetime.strptime(s, "%Y-%m-%d").date()


def month_bounds(month: str):
    """-> (first day, last day, first day of next month) as date objects."""
    y, m = int(month[:4]), int(month[5:7])
    if y < 1 or not 1 <= m <= 12:
        raise HTTPException(422, "month must be YYYY-MM")
    first = date_cls(y, m, 1)
    nxt = date_cls(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1), nxt


def round_half_up(x, places: int) -> float:
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules R1-R5
# --------------------------------------------------------------------------- #
def parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM strings compare correctly


def shift_instant(d: date_cls, hhmm: str, add_days: int = 0) -> datetime:
    h, m = parse_hhmm(hhmm)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST) + timedelta(days=add_days)


def attendance_date(ts: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: IST calendar date of punch_in; overnight shifts before shift_end belong to the previous day."""
    local = ts.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if local.hour * 3600 + local.minute * 60 + local.second < eh * 3600 + em * 60:
            d -= timedelta(days=1)
    return d


def compute_late_minutes(punch_in: datetime, att_date: date_cls, shift_start: str) -> int:
    """R2: late only if strictly more than 600 s after shift_start; then floor minutes since shift_start."""
    secs = int((punch_in - shift_instant(att_date, shift_start)).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, not float round())."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, att_date: date_cls, shift_start: str, shift_end: str) -> int:
    """R3: floor minutes after shift_end, counted only when >= 30. Overnight shift ends the next day."""
    end = shift_instant(att_date, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    mins = int((punch_out - end).total_seconds() // 60)
    return mins if mins >= 30 else 0


def derive(emp: dict, att_date: date_cls, status: str, punch_in, punch_out) -> dict:
    """All derived fields for a record, from its final status and punch times."""
    ss, se = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, att_date, ss)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": wh,
        "late_minutes": late,
        "overtime_minutes": compute_overtime(punch_out, att_date, ss, se),
        "half_day": wh < 4.5,  # R5, on the rounded value
    }


# --------------------------------------------------------------------------- #
# Serialisation (Mongo document -> API shape). _id never leaves the server.
# --------------------------------------------------------------------------- #
def history_out(h: dict) -> dict:
    changes = {}
    for k, v in (h.get("changes") or {}).items():
        if k in ("punch_in", "punch_out"):
            v = {"from": dt_to_ms(v.get("from")), "to": dt_to_ms(v.get("to"))}
        changes[k] = v
    return {"at": dt_to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def record_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": [history_out(h) for h in (d.get("history") or [])],
    }


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d.get("name"),
        "email": d.get("email"),
        "department": d.get("department"),
        "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"),
        "joined_on": d.get("joined_on"),
        "created_at": dt_to_ms(d.get("created_at")),
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
# strict=True rejects floats and strings; the bounds reject values that look like seconds.
EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=TIME_PATTERN)
    shift_end: str = Field("18:30", pattern=TIME_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on(cls, v):
        if not valid_date_str(v):
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code is the real guard (no check-then-insert)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    att_date = attendance_date(ts, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, att_date, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one of N racers wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    pin = aware(rec["punch_in"])
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours of it")
    d = derive(emp, parse_date(rec["date"]), rec["status"], pin, ts)
    update = {
        "punch_out": ts,
        "work_hours": d["work_hours"],
        "overtime_minutes": d["overtime_minutes"],
        "half_day": d["half_day"],
    }
    # Conditional update: only succeeds while the record is still open, so two racers can't both win.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_query(emp_code, date_from, date_to, status) -> dict:
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.estimated_document_count() if not q else db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(ATT_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    att_date = parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_status, old_in, old_out = rec["status"], aware(rec.get("punch_in")), aware(rec.get("punch_out"))
    new_status = body.status or old_status
    req_in = ms_to_dt(body.punch_in) if body.punch_in is not None else None
    req_out = ms_to_dt(body.punch_out) if body.punch_out is not None else None

    if new_status in ("ABSENT", "LEAVE"):
        if req_in is not None or req_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE clear the punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = req_in or old_in
        new_out = req_out or old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if req_in is not None and attendance_date(
            new_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
        ) != att_date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if new_status == old_status and new_in == old_in and new_out == old_out:
        raise HTTPException(422, "the request changes nothing")

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                **derive(emp, att_date, new_status, new_in, new_out)}
    old_vals = {
        "status": old_status, "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if v != old_vals[k]}
    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    hist = rec.get("history")
    # Optimistic concurrency: the update only applies if nobody changed the record since we read it.
    flt = {
        "_id": rec["_id"],
        "status": old_status,
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "history": {"$size": len(hist)} if hist is not None else {"$exists": False},
    }
    updated = db.attendance_logs.find_one_and_update(
        flt, {"$set": new_vals, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the real endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def rec_exprs(p: str) -> dict:
    """Per-record expressions. p = '$' for a top-level document, '$$l.' for $map/$filter variable l.
    Reads the STORED derived values; nothing is recomputed from punch times."""
    v = lambda n: p + n  # noqa: E731
    present = {"$in": [v("status"), PRESENCE]}
    weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": v("date"), "format": "%Y-%m-%d"}}}, WEEKDAYS]}
    half = {"$eq": [{"$ifNull": [v("half_day"), False]}, True]}
    late = {"$ifNull": [v("late_minutes"), 0]}
    has_wh = {"$and": [present, {"$isNumber": v("work_hours")}]}
    return {
        "present_days": {"$cond": [{"$and": [present, weekday]}, {"$cond": [half, 0.5, 1]}, 0]},
        "is_late": {"$cond": [{"$gt": [late, 0]}, 1, 0]},
        "late_min": late,
        "ot_min": {"$ifNull": [v("overtime_minutes"), 0]},
        "is_leave": {"$cond": [{"$eq": [v("status"), "LEAVE"]}, 1, 0]},
        "is_on_duty": {"$cond": [{"$eq": [v("status"), "ON_DUTY"]}, 1, 0]},
        # work_hours in hundredths -> integer sums, so the later average rounds exactly (half-up)
        "wh100": {"$cond": [has_wh, {"$round": [{"$multiply": [v("work_hours"), 100]}, 0]}, 0]},
        "has_wh": {"$cond": [has_wh, 1, 0]},
    }


def monthly_pipeline(emp_code: str, first: date_cls, nxt: date_cls) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}}},
        {"$group": {"_id": None, **{k: {"$sum": ex} for k, ex in rec_exprs("$").items()}}},
    ]


def dept_summary_pipeline(first: date_cls, last: date_cls, nxt: date_cls, department: Optional[str]) -> list:
    emp_match = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department is not None:
        emp_match["department"] = department
    ex = rec_exprs("$$l.")
    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}}},
                {"$project": {"_id": 0, "status": 1, "date": 1, "half_day": 1, "late_minutes": 1,
                              "overtime_minutes": 1, "work_hours": 1}},
            ],
            "as": "logs",
        }},
        # one row per employee (employees with no logs keep an empty array -> zeros, still counted)
        {"$project": {"_id": 0, "department": 1,
                      **{k: {"$sum": {"$map": {"input": "$logs", "as": "l", "in": e}}} for k, e in ex.items()}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **{k: {"$sum": f"${k}"} for k in ex}}},
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: date_cls, nxt: date_cls, limit: int, department: Optional[str], codes: list) -> list:
    match = {"date": {"$gte": first.isoformat(), "$lt": nxt.isoformat()}, "late_minutes": {"$gt": 0}}
    if department is not None:
        match["emp_code"] = {"$in": codes}  # narrows the scan to that department's employees
    pipe = [
        {"$match": match},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped here
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})
    pipe += [
        # $rank = standard competition ranking: ties share a rank, next rank skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipe


def trend_pipeline(department: str, codes: list, d_from: date_cls, d_to: date_cls) -> list:
    f, t = d_from.isoformat(), d_to.isoformat()
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    to_day = lambda s: {"$dateFromString": {"dateString": s, "format": "%Y-%m-%d"}}  # noqa: E731
    half = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    return [
        # 1) per-day present (in half units, so 0.5 stays an integer) and late counts from the logs
        {"$match": {"date": {"$gte": f, "$lte": t}, "emp_code": {"$in": codes}}},
        {"$group": {
            "_id": "$date",
            "present_halves": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]}, {"$cond": [half, 1, 2]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "day": to_day("$_id"), "joins": {"$literal": 0}, "present_halves": 1, "late_count": 1}},
        # 2) one row per employee who has joined by `to`, placed on max(joined_on, from): running sum = headcount (R9)
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department, "joined_on": {"$lte": t}}},
            {"$project": {"_id": 0,
                          "day": to_day({"$cond": [{"$lt": ["$joined_on", f]}, f, "$joined_on"]}),
                          "joins": {"$literal": 1}, "present_halves": {"$literal": 0}, "late_count": {"$literal": 0}}},
        ]}},
        {"$group": {"_id": "$day", "joins": {"$sum": "$joins"}, "present_halves": {"$sum": "$present_halves"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$project": {"_id": 0, "day": "$_id", "joins": 1, "present_halves": 1, "late_count": 1}},
        # 3) gap filling: exactly one row per calendar day in [from, to]
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$set": {"joins": {"$ifNull": ["$joins", 0]}, "present_halves": {"$ifNull": ["$present_halves", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]}}},
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}}}}},
        {"$set": {"is_working_day": {"$in": [{"$dayOfWeek": "$day"}, WEEKDAYS]}}},
        # 4) rate in units of 1e-4, half-up with integer maths: floor((10000*halves + h) / (2h))
        {"$set": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                                    {"$multiply": ["$headcount", 2]}]}},
            None]}}},
        # 5) 7-row moving average over the non-null rates inside the range (window starts at the first row)
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "ma_sum": {"$sum": "$rate_int", "window": {"documents": [-6, "current"]}},
            "ma_cnt": {"$sum": {"$cond": [{"$ne": ["$rate_int", None]}, 1, 0]}, "window": {"documents": [-6, "current"]}},
        }}},
        {"$set": {"ma_int": {"$cond": [
            {"$gt": ["$ma_cnt", 0]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$ma_sum", 2]}, "$ma_cnt"]}, {"$multiply": ["$ma_cnt", 2]}]}},
            None]}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
            "is_working_day": 1, "headcount": 1,
            "present_count": {"$divide": ["$present_halves", 2]},
            "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$rate_int", None]}, None, {"$divide": ["$rate_int", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$ma_int", None]}, None, {"$divide": ["$ma_int", 10000]}]},
        }},
        {"$sort": {"date": 1}},
    ]


def working_days(first: date_cls, last: date_cls, joined_on: Optional[str]) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last. Pure calendar arithmetic, no data involved."""
    start = first
    if joined_on and valid_date_str(joined_on):
        start = max(first, datetime.strptime(joined_on, "%Y-%m-%d").date())
    n, d = 0, start
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def dept_codes(department: str) -> list:
    return db.employees.distinct("emp_code", {"department": department})


def check_trend_range(frm: str, to: str):
    d_from, d_to = parse_date(frm, "from"), parse_date(to, "to")
    if d_to < d_from:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return d_from, d_to


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last, nxt = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, nxt)))
    r = rows[0] if rows else {}
    wd = working_days(first, last, emp.get("joined_on"))
    present = float(r.get("present_days", 0))
    pct = None
    if wd > 0:
        pct = float((Decimal(str(present)) * 100 / Decimal(wd)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {
        "emp_code": emp_code, "month": month, "working_days": wd, "present_days": present,
        "leave_days": int(r.get("is_leave", 0)), "late_count": int(r.get("is_late", 0)),
        "total_late_minutes": int(r.get("late_min", 0)), "total_overtime_minutes": int(r.get("ot_min", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last, nxt = month_bounds(month)
    items = []
    for r in db.employees.aggregate(dept_summary_pipeline(first, last, nxt, department)):
        n = int(round(r["has_wh"]))
        avg = None
        if n > 0:  # exact: integer hundredths / record count, then half-up to 2 decimals
            avg = float((Decimal(int(round(r["wh100"]))) / Decimal(n) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]), "present_days": float(r["present_days"]),
            "avg_work_hours": avg, "late_count": int(r["is_late"]), "total_late_minutes": int(r["late_min"]),
            "leave_count": int(r["is_leave"]), "on_duty_count": int(r["is_on_duty"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, _last, nxt = month_bounds(month)
    codes = dept_codes(department) if department is not None else []
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(first, nxt, limit, department, codes))
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
              "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"])} for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    d_from, d_to = check_trend_range(from_, to)
    codes = dept_codes(department)
    if not codes:
        raise HTTPException(404, "unknown department")
    rows = db.attendance_logs.aggregate(trend_pipeline(department, codes, d_from, d_to))
    items = [{
        "date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
        "present_count": float(r["present_count"]), "late_count": int(r["late_count"]),
        "attendance_rate": r["attendance_rate"], "moving_avg_7d": r["moving_avg_7d"],
    } for r in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def _need(value, name):
    if value is None:
        raise HTTPException(422, f"query parameter '{name}' is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = attendance_query(emp_code, date_from, date_to, status)
        coll = "attendance_logs"
        cmd = {"find": coll, "filter": q, "sort": dict(ATT_SORT), "skip": (page - 1) * page_size, "limit": page_size}
    else:
        if endpoint == "employee_monthly":
            first, _l, nxt = month_bounds(_need(month, "month"))
            coll, pipe = "attendance_logs", monthly_pipeline(_need(emp_code, "emp_code"), first, nxt)
        elif endpoint == "department_summary":
            first, last, nxt = month_bounds(_need(month, "month"))
            coll, pipe = "employees", dept_summary_pipeline(first, last, nxt, department)
        elif endpoint == "late_leaderboard":
            first, _l, nxt = month_bounds(_need(month, "month"))
            codes = dept_codes(department) if department is not None else []
            coll, pipe = "attendance_logs", leaderboard_pipeline(first, nxt, limit, department, codes)
        else:  # department_trend
            dept = _need(department, "department")
            d_from, d_to = check_trend_range(_need(from_, "from"), _need(to, "to"))
            coll, pipe = "attendance_logs", trend_pipeline(dept, dept_codes(dept), d_from, d_to)
        cmd = {"aggregate": coll, "pipeline": pipe, "cursor": {}}
    out = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(out))}