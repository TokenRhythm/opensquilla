import sqlite3

db = sqlite3.connect(r"C:\Users\chine\.opensquilla\state\sessions.db")
db.row_factory = sqlite3.Row

for table in ("turn_errors", "agent_tasks"):
    print(f"===== {table} columns =====")
    cols = [c[1] for c in db.execute(f"PRAGMA table_info({table})")]
    print(cols)
    try:
        rows = db.execute(
            f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 8"
        ).fetchall()
        for r in rows:
            print("---")
            for k in r.keys():
                v = str(r[k])
                if len(v) > 220:
                    v = v[:220] + "..."
                print(f"  {k} = {v}")
    except Exception as exc:
        print(f"query failed: {exc}")
db.close()
