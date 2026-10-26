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

## Run the web app

Install dependencies and set your Gemini API key:

**Windows PowerShell**
```powershell
python -m pip install -r requirements.txt
$env:GEMINI_API_KEY = "your-api-key"
python -m uvicorn main:app --reload
```

**macOS/Linux**
```sh
python -m pip install -r requirements.txt
export GEMINI_API_KEY="your-api-key"
python -m uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/> for the browser interface. It supports adding participants, starting a
matching run, and viewing profiles, rankings, and date details. The API documentation is at
<http://127.0.0.1:8000/docs>.

The MCP server can be started separately with `python mcp_server.py`. It also requires `GEMINI_API_KEY`
for operations that call Gemini.

Keep your API key private; do not commit it or include it in uploaded files.

## Optional LinkedIn account import

The form can import a participant's own LinkedIn OpenID Connect profile through Nango. This is OAuth,
not scraping: the account owner must authorize access. LinkedIn only returns fields allowed by the
configured product and scopes, so this may provide limited profile information rather than posts or
interests. Instagram text must still be supplied through a participant-authorized source.

1. Create a Nango account and configure a LinkedIn integration using LinkedIn OpenID Connect with the
   `openid` and `profile` scopes. Confirm the LinkedIn app and Nango integration are approved/configured.
2. Copy the integration's unique key and create an Nango environment API key with connect-session and
   proxy permissions.
3. Set these environment variables in Render (or your local shell):
   - `NANGO_SECRET_KEY`: the Nango environment API key.
   - `NANGO_LINKEDIN_INTEGRATION`: the integration's unique key from Nango.
4. On the participant form, enter their name and LinkedIn profile URL, click **Import my LinkedIn
   profile**, and have them authorize their own LinkedIn account. Review the imported text before adding
   the participant.

## Data sources and consent
Source text must come from an authorized path: the participant connects their own accounts through a
connector platform (Composio, Nango) or exports their own data. No scraping. Only consenting adults
should be added. Transcripts are simulations; they are not things the real people said, and the
people have not agreed to a date.

## Cost note
All pairs for 25 people is 300 dates x (6 turns + 2 assessments). Set `ROUNDS`, `CONCURRENCY`,
`FAST_MODEL`, `SMART_MODEL` via environment variables.
