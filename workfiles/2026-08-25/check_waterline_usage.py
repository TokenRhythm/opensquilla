import sqlite3, json, datetime

con = sqlite3.connect(r"file:C:\Users\chine\.opensquilla\state\sessions.db?mode=ro", uri=True)
rows = con.execute(
    "SELECT id, created_at, turn_usage FROM transcript_entries "
    "WHERE session_key='agent:gaokao-agent:webchat:z08yyx0l' AND role='assistant' AND turn_usage IS NOT NULL "
    "ORDER BY id DESC LIMIT 12"
).fetchall()
for rid, ts, usage in rows:
    u = json.loads(usage)
    print(rid, datetime.datetime.fromtimestamp(ts/1000).strftime("%m-%d %H:%M"),
          "input:", u.get("input_tokens"), "cached:", u.get("cached_tokens"))
con.close()
