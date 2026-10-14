# Employee Attendance & Analytics API

A backend for employee attendance, built with FastAPI and MongoDB. Employees punch in and out, managers can correct records with an audit trail, and a set of analytics endpoints answer questions about the data using MongoDB aggregation pipelines. All application code is in `app/main.py`.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (a local Docker container or a free MongoDB Atlas cluster)

## How to run

1. Create and activate a virtual environment, then install the dependencies:

```bash
   python -m venv .venv
   # Windows:      .venv\Scripts\activate
   # macOS/Linux:  source .venv/bin/activate
   pip install -r requirements.txt
```

2. Set the connection details as environment variables:

```bash
   # macOS/Linux
   export MONGO_URI="mongodb://localhost:27017"   # or your Atlas URI
   export MONGO_DB="attendance_db"

   # Windows PowerShell
   $env:MONGO_URI="mongodb://localhost:27017"
   $env:MONGO_DB="attendance_db"
```

   A local `.env` file with the same two variables also works, but real environment variables take priority.

3. Start the server from the repository root:

```bash
   uvicorn app.main:app --port 8000
```

4. Check it:
   - `GET http://localhost:8000/health` returns `{"status":"ok"}` once MongoDB answers (503 otherwise).
   - `http://localhost:8000/docs` shows the interactive API documentation.

Indexes are created automatically at startup, so no manual setup is needed.

## What is implemented

Every endpoint in the contract:

- **Employees:** create (409 on a duplicate `emp_code`) and list (department filter, pagination).
- **Attendance:** punch-in, punch-out (works across midnight for overnight shifts), list (filters, pagination), and a PATCH endpoint for manual corrections that recomputes the derived fields and appends to the record's `history`.
- **Analytics (aggregation pipelines):** employee monthly summary, department summary, late-comers leaderboard with tied ranks, and a daily department trend with gap filling and a 7-day moving average.
- **Admin:** `GET /admin/explain/{endpoint}` returns MongoDB's `executionStats` explain output for each endpoint's main query.

## Design notes

- Instants are epoch milliseconds in the API and UTC datetimes in MongoDB, truncated to whole seconds. Dates and shift times are IST.
- Concurrency uses database guarantees instead of check-then-write: unique indexes for punch-in and employee creation, a conditional update for punch-out, and an optimistic check for PATCH (the loser of a race gets 409).
- Analytics read the stored derived values (`late_minutes`, `work_hours`, and so on) and never recompute them from the punch times.
- Rounding is half-up, done with integer arithmetic inside the pipelines so exact ties round correctly.

## Assumptions

- `moving_avg_7d` is the average of the already rounded daily `attendance_rate` values.
- A PATCH counts as changing nothing when `status`, `punch_in` and `punch_out` all match what is stored. Such a request returns 422.

## Testing

I tested with my own data covering ties, employees with no logs, weekend records, a mid-month joiner, an overnight shift, and parallel requests. The test script is not part of this repository.

Nothing from the contract is missing.