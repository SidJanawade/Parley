# Parley

Each person is represented by an LLM agent. The agents read two public sources (LinkedIn, Instagram),
build an evidence-backed profile, hold multi-turn dates with every other agent, and rank best fits.

## How it works
1. **Intake** (`POST /people`): validates both URLs, blocks duplicates, requires source text.
2. **Profile reading** (`agents.read_person`): Gemini Pro returns structured JSON. Every claim needs a
   verbatim quote; quotes not found in the source are dropped. Unsupported fields stay `Unknown`.
3. **Agents**: one persona per person, with its own profile and a memory of earlier dates (SQLite table).
4. **Dates** (`agents.run_date`): two agents alternate turns. Each sees only its own profile plus the other's
   public card, so facts must come out in conversation. Gemini Flash writes turns.
5. **Assessment**: each agent scores the date from its own person's needs. Mutual score = harmonic mean.
6. **Rankings** (`GET /people/{id}/rankings`): mutual score, both views, shared interests, friction, unknowns.

`POST /runs` runs the whole pipeline in the background; `GET /runs/{id}` reports real per-stage status,
failures and timings. `mcp_server.py` exposes `get_profile`, `get_rankings`, `run_date` over MCP.

## Run
    pip install -r requirements.txt
    export GEMINI_API_KEY=...
    uvicorn main:app --reload        # API
    python mcp_server.py             # MCP server

## Data sources and consent
Source text must come from an authorized path: the participant connects their own accounts through a
connector platform (Composio, Nango) or exports their own data. No scraping. Only consenting adults
should be added. Transcripts are simulations; they are not things the real people said, and the
people have not agreed to a date.

## Cost note
All pairs for 25 people is 300 dates x (6 turns + 2 assessments). Set `ROUNDS`, `CONCURRENCY`,
`FAST_MODEL`, `SMART_MODEL` via environment variables.
