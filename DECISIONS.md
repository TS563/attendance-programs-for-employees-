# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
- Indexes. employees: unique emp_code (uniqueness and lookups), (department, emp_code) for the sorted employee list, (department, joined_on) and (joined_on) for headcount. attendance_logs: unique (emp_code, date) (one record per day, per-employee queries), (date desc, emp_code) for the list sort and month scans, (status, date desc, emp_code) for status filters, (emp_code, punch_in desc) to find the record to punch out. I rejected a standalone status index because it is too unselective.

2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
- Punch-in race. Both requests compute the same date and call insert_one. The unique (emp_code, date) index lets MongoDB accept only one; the other raises DuplicateKeyError, which I return as 409. I never check first, so there is no gap.

3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
-Ties. I rank with $rank before applying the limit, then keep rows where rank <= limit. Employees tied at the cutoff all appear, so the result can exceed limit.

4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
-Headcount. The summary starts from employees (joined by month end) and uses $lookup for logs. An employee with no logs gets an empty array, contributes zeros, and is still counted.

5. **One thing you would change** if this had to serve 100x the data.
-At 100x data. I would copy department into each log, so analytics need no join, and add monthly rollups.
