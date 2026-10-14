# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The shift start is built with `punch_in.replace(hour=..)` on a naive, server-local datetime, so it is not IST. | Punch in at 09:28 IST on a machine running UTC: the comparison is against 09:30 UTC. | Compare real instants against `shift_instant()`, the shift start in IST on the attendance date. |
| 2 | `compute_late_minutes` | `int(seconds / 60) > 10` gets the grace boundary wrong. 09:40:30 truncates to 10, so it is not late, but R2 says strictly more than 10:00 is late. | Shift 09:30, punch 09:40:01 must give 10, the starter gives 0. | Test `secs > 600`, then return `secs // 60`. |
| 3 | `compute_late_minutes` | Ignores overnight shifts, and early punch-ins can give negative values. | 22:00-06:00 shift, punch-in at 21:55 or 03:00. | Use the R1 attendance date, and return 0 unless late. |
| 4 | `compute_work_hours` | Python `round()` on a float is not half-up, and microseconds are not truncated. | 16218 s is 4.505 h; the starter returns 4.5 instead of 4.51. | `Decimal.quantize(..., ROUND_HALF_UP)` on whole seconds. |
| 5 | `compute_overtime` | No 30-minute minimum (R3). An overnight shift end is not moved to the next day. It subtracts a naive datetime from an aware one, which raises `TypeError` if reused. | Punch-out 5 minutes after shift end returns 5, not 0. | Threshold `>= 30`, `+1 day` for overnight shifts, everything timezone-aware. |
| 6 | `create_employee` | Check-then-insert race, and no unique index, so parallel creates make duplicates. | Fire two identical POSTs at once: both return 201. | Unique index on `emp_code` and catch `DuplicateKeyError` -> 409. |
| 7 | `create_employee` | `datetime.now()` is naive local time, and the response returns a datetime instead of epoch milliseconds. | `created_at` appears as an ISO string. | UTC time, converted to epoch ms at the boundary. |
| 8 | `EmployeeIn` | No validation of `emp_code`, email, `HH:MM` shifts, `joined_on` or `shift_start != shift_end`. A bad time string crashes later in `map(int, ...)`. | `shift_start: "9:5"` or `"abc"` leads to a 500 later. | Pydantic patterns and validators, returning 422. |
| 9 | `list_employees` | `skip = page * page_size` skips the first page. `total` counts the whole collection, ignoring the filter. No sort. No page validation, so `page=0` gives a negative skip and a 500. | `GET /employees?page=1` returns page 2. | `(page - 1) * page_size`, `count_documents(q)`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 10 | `punch_in` | An unknown employee makes `emp["shift_start"]` fail on `None`, giving a 500 instead of 404. | POST with an unknown `emp_code`. | Explicit 404. |
| 11 | `punch_in` | `fromtimestamp(ms / 1000)` uses server local time and keeps microseconds. `if body.punched_at` treats 0 as missing. No range check. | A seconds-valued `punched_at` is accepted. | UTC, truncated to seconds, and a strict bounded `EpochMs` type that returns 422. |
| 12 | `punch_in` | The date is the server-local date, not the IST date, and the overnight rule (R1) is ignored. | A punch at 23:00 UTC is already the next day in IST. | `attendance_date()`. |
| 13 | `punch_in` | `status` is any string, so `ABSENT` or `"FOO"` is accepted. | POST with `status: "FOO"`. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 14 | `punch_in` | Check-then-insert race, no unique index. | Parallel punch-ins return several 201s. | Unique `(emp_code, date)` index and `DuplicateKeyError` -> 409. |
| 15 | `punch_in` / `list_attendance` | The response leaks `id` (the `_id`) and returns datetimes, not epoch ms. | The response has an `id` field. | `record_out()` builds the API shape. |
| 16 | `list_attendance` | Loads the entire result set into memory, sorts in Python, and uses `total = len(docs)`. The sort misses the `emp_code` tiebreak. | Slow at 100k documents. | `find().sort().skip().limit()`, `count_documents`, and indexes. |
| 17 | `list_attendance` | No validation of dates, `date_from > date_to`, `status` or paging. Legacy documents without `half_day` or `history` give the wrong shape. | `date_from=abc` is silently accepted. | Validate and return 422. `record_out()` supplies defaults. |
| 18 | `/health` | Always returns ok without touching MongoDB. | MongoDB down still gives 200. | Ping MongoDB, return 503 on failure. |
| 19 | Startup | The client is not timezone-aware and no indexes are created. | Naive datetimes everywhere, and collection scans. | `tz_aware=True` and `ensure_indexes()` at startup. |

## Looked at and not a defect

- `load_dotenv()` does not override variables that are already set, so real environment variables already take priority.
- `doc.pop("_id", None)` in `create_employee` correctly removes the `_id` that `insert_one` adds to the dict.
- Comparing date strings with `$gte` / `$lte` is correct, because `YYYY-MM-DD` sorts the same as the dates it represents.