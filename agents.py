"""Each person gets an LLM-backed agent. Agents talk to each other turn by turn."""
import asyncio, json, os, re, time
from google import genai
from google.genai import types
import db

client = genai.Client()   # reads GEMINI_API_KEY from the environment
SMART = os.getenv("SMART_MODEL", "gemini-2.5-pro")    # profile reading + assessments
FAST = os.getenv("FAST_MODEL", "gemini-2.5-flash")    # dialogue turns (check Google's docs for current names)
sem = asyncio.Semaphore(int(os.getenv("CONCURRENCY", "8")))
ROUNDS = int(os.getenv("ROUNDS", "3"))                   # turns per agent
MIN_GAP = float(os.getenv("MIN_GAP", "0"))               # seconds between API calls (free tier: use 7)
_gap, _last = asyncio.Lock(), [0.0]

async def llm(model, system, messages, max_tokens=600, as_json=False):
    # Gemini uses roles "user"/"model"; merge consecutive same-role turns.
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        if contents and contents[-1].role == role:
            contents[-1].parts[0].text += "\n" + m["content"]
        else:
            contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))
    cfg = dict(system_instruction=system, max_output_tokens=max_tokens + 2000)  # headroom for thinking tokens
    if as_json:
        cfg["response_mime_type"] = "application/json"
    if "flash" in model:
        cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    for attempt in range(6):
        try:
            async with sem:
                async with _gap:   # space calls out to respect requests-per-minute limits
                    wait = _last[0] + MIN_GAP - time.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    _last[0] = time.time()
                r = await client.aio.models.generate_content(model=model, contents=contents,
                                                             config=types.GenerateContentConfig(**cfg))
            return (r.text or "").strip()
        except Exception as e:
            msg = str(e)
            hard = "PerDay" in msg or "limit: 0" in msg   # daily/zero quota: waiting will not help
            if "429" in msg and not hard and attempt < 5:
                await asyncio.sleep(20 * (attempt + 1))
                continue
            raise

def parse_json(t):
    return json.loads(re.search(r"\{.*\}", t, re.S).group(0))

READ_SYSTEM = """You analyze one person from two public sources only: LinkedIn text and Instagram text.
Return ONLY JSON:
{"interests":[{"name":str,"confidence":"high|medium|low","evidence":[{"source":"linkedin|instagram","quote":str}]}],
 "hobbies":[str],"lifestyle":{"rhythm":str,"social":str},"relationship_goal":str,
 "communication_style":str,"needs":[str],"unknown":[str]}
Rules: every claim needs a verbatim quote from the sources. If a field is not supported, set it to "Unknown"
and list it in "unknown". Never invent traits. 'high' confidence needs support from both sources."""

def verify_evidence(profile, li, ig):
    """Drop any quote that is not literally in the source text, so evidence cannot be fabricated."""
    src = {"linkedin": li or "", "instagram": ig or ""}
    for it in profile.get("interests", []):
        it["evidence"] = [e for e in it.get("evidence", []) if e.get("quote", "") in src.get(e.get("source"), "")]
        if not it["evidence"]:
            it["confidence"] = "low"
    profile["interests"] = [i for i in profile.get("interests", []) if i["evidence"]]
    return profile

async def read_person(p):
    text = f"LINKEDIN:\n{p['li_text'] or '(unavailable)'}\n\nINSTAGRAM:\n{p['ig_text'] or '(unavailable)'}"
    out = parse_json(await llm(SMART, READ_SYSTEM, [{"role": "user", "content": text}], 1500, True))
    return verify_evidence(out, p["li_text"], p["ig_text"])

def public_card(p):
    pr = json.loads(p["profile"])
    return {"name": p["name"], "interests": [i["name"] for i in pr["interests"][:4]]}

