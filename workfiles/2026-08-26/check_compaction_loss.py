import sqlite3, datetime

con = sqlite3.connect(r"file:C:\Users\chine\.opensquilla\state\sessions.db?mode=ro", uri=True)

# 1) 表结构
cols = con.execute("PRAGMA table_info(compacted_transcript_entries)").fetchall()
print("列:", [c[1] for c in cols])

# 2) f4d2b4dc 在该表的残留
rows = con.execute(
    "SELECT COUNT(*), SUM(LENGTH(COALESCE(content,''))) FROM compacted_transcript_entries "
    "WHERE session_key='agent:main:webchat:f4d2b4dc'"
).fetchone()
print(f"\nf4d2b4dc 在 compacted_transcript_entries: {rows[0]} 条, 共 {rows[1]:,} 字符")

# 3) 该表最近被压缩的会话分布（按 session_key 计数，看这张表的整体情况）
dist = con.execute(
    "SELECT session_key, COUNT(*), SUM(LENGTH(COALESCE(content,''))) FROM compacted_transcript_entries "
    "GROUP BY session_key ORDER BY 3 DESC LIMIT 8"
).fetchall()
print("\n表内 Top8 会话（按字符量）:")
for r in dist:
    print(f"  {r[0]}: {r[1]} 条, {r[2]:,} 字符")

# 4) 该表最新写入时间
latest = con.execute("SELECT MAX(id) FROM compacted_transcript_entries").fetchone()[0]
print(f"\n最大 id: {latest}")
try:
    row = con.execute(
        "SELECT session_key, created_at FROM compacted_transcript_entries WHERE id=?", (latest,)
    ).fetchone()
    ts = row[1] / 1000 if row and row[1] else 0
    print("最新一条:", row[0], datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"))
except Exception as e:
    print("取最新一条失败:", e)
con.close()
