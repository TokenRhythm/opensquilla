---
name: meta-travel-planner
description: "Use this meta-skill instead of answering directly when the user needs a trip plan, travel itinerary, business-trip schedule, or day-by-day travel brief that benefits from multi-skill orchestration across preference inference, weather, place search, constraint extraction, itinerary drafting, variants, and optional artifact guidance."
kind: meta
meta_priority: 50
always: false
final_text_mode: "step:final_plan"
triggers:
  - "travel plan"
  - "trip plan"
  - "trip itinerary"
  - "travel itinerary"
  - "day-by-day travel"
  - "days in"
  - "day in"
  - "plan my trip"
  - "plan our trip"
  - "plan a trip"
  - "itinerary for"
  - "旅游计划"
  - "出差行程"
  - "行程安排"
  - "规划行程"
  - "帮我安排"
  - "怎么玩"
  - "做个行程"
provenance:
  origin: opensquilla-original
  license: Apache-2.0
composition:
  steps:
    - id: trip_collect
      kind: llm_chat
      with:
        system: "You extract travel requirements without asking a follow-up unless the destination or trip length is genuinely absent."
        task: |
          Extract a structured trip brief from the original user request.
          Do NOT ask the user to confirm details that are already stated or
          safely inferable. If a value is missing, make a conservative
          assumption and mark it as ASSUMED, except only when destination or trip length is absent.
          In that case set NEEDS_CLARIFICATION: yes so the next step can pause
          and ask for the missing critical details.

          Do not invent exact calendar dates, weekdays, booking status, or
          weather probabilities from vague timing such as "late June" or
          "sometime next year". Preserve the user's wording when the year or
          exact dates are absent.

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(1400) }}

          Return exactly:
          DESTINATION: <city/region, or ASSUMED: ...>
          DAYS: <integer or ASSUMED: ...>
          DATES: <date range/season, or ASSUMED: ...>
          PARTY: <party size/type, or ASSUMED: ...>
          BUDGET: <budget|mid|premium, or ASSUMED: mid>
          PACE: <relaxed|balanced|packed>
          INTERESTS:
            - <interest>
          MUST_INCLUDE:
            - <explicit user requirement>
          NEEDS_CLARIFICATION: <yes|no>
          MISSING_FIELDS:
            - <destination|days|none>
          CLARIFY_REASON: <one concise reason, or none>
          ASSUMPTIONS:
            - <assumption>
    - id: trip_clarify
      kind: user_input
      depends_on: [trip_collect]
      when: "'NEEDS_CLARIFICATION: yes' in outputs.trip_collect"
      clarify:
        mode: form
        intro: |
          行程关键条件还不完整。请补齐目的地和天数；如果已有信息不变，可以重复填写。
        nl_extract: true
        fields:
          - name: destination
            type: string
            required: true
            prompt: "目的地 / Destination"
            max_chars: 120
          - name: days
            type: int
            required: true
            min: 1
            max: 60
            prompt: "旅行天数 / Number of days"
          - name: dates
            type: string
            prompt: "日期或季节 / Dates or season"
            max_chars: 120
          - name: party
            type: string
            prompt: "同行人和人数 / Party size"
            max_chars: 120
          - name: must_include
            type: string
            prompt: "必须包含或避开的事项 / Must include or avoid"
            max_chars: 300
        cancel_keywords: ["算了", "取消", "cancel", "stop", "abort"]
        timeout_hours: 24
    - id: trip_preferences
      kind: llm_chat
      depends_on: [trip_collect, trip_clarify]
      with:
        system: "You expand extracted travel facts into a structured planning contract."
        task: |
          Expand the extracted travel facts into a full planning contract.
          Never return a clarification question. If facts are uncertain, keep
          the assumption explicit and continue with a practical default.
          Prefer explicit clarification answers over first-pass assumptions.

          Extracted facts:
          {{ outputs.trip_collect | truncate(1200) }}

          Clarification answers (may be empty when not needed):
          {{ inputs.get('collected', {}).get('trip_clarify', {}) | tojson }}

          Original user request:
          {{ inputs.user_message | xml_escape | truncate(1200) }}

          Return exactly:
          DESTINATION: <city/region>
          DATES: <duration, date range, season, or ASSUMED value>
          PARTY: <party size/type>
          BUDGET: <budget level>
          PACE: <relaxed|balanced|packed>
          INTERESTS:
            - <interest>
          CONSTRAINTS:
            - <constraint or assumption>
    - id: weather
      kind: skill_exec
      skill: weather
      depends_on: [trip_preferences]
      with:
        location: "{{ outputs.trip_preferences | truncate(512) }}"
        days: 3
        max_chars: 2200
    - id: poi
      kind: skill_exec
      skill: multi-search-engine
      depends_on: [trip_preferences]
      with:
        query: "{{ outputs.trip_preferences | truncate(512) }} sights restaurants transport hours neighborhoods"
        engines: [brave, duckduckgo]
        max_results: 15
    - id: constraints
      kind: llm_chat
      depends_on: [weather, poi]
      with:
        system: "You convert weather and search results into itinerary constraints."
        task: |
          Extract itinerary constraints from weather and POI results: opening
          hours, transit time assumptions, weather risks, neighborhoods to
          group together, and any likely booking constraints.

          Evidence boundary:
          - Weather tools often return short-range/current forecasts. If the
            trip timing is vague, seasonal, or outside the forecast window, do
            not convert current weather into trip-day probabilities. Use
            seasonal risk language and mark exact forecast as unavailable.
          - If POI search is thin or missing, do not list restaurants, opening
            hours, events, or booking requirements as verified.
          - Preserve explicit mobility, dietary, fixed-booking, budget, and
            rest constraints before adding optional attractions.

          Preferences:
          {{ outputs.trip_preferences | truncate(1200) }}

          Weather:
          {{ outputs.weather | truncate(2000) }}

          POI search:
          {{ outputs.poi | truncate(6000) }}
    - id: itinerary
      kind: llm_chat
      depends_on: [constraints]
      with:
        system: "You write complete, practical travel itineraries. Return only the itinerary."
        task: |
          Build the primary day-by-day itinerary. It must be complete enough
          to use without reading any later step.

          Include:
          - assumptions
          - one section per day with morning / afternoon / evening
          - neighborhood grouping and transit notes
          - food suggestions
          - rain-aware risks and substitutions
          - rough budget notes

          Trip preferences:
          {{ outputs.trip_preferences | truncate(1200) }}

          Weather forecast:
          {{ outputs.weather | truncate(2000) }}

          POI search:
          {{ outputs.poi | truncate(5000) }}

          Constraints:
          {{ outputs.constraints | truncate(3000) }}
    - id: final_plan
      kind: llm_chat
      depends_on: [itinerary, constraints, weather, poi]
      with:
        system: "You assemble complete travel plans for users. Return only the final answer."
        task: |
          Assemble the complete travel product. Do not return only variants.
          Do not include process commentary.
          Return every required section. Keep the whole answer compact enough
          to fit in one model response: 4,500-6,500 characters is preferred.
          If space is tight, shorten day descriptions before omitting the
          variants, evidence, next-step, or artifact sections.

          Required sections:
          1. Assumptions
          2. Primary itinerary matching the requested or inferred trip length
          3. Weather-aware risks and rain backups
          4. Variants
          5. Budget and booking notes
          6. Evidence and source notes
          7. Next steps

          Preserve concrete timings, neighborhoods, transit grouping, food
          ideas, weather constraints, and budget constraints. Do not open with
          "I researched" or imply live verification unless a tool result is
          shown in the evidence notes. Do not invent exact trip calendar dates,
          weekdays, or daily rain percentages from vague timing such as
          "late June" unless the user supplied exact dates and weather evidence
          covers those dates. If weather evidence is short-range/current but
          the trip is future or seasonal, say "seasonal planning assumption"
          rather than "forecast". Keep each day to 5-7 highly actionable
          bullets or a compact schedule. Include:
          - a Route spine line for each day, e.g. Neighborhood A -> B -> C
          - no more than 2-3 main anchors per day unless the user requested a
            packed pace
          - an explicit pacing note: relaxed/balanced/packed and what to skip
            if tired
          - one rest block or pacing reset per day for balanced/relaxed trips
          - transit-coherent neighborhood adjacency; avoid cross-city zigzags
          - relaxed version
          - efficient/packed version
          - bad-weather backup
          - weather switch points: if rain/heavy heat, swap X for Y, framed as
            seasonal risk unless exact forecast evidence covers the trip dates
          - rough daily budget notes as ranges and flex levers, not false
            precision
          - specific checks before booking, including opening-hours checks,
            timed-entry reservations, and transit-pass choice
          - mark specific restaurants, opening hours, and seasonal events as
            "verify before booking" unless they came from explicit search
            evidence in this run
          - omit artifact generation suggestions unless the user explicitly
            asked for an artifact or file

          If search or weather evidence is thin, state assumptions plainly
          instead of inventing sources. Include map/search links only as
          plain URLs when useful. Use the words Evidence, Source notes,
          Reference checks, Next steps, Verify, HTML, and Report
          only where they fit naturally in the final sections.

          Itinerary:
          {{ outputs.itinerary | truncate(7000) }}

          Constraint notes:
          {{ outputs.constraints | truncate(2500) }}

          Weather evidence:
          {{ outputs.weather | truncate(1600) }}

          POI/source notes:
          {{ outputs.poi | truncate(2000) }}
---

# Travel Planner (Meta-Skill)

Weather + POI/restaurant/transport search + constraints + a complete itinerary
with variants. The default answer is a complete travel plan; HTML export is an
optional handoff when the user explicitly asks for a file.

## Fallback

Manually call weather, multi-search-engine, summarize. If the user explicitly
asks for HTML export, ask the LLM to write a styled `travel-itinerary.html`
and `publish_artifact` it.
