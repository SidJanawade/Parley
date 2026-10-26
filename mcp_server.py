"""Exposes the same capabilities as MCP tools so any MCP client (or another agent) can call them."""
import json
from mcp.server.fastmcp import FastMCP
import db, agents

db.init()
mcp = FastMCP("parley")

@mcp.tool()
def get_profile(person_id: int) -> dict:
    """Evidence-backed profile of one person."""
    r = db.q("select name,profile from people where id=?", (person_id,))
    return {"name": r[0]["name"], "profile": json.loads(r[0]["profile"] or "null")} if r else {"error": "not found"}

@mcp.tool()
def get_rankings(person_id: int) -> list:
    """Ranked matches with scores, shared interests, friction and date ids."""
    return agents.rankings(person_id)

@mcp.tool()
async def run_date(a_id: int, b_id: int) -> dict:
    """Run a fresh agent-to-agent date between two analyzed people."""
    a, b = (db.q("select * from people where id=?", (i,))[0] for i in (a_id, b_id))
    did = await agents.date_pair(a, b, run_id=0)
    return db.q("select transcript,mutual,outcome from dates where id=?", (did,))[0]

if __name__ == "__main__":
    mcp.run()
