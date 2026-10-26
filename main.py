import asyncio, itertools, json, re, time
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import db, agents

app = FastAPI(title="Parley")
db.init()

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
