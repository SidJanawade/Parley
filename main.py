import asyncio, itertools, json, re, time
import os, secrets, urllib.error, urllib.request
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import db, agents

app = FastAPI(title="Parley")
db.init()

NANGO_API = "https://api.nango.dev"

def nango_request(path, payload=None, headers=None):
    secret = os.getenv("NANGO_SECRET_KEY")
    if not secret:
        raise HTTPException(503, "Nango is not configured: set NANGO_SECRET_KEY")
    request_headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json"}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = urllib.request.Request(
        f"{NANGO_API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers=request_headers,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(1_000_001)
    except urllib.error.HTTPError as error:
        detail = error.read(2000).decode("utf-8", errors="replace")
        raise HTTPException(502, f"Nango request failed ({error.code}): {detail}") from error
    except urllib.error.URLError as error:
        raise HTTPException(502, f"Could not reach Nango: {error.reason}") from error
    if len(body) > 1_000_000:
        raise HTTPException(502, "Nango response exceeded the 1 MB limit")
    try:
        return json.loads(body)
    except json.JSONDecodeError as error:
        raise HTTPException(502, "Nango returned invalid JSON") from error

@app.get("/", include_in_schema=False)
def home():
    return FileResponse(Path(__file__).with_name("index.html"))

LI = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/([\w%\-]+)/?$", re.I)
IG = re.compile(r"^https?://(www\.)?instagram\.com/([\w.]+)/?$", re.I)

class PersonIn(BaseModel):
    name: str
    linkedin_url: str
    instagram_url: str
    linkedin_text: str = ""     # filled by a permitted connector or pasted by the participant
    instagram_text: str = ""

@app.post("/connect/linkedin/session")
def linkedin_session():
    integration = os.getenv("NANGO_LINKEDIN_INTEGRATION")
    if not integration:
        raise HTTPException(503, "Nango is not configured: set NANGO_LINKEDIN_INTEGRATION")
    connection_id = secrets.token_urlsafe(24)
    response = nango_request("/connect/sessions", {
        "allowed_integrations": [integration],
        "tags": {"end_user_id": connection_id},
    })
    session_data = response.get("data") if isinstance(response, dict) else None
    token = session_data.get("token") if isinstance(session_data, dict) else None
    if not token:
        raise HTTPException(502, "Nango did not return a connect-session token")
    return {"token": token, "integration": integration, "connection_id": connection_id}

@app.get("/connect/linkedin/profile/{connection_id}")
def linkedin_profile(connection_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", connection_id):
        raise HTTPException(400, "Invalid LinkedIn connection")
    integration = os.getenv("NANGO_LINKEDIN_INTEGRATION")
    if not integration:
        raise HTTPException(503, "Nango is not configured: set NANGO_LINKEDIN_INTEGRATION")
    profile = nango_request("/proxy/v2/userinfo", headers={
        "Connection-Id": connection_id,
        "Provider-Config-Key": integration,
    })
    if not isinstance(profile, dict) or not profile:
        raise HTTPException(502, "LinkedIn returned an empty or invalid profile")
    public_fields = {key: profile[key] for key in ("name", "given_name", "family_name", "locale") if key in profile}
    if not public_fields:
        raise HTTPException(502, "LinkedIn did not return any supported profile fields")
    return public_fields

@app.post("/people")
def add_person(p: PersonIn):
    li, ig = LI.match(p.linkedin_url), IG.match(p.instagram_url)
    if not li: raise HTTPException(422, "Not a LinkedIn profile URL")
    if not ig or ig.group(2).lower() in {"p", "reel", "explore", "accounts"}:
        raise HTTPException(422, "Not an Instagram profile URL")
    if not (p.linkedin_text or p.instagram_text):
        raise HTTPException(422, "No source text from either profile")
    lu, iu = f"linkedin.com/in/{li.group(2).lower()}", f"instagram.com/{ig.group(2).lower()}"
    if db.q("select id from people where li_url=? or ig_url=?", (lu, iu)):
        raise HTTPException(409, "Person already added")
    pid = db.x("insert into people(name,li_url,ig_url,li_text,ig_text,status) values(?,?,?,?,?,'pending')",
               (p.name, lu, iu, p.linkedin_text, p.instagram_text))
    return {"id": pid}

async def pipeline(run_id):
    st = {}
    def mark(stage, **v):
        st[stage] = v
        db.x("update runs set stages=? where id=?", (json.dumps(st), run_id))
    people = db.q("select * from people")
    t = time.time()
    usable = [p for p in people if p["li_text"] or p["ig_text"]]
    mark("extraction", status="done", usable=len(usable), total=len(people))
    t = time.time()
    results = await asyncio.gather(*[agents.read_person(p) for p in usable], return_exceptions=True)
    ready, failed = [], []
    for p, r in zip(usable, results):
        if isinstance(r, Exception):
            db.x("update people set status=? where id=?", (f"analysis failed: {r}", p["id"])); failed.append(p["id"])
        else:
            db.x("update people set profile=?, status='analyzed' where id=?", (json.dumps(r), p["id"]))
            p["profile"] = json.dumps(r); ready.append(p)
    mark("analysis", status="done" if not failed else "partial", ok=len(ready), failed=failed, secs=round(time.time() - t, 1))
    mark("agents", status="done", created=len(ready))
    t = time.time()
    pairs = list(itertools.combinations(ready, 2))
    res = await asyncio.gather(*[agents.date_pair(a, b, run_id) for a, b in pairs], return_exceptions=True)
    bad = sum(isinstance(r, Exception) for r in res)
    mark("dates", status="done" if not bad else "partial", ok=len(pairs) - bad, failed=bad, secs=round(time.time() - t, 1))
    mark("ranking", status="done", lists=len(ready))

@app.post("/runs")
def start_run(bg: BackgroundTasks):
    rid = db.x("insert into runs default values")
    bg.add_task(pipeline, rid)
    return {"run_id": rid}

@app.get("/runs/{rid}")
def run_status(rid: int):
    r = db.q("select * from runs where id=?", (rid,))
    if not r: raise HTTPException(404)
    return {"id": rid, "stages": json.loads(r[0]["stages"])}

@app.get("/people")
def people():
    return [{"id": p["id"], "name": p["name"], "status": p["status"]} for p in db.q("select * from people")]

@app.get("/people/{pid}")
def person(pid: int):
    r = db.q("select * from people where id=?", (pid,))
    if not r: raise HTTPException(404)
    p = r[0]
    return {**{k: p[k] for k in ("id", "name", "li_url", "ig_url", "status")}, "profile": json.loads(p["profile"] or "null")}

@app.get("/people/{pid}/rankings")
def person_rankings(pid: int):
    return agents.rankings(pid)

@app.get("/dates/{did}")
def date(did: int):
    r = db.q("select * from dates where id=?", (did,))
    if not r: raise HTTPException(404)
    return r[0]
