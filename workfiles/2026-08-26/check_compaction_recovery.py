import sqlite3, datetime, json

con = sqlite3.connect(r"file:C:\Users\chine\.opensquilla\state\sessions.db?mode=ro", uri=True)

KEY = "agent:main:webchat:f4d2b4dc"

rows = con.execute(
    "SELECT id, role, LENGTH(COALESCE(content,'')), LENGTH(COALESCE(tool_calls,'')), "
    "LENGTH(COALESCE(reasoning_content,'')), archived_at, created_at "
    "FROM compacted_transcript_entries WHERE session_key=? ORDER BY archived_at", (KEY,)
).fetchall()

print(f"f4d2b4dc 归档条目: {len(rows)} 条")
total_content = sum(r[2] for r in rows)
total_tool = sum(r[3] for r in rows)
total_reason = sum(r[4] for r in rows)
print(f"content 合计 {total_content:,} 字符; tool_calls {total_tool:,}; reasoning {total_reason:,}")

# 找那个 17 万字符的摘要条目
big = [r for r in rows if r[2] > 100000]
print(f"\n超大 content 条目 (>100k 字符): {len(big)} 条")
for r in big:
    print(f"  id={r[0]} role={r[1]} content={r[2]:,} archived={r[5]}")

# 归档时间分布
arch = [r[5] for r in rows if r[5]]
if arch:
    print("\n归档时间范围:",
          datetime.datetime.fromtimestamp(min(arch)/1000).strftime("%m-%d %H:%M"),
          "~",
          datetime.datetime.fromtimestamp(max(arch)/1000).strftime("%m-%d %H:%M"))

# 最大单条（摘要特征）
rows2 = con.execute(
    "SELECT role, content FROM compacted_transcript_entries WHERE session_key=? AND LENGTH(COALESCE(content,''))>50000 "
    "ORDER BY LENGTH(content) DESC LIMIT 1", (KEY,)
).fetchall()
if rows2:
    head = rows2[0][1][:200].replace("\n", " ")
    print(f"\n最大摘要条目前 200 字符 (role={rows2[0][0]}):")
    print(head)
con.close()
