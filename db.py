import sqlite3, os
DB = os.getenv("DB", "parley.db")

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init():
    with conn() as c:
        c.executescript("""
        create table if not exists people(id integer primary key, name text, li_url text unique,
            ig_url text unique, li_text text, ig_text text, profile text, status text);
        create table if not exists dates(id integer primary key, run_id integer, a integer, b integer,
            transcript text, assess_a text, assess_b text, mutual integer, outcome text);
        create table if not exists memory(id integer primary key, person_id integer, note text,
            ts text default current_timestamp);
        create table if not exists runs(id integer primary key, stages text default '{}',
            created text default current_timestamp);""")

def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]

def x(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).lastrowid
