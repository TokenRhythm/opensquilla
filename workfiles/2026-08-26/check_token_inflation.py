import sqlite3, datetime, json

con = sqlite3.connect(r"file:C:\Users\chine\.opensquilla\state\sessions.db?mode=ro", uri=True)
KEY = "agent:main:webchat:f4d2b4dc"

# 11:39:38 预检时 total_tokens=1,210,665；11:40 压缩把当时条目移入归档表
# 对归档表全量对账：token_count 分布 + 总和验证
rows = con.execute("""
    SELECT id, role, LENGTH(COALESCE(content,'')) AS clen, token_count, created_at,
           LENGTH(COALESCE(tool_calls,'')) AS tclen,
           LENGTH(COALESCE(reasoning_content,'')) AS rclen,
           turn_usage IS NOT NULL AS has_usage, archived_at
    FROM compacted_transcript_entries WHERE session_key=?
    ORDER BY token_count DESC
""", (KEY,)).fetchall()

print(f"f4d2b4dc 归档条目共 {len(rows)} 条")
total_tc = sum(r[3] or 0 for r in rows)
total_clen = sum(r[2] or 0 for r in rows)
print(f"token_count 总和: {total_tc:,} | content 字符总和: {total_clen:,} | 整体比例: {total_tc/max(total_clen,1):.2f} token/char")
print(f"(预检触发值 1,210,665 vs 归档总和 {total_tc:,} —— 差值含当时未归档的近期尾部)")

print("\n=== token_count 降序 Top 20（虚高条目排查）===")
for r in rows[:20]:
    id_, role, clen, tc, ts, tclen, rclen, has_usage, arch = r
    ratio = (tc / clen) if (clen and tc) else 0
    dt = datetime.datetime.fromtimestamp(ts/1000).strftime("%m-%d %H:%M") if ts else "-"
    adt = datetime.datetime.fromtimestamp(arch/1000).strftime("%m-%d %H:%M") if arch else "-"
    print(f"id={id_} role={role:9s} clen={clen:>7,} tc={tc:>9,} tclen={tclen:>6} rclen={rclen:>6} usage={has_usage} ratio={ratio:>5.2f} at={dt} arch={adt}")

# 求和但排除虚高条目（token_count <= clen*2 视为正常）
normal = [r for r in rows if (r[3] or 0) <= max((r[2] or 0) * 2, 200)]
abnormal = [r for r in rows if (r[3] or 0) > max((r[2] or 0) * 2, 200)]
print(f"\n正常条目 {len(normal)} 条: token_count 合计 {sum(r[3] or 0 for r in normal):,}")
print(f"虚高条目 {len(abnormal)} 条: token_count 合计 {sum(r[3] or 0 for r in abnormal):,}")
con.close()