def agent_system(p, notes):
    return f"""You are the dating agent for {p['name']}. You are an AI acting on their behalf, never the person.
Their profile, from public sources only: {p['profile']}
Your memory of earlier dates: {notes or 'none'}
Rules: use only facts in the profile; say 'I don't know' for anything Unknown. Ask follow-up questions about
what the other agent says. Disagree honestly when routines, goals or interests clash. 1-3 sentences per turn."""

def history_for(me, other, log, opening):
    msgs = [{"role": "user", "content": f"You are meeting the agent for {other['name']}. Their public card: "
             f"{json.dumps(public_card(other))}. {opening}"}]
    for who, text in log:
        msgs.append({"role": "assistant" if who == me["id"] else "user", "content": text})
    return msgs

ASSESS = """You are {name}'s dating agent. Using only the transcript and your person's own profile, return ONLY JSON:
{{"score":0-100,"wants_second_date":bool,"shared":[str],"friction":[str],"unknown":[str],"summary":str}}
Score from what was actually discussed and your person's stated needs. Unknowns lower confidence, not score."""

async def run_date(a, b, notes_a="", notes_b=""):
    log, sys_a, sys_b = [], agent_system(a, notes_a), agent_system(b, notes_b)
    for _ in range(ROUNDS):
        log.append((a["id"], await llm(FAST, sys_a, history_for(a, b, log, "Open or continue the date."), 250)))
        log.append((b["id"], await llm(FAST, sys_b, history_for(b, a, log, "Respond to the other agent."), 250)))
    transcript = "\n".join(f"{a['name'] if w == a['id'] else b['name']}'s agent: {t}" for w, t in log)
    async def assess(me):
        r = await llm(SMART, ASSESS.format(name=me["name"]) + f"\nProfile: {me['profile']}",
                      [{"role": "user", "content": transcript}], 700, True)
        return parse_json(r)
    aa, ab = await asyncio.gather(assess(a), assess(b))
    s1, s2 = aa["score"], ab["score"]
    mutual = round(2 * s1 * s2 / max(1, s1 + s2))   # harmonic mean punishes one-sided interest
    outcome = ("both want a second date" if aa["wants_second_date"] and ab["wants_second_date"]
               else "one-sided" if aa["wants_second_date"] or ab["wants_second_date"] else "no second date")
    return {"transcript": transcript, "aa": aa, "ab": ab, "mutual": mutual, "outcome": outcome}

def notes(pid, n=5):
    rows = db.q("select note from memory where person_id=? order by id desc limit ?", (pid, n))
    return "\n".join(r["note"] for r in rows)

async def date_pair(a, b, run_id):
    r = await run_date(a, b, notes(a["id"]), notes(b["id"]))
    did = db.x("insert into dates(run_id,a,b,transcript,assess_a,assess_b,mutual,outcome) values(?,?,?,?,?,?,?,?)",
               (run_id, a["id"], b["id"], r["transcript"], json.dumps(r["aa"]), json.dumps(r["ab"]), r["mutual"], r["outcome"]))
    db.x("insert into memory(person_id,note) values(?,?)", (a["id"], f"Met {b['name']}'s agent: {r['aa']['summary']}"))
    db.x("insert into memory(person_id,note) values(?,?)", (b["id"], f"Met {a['name']}'s agent: {r['ab']['summary']}"))
    return did

def rankings(pid):
    out = []
    for d in db.q("select * from dates where a=? or b=?", (pid, pid)):
        me_a = d["a"] == pid
        mine, theirs = json.loads(d["assess_a" if me_a else "assess_b"]), json.loads(d["assess_b" if me_a else "assess_a"])
        out.append({"other_id": d["b"] if me_a else d["a"], "mutual": d["mutual"], "my_view": mine["score"],
                    "their_view": theirs["score"], "shared": mine["shared"], "friction": mine["friction"],
                    "unknown": mine["unknown"], "outcome": d["outcome"], "date_id": d["id"]})
    return sorted(out, key=lambda r: -r["mutual"])
