import sqlite3, json, datetime

con = sqlite3.connect(r"file:C:\Users\chine\.opensquilla\state\sessions.db?mode=ro", uri=True)
KEY = "agent:main:webchat:f4d2b4dc"

# 取两个最极端的条目：id=4145 (770k) 和 id=4137 (277k)
for entry_id in (4145, 4137):
    row = con.execute("""
        SELECT id, role, content, tool_calls, token_count, turn_usage, created_at
        FROM compacted_transcript_entries WHERE session_key=? AND id=?
    """, (KEY, entry_id)).fetchone()
    id_, role, content, tool_calls, tc, usage, ts = row
    print(f"===== id={id_} role={role} token_count={tc:,} =====")
    print(f"content({len(content or '')} chars): {(content or '')[:150]!r}")
    try:
        calls = json.loads(tool_calls) if tool_calls else []
        print(f"tool_calls: {len(calls)} 个")
        # 逐个看 name + input 大小
        for i, c in enumerate(calls[:8]):
            inp = json.dumps(c.get("function", {}).get("arguments", c.get("input", "")))
            name = c.get("function", {}).get("name", c.get("name", "?"))
            print(f"  [{i}] {name}: input {len(inp):,} chars | 输入头部: {inp[:120]!r}")
        if len(calls) > 8:
            print(f"  ... 共 {len(calls)} 个调用")
    except Exception as e:
        print(f"tool_calls 解析失败: {e} | 原始头部: {tool_calls[:200]!r}")
    if usage:
        u = json.loads(usage)
        print(f"turn_usage: input={u.get('input_tokens'):,} output={u.get('output_tokens'):,}")
    print()

con.close()
