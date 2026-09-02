import sqlite3

db = sqlite3.connect(r"C:\Users\chine\.opensquilla\state\sessions.db")
db.row_factory = sqlite3.Row

print("== tables ==")
for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    print(r["name"])

print("\n== find session ir0u1msy terminal messages ==")
try:
    rows = db.execute(
        "SELECT * FROM messages WHERE session_key LIKE ? ORDER BY rowid DESC LIMIT 60",
        ("%ir0u1msy%",),
    ).fetchall()
except Exception as exc:
    print("messages table failed:", exc)
    rows = []

for r in rows:
    keys = r.keys()
    content = str(r["content"]) if "content" in keys else ""
    meta = str(r["metadata"]) if "metadata" in keys else ""
    if any(k in (content + meta) for k in ("too_large", "b9c97f84", "terminal", "budgetLimited")):
        print("---")
        for k in keys:
            v = str(r[k])
            if len(v) > 300:
                v = v[:300] + "..."
            print(f"  {k} = {v}")
db.close()
