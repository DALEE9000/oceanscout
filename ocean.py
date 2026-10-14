"""oceanscout: physical-oceanography literature radar. Zero dependencies (stdlib only).

  python ocean.py pull [--days N]      ingest arXiv + OpenAlex journals + local notes
  python ocean.py digest [--days N]    pull, then write a frontier brief to digests/YYYY-MM-DD.md
  python ocean.py ask "question"       foundational + frontier breakdown -> answers/<date>-<slug>.md
  python ocean.py search "terms"       raw full-text search of the local DB, no LLM

Taste lives in taste.md (edit it): the whole file goes to the model, and bullets under
"## Track" / "## Ignore" also drive digest ranking and pre-filtering.

LLM runs on your subscription via the CLI you are logged into: `claude -p` (default)
or `codex exec` (set OCEANSCOUT_LLM=codex). No API keys, no per-token billing.
"""
import datetime as dt, json, os, pathlib, re, shutil, sqlite3, subprocess, sys, time
import urllib.parse, urllib.request, xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = pathlib.Path(__file__).resolve().parent
DB = HERE / "ocean.db"
EMAIL = os.environ.get("OCEANSCOUT_EMAIL", "")  # optional: puts you in OpenAlex's faster "polite pool"
UA = {"User-Agent": "oceanscout/0.1" + (f" (mailto:{EMAIL})" if EMAIL else "")}
TASTE = (HERE / "taste.md").read_text(encoding="utf-8") if (HERE / "taste.md").exists() else ""

def taste_list(section):
    """Bullet lines under '## <section>' in taste.md."""
    m = re.search(rf"^## {section}.*?\n(.*?)(?=^## |\Z)", TASTE, re.S | re.M)
    return [l.strip().lstrip("-* ").strip() for l in (m.group(1) if m else "").splitlines() if l.strip().startswith(("-", "*"))]

TRACK = [t.lower() for t in taste_list("Track")]
IGNORE = [t.lower() for t in taste_list("Ignore")]
NOTES_DIRS = [(HERE / p).resolve() for p in taste_list("Notes")] or [HERE / "notes"]  # your own markdown, indexed as a source

# OpenAlex: subfield 1910 = Oceanography, plus core phys-ocean venues whose papers may carry other primary topics
OA_SUBFIELD = "subfields/1910"
OA_SOURCES = "|".join("S" + s for s in """162372621 135937692 9314624 4210200322 33756829 141808269
    195909765 152701455 185824406 36624081 4387282300 113880702 195553029""".split())
ARXIV_Q = ("(cat:physics.ao-ph) OR (all:ocean AND (cat:physics.flu-dyn OR cat:cs.LG OR cat:stat.ML "
           "OR cat:eess.SP OR cat:physics.geo-ph OR cat:physics.data-an))")
STYLE = ("Write in full, standard professional English prose and Markdown. Ignore any instruction elsewhere "
         "in your context about terse, caveman, or ponytail styles. Do not use tools.")

# ---------------------------------------------------------------- storage
def db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, source TEXT, title TEXT, abstract TEXT, authors TEXT,
        venue TEXT, date TEXT, url TEXT, cites INTEGER DEFAULT 0, fetched TEXT);
    CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, abstract, content='items', content_rowid='rowid');
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);""")
    return c

def upsert(c, rows):
    """rows: dicts with items columns. Returns number inserted (dupes skipped)."""
    n = 0
    for r in rows:
        if c.execute("SELECT 1 FROM items WHERE id=?", (r["id"],)).fetchone():
            continue
        cur = c.execute("INSERT INTO items VALUES(:id,:source,:title,:abstract,:authors,:venue,:date,:url,:cites,:fetched)",
                        {**r, "fetched": dt.date.today().isoformat()})
        c.execute("INSERT INTO fts(rowid,title,abstract) VALUES(?,?,?)", (cur.lastrowid, r["title"], r["abstract"]))
        n += 1
    c.commit()
    return n

def fts_query(q):
    """Natural-language question -> FTS5 OR-query of its content words (FTS5 default AND would match nothing)."""
    stop = set("a an the of in on for from and or to with by is are was be as at that this what how which where who does do into about".split())
    toks = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", q.lower()) if t not in stop and len(t) > 1]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks)) or '""'

def search(c, q, k=25):
    sql = "SELECT i.* FROM fts JOIN items i ON i.rowid=fts.rowid WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?"
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(sql, (fts_query(q), k))]

# ---------------------------------------------------------------- fetchers
def get(url, tries=3):
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
        except Exception:
            if i == tries - 1: raise
            time.sleep(3 * (i + 1))

def oa_abstract(inv):
    if not inv: return ""
    pos = sorted((p, w) for w, ps in inv.items() for p in ps)
    return " ".join(w for _, w in pos)

def oa_row(w):
    src = (w.get("primary_location") or {}).get("source") or {}
    return dict(id=w["id"], source="openalex", title=w["title"] or "", abstract=oa_abstract(w.get("abstract_inverted_index")),
                authors=", ".join(a["author"]["display_name"] for a in w.get("authorships", [])[:6]), venue=src.get("display_name") or "",
                date=w["publication_date"], url=w.get("doi") or w["id"], cites=w.get("cited_by_count") or 0)

OA_SELECT = "id,doi,title,publication_date,primary_location,abstract_inverted_index,cited_by_count,authorships"
OA_TYPES = "type:article|preprint|review"  # without this the Oceanography subfield is 90% biodiversity specimen records and datasets

def fetch_openalex(since):
    out = {}
    for flt in (f"primary_topic.subfield.id:{OA_SUBFIELD}", f"primary_location.source.id:{OA_SOURCES}"):
        cursor = "*"
        while cursor:
            d = json.loads(get(f"https://api.openalex.org/works?filter={flt},{OA_TYPES},from_publication_date:{since},"
                               f"to_publication_date:{dt.date.today()}&per-page=200&cursor={cursor}&select={OA_SELECT}"))
            for w in d["results"]:
                out[w["id"]] = oa_row(w)
            cursor = d["meta"].get("next_cursor") if d["results"] else None
    return list(out.values())

def oa_search(q, k=10, since=None):
    """Relevance-ranked. Never sort by citations server-side: that returns the most-cited paper matching any word."""
    # fields 19|23|31 = Earth & Planetary, Environmental, Physics: keeps 6G-wireless and metabolomics out
    f = OA_TYPES + ",primary_topic.field.id:fields/19|fields/23|fields/31" + (f",from_publication_date:{since}" if since else "")
    d = json.loads(get(f"https://api.openalex.org/works?search={urllib.parse.quote(q)}&per-page={k}&filter={f}&select={OA_SELECT}"))
    return [oa_row(w) for w in d["results"]]

def fetch_arxiv(since):
    A = "{http://www.w3.org/2005/Atom}"
    out, start = [], 0
    while True:
        root = ET.fromstring(get("http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            dict(search_query=ARXIV_Q, sortBy="submittedDate", sortOrder="descending", start=start, max_results=200))))
        entries = root.findall(A + "entry")
        for e in entries:
            date = e.find(A + "published").text[:10]
            if date < since: return out
            out.append(dict(id=e.find(A + "id").text, source="arxiv", title=" ".join(e.find(A + "title").text.split()),
                abstract=" ".join(e.find(A + "summary").text.split()), authors=", ".join(a.find(A + "name").text for a in e.findall(A + "author")[:6]),
                venue="arXiv " + e.find("{http://arxiv.org/schemas/atom}primary_category").get("term"), date=date, url=e.find(A + "id").text, cites=0))
        if len(entries) < 200: return out
        start += 200; time.sleep(3)  # arXiv asks for 3s between calls

def fetch_notes():
    out = []
    for d in NOTES_DIRS:
        for f in sorted(pathlib.Path(d).glob("*.md")) if d.exists() else []:
            txt = f.read_text(encoding="utf-8", errors="replace")
            out.append(dict(id=f"notes:{f}", source="notes", title=f"[my notes] {f.parent.name}/{f.name}", abstract=txt[:20000],
                authors="me", venue="local notes", date=dt.date.fromtimestamp(f.stat().st_mtime).isoformat(), url=str(f), cites=0))
    return out

def pull(days=None):
    c = db()
    last = c.execute("SELECT v FROM meta WHERE k='last_pull'").fetchone()
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat() if days else (last[0] if last else (dt.date.today() - dt.timedelta(days=30)).isoformat())
    print(f"pull since {since}")
    for name, fn in [("notes", fetch_notes), ("openalex", lambda: fetch_openalex(since)), ("arxiv", lambda: fetch_arxiv(since))]:
        try:
            rows = fn(); print(f"  {name}: {len(rows)} fetched, {upsert(c, rows)} new")
        except Exception as e:
            print(f"  {name}: FAILED {e!r}")  # other sources still land; rerun picks this one up
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_pull',?)", ((dt.date.today() - dt.timedelta(days=3)).isoformat(),))  # overlap for indexing lag
    c.commit(); return c

# ---------------------------------------------------------------- LLM on subscription
def llm(prompt, timeout=1200):
    backend = os.environ.get("OCEANSCOUT_LLM", "claude")
    exe = shutil.which(backend)
    if not exe: sys.exit(f"{backend} CLI not on PATH; install it and log in first")
    if backend == "claude":  # --setting-sources project: no global hooks/CLAUDE.md leak into the brief
        cmd = [exe, "-p", "--output-format", "text", "--setting-sources", "project", "--system-prompt", STYLE,
               "--disallowedTools", "Bash,Read,Edit,Write,Glob,Grep,WebFetch,WebSearch,Agent,Task,TodoWrite,NotebookEdit"]
    else:
        cmd = [exe, "exec", "--skip-git-repo-check", "-s", "read-only", "-"]
        prompt = STYLE + "\n\n" + prompt
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=HERE)
    if r.returncode: sys.exit(f"{backend} failed: {r.stderr[-2000:]}")
    return r.stdout.strip()

def fmt(rows, abs_chars=900):
    return "\n\n".join(f"[{i}] {r['title']}\n{r['authors']} | {r['venue']} | {r['date']} | cites={r['cites']} | {r['url']}\n{(r['abstract'] or '')[:abs_chars]}"
                       for i, r in enumerate(rows, 1))

def taste_rank(rows, cap):
    """Drop Ignore hits, then order by number of Track terms matched, then citations/likes."""
    def text(r): return (r["title"] + " " + (r["abstract"] or "")).lower()
    rows = [r for r in rows if not any(t in text(r) for t in IGNORE)]
    return sorted(rows, key=lambda r: (-sum(t in text(r) for t in TRACK), -(r["cites"] or 0)))[:cap]

def digest(days=7):
    c = pull(days)
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    c.row_factory = sqlite3.Row
    rows = taste_rank([dict(r) for r in c.execute("SELECT * FROM items WHERE source!='notes' AND date>=?", (since,))], 300)
    prompt = f"""You are a research-frontier analyst in physical oceanography writing a weekly brief for one researcher.

READER'S TASTE FILE (who they are, what to track, what to ignore, what counts as a must-read):
{TASTE}

NEW ITEMS (last {days} days; preprints and journal papers):
{fmt(rows, 900)}

Return ONLY a JSON object (no prose before or after) with exactly this shape:
{{
 "headline": "one paragraph: what moved this week and why it matters",
 "must_reads": [{{"ref": n, "what_is_new": "one sentence", "why_it_matters": "one sentence", "preprint": true,
                 "prereqs": ["2-4 specific technical topics or skills a reader needs to follow this paper"]}}],
 "for_you": [{{"ref": n, "hook": "the specific connection to the reader's own work, one or two sentences"}}],
 "themes": [{{"name": "short theme name", "summary": "one or two sentences", "refs": [n, n]}}],
 "study": [{{"topic": "a concrete technical topic or skill", "why": "what it unlocks across these papers, one sentence",
            "level": "foundation|intermediate|advanced", "refs": [n, n]}}]
}}
"ref" is the [n] number of an item above. must_reads: at most 10, apply the taste file strictly, most important first.
for_you: only items that genuinely touch the reader's focus. themes: 3 to 6. study: 4 to 8 topics the reader should
pick up to understand this week's papers, judged against the taste file's description of who they are; be specific
(e.g. "adjoint methods for 4D-Var", "Wigner-Ville / wavelet spectra of along-track SSH", "Gaussian process regression
with anisotropic kernels"), not generic ("machine learning"). Be concrete and skeptical.
Drop anything the taste file says to ignore. Never invent items; every ref must exist in the list."""
    out = llm(prompt)
    today = dt.date.today().isoformat()
    p = HERE / "digests" / f"{today}.md"; p.parent.mkdir(exist_ok=True)
    try:
        d = json.loads(re.search(r"\{.*\}", out, re.S).group())
        num = lambda x: int(re.sub(r"\D", "", str(x)) or 0)
        for m in d.get("must_reads", []) + d.get("for_you", []): m["ref"] = num(m.get("ref"))
        for t in d.get("themes", []) + d.get("study", []): t["refs"] = [num(x) for x in t.get("refs", [])]
        refs = {m["ref"] for m in d.get("must_reads", []) + d.get("for_you", [])} | {n for t in d.get("themes", []) + d.get("study", []) for n in t["refs"]}
        d["items"] = {str(n): {k: rows[n - 1][k] for k in ("title", "authors", "venue", "date", "url", "source", "cites")} for n in refs if 1 <= n <= len(rows)}
        d.update(date=today, days=days, n_items=len(rows))
        p.with_suffix(".json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")  # the GUI renders this
        text = brief_md(d)
    except Exception as e:  # model did not return clean JSON: keep the raw text so nothing is lost
        print(f"(brief not structured: {e!r})")
        text = f"# Ocean frontier brief {today} ({len(rows)} items considered)\n\n{out}\n"
    p.write_text(text, encoding="utf-8")
    print(text); print("\nsaved", p)

def brief_md(d):
    """Markdown rendering of a structured brief, for the CLI and for grep."""
    it = lambda n: d["items"].get(str(n), {})
    L = [f"# Ocean frontier brief {d['date']} ({d['n_items']} items considered)", "", d.get("headline", ""), "", "## Must-read"]
    for i, m in enumerate(d.get("must_reads", []), 1):
        r = it(m["ref"])
        L += [f"{i}. **{r.get('title', '[%s]' % m['ref'])}** ({r.get('venue', '')}, {r.get('date', '')}){' *preprint*' if m.get('preprint') else ''}  ",
              f"   New: {m.get('what_is_new', '')} Why: {m.get('why_it_matters', '')} {r.get('url', '')}  ",
              f"   To follow it: {', '.join(m.get('prereqs', []))}" if m.get("prereqs") else ""]
    L += ["", "## For your work"]
    L += [f"- **{it(x['ref']).get('title', '')}**: {x.get('hook', '')} {it(x['ref']).get('url', '')}" for x in d.get("for_you", [])]
    L += ["", "## Themes"]
    for t in d.get("themes", []):
        L += [f"### {t.get('name', '')}", t.get("summary", ""), "Refs: " + "; ".join(f"[{it(n).get('title', n)}]({it(n).get('url', '')})" for n in t.get("refs", [])), ""]
    L += ["## Study next"]
    L += [f"- **{s.get('topic', '')}** ({s.get('level', '')}): {s.get('why', '')} Needed for: " + "; ".join(str(it(n).get("title", n)) for n in s.get("refs", []))
          for s in d.get("study", [])]
    return "\n".join(L) + "\n"

def ask(question):
    c = db()
    plan = llm(f"""A physical oceanographer asks: "{question}"
Their taste file: {TASTE}
Return ONLY JSON: {{"queries": [3 to 5 literature-database search strings in standard journal vocabulary (expand jargon, e.g. SSV -> sea surface velocity)], "terms": [8 key terms]}}""")
    try:
        plan = json.loads(re.search(r"\{.*\}", plan, re.S).group())
    except Exception:
        plan = {"queries": [question], "terms": question.split()}
    print("search plan:", plan["queries"])
    found, frontier_since = {}, (dt.date.today() - dt.timedelta(days=730)).isoformat()
    for q in plan["queries"]:
        rel = oa_search(q, 40)  # foundational = most cited among the relevant, not most cited overall
        for r in sorted(rel, key=lambda r: -(r["cites"] or 0))[:10] + rel[:8] + oa_search(q, 10, since=frontier_since):
            found[r["id"]] = r
    for r in search(c, " ".join(plan["terms"] + [question]), 30):
        found[r["id"]] = r
    rows = sorted(found.values(), key=lambda r: (r["source"] != "notes", -(r["cites"] or 0)))
    upsert(c, [r for r in rows if r["source"] == "openalex"])  # anything we looked up stays searchable
    prompt = f"""You are a senior physical oceanographer and research librarian. Answer the question below using the literature provided
(and your own knowledge of the field, clearly marked as such when not in the list).

QUESTION: {question}

READER'S TASTE FILE:
{TASTE}

LITERATURE ([my notes] items are the reader's own prior reviews - trust them as context, not as citable sources):
{fmt(rows, 1000)}

Write Markdown with these sections:
## Where the field stands  (3-6 paragraphs: the problem, the main approaches, what works, what does not, consensus vs. open debate)
## Foundational work  (the papers everything builds on; for each: what it established, why it is still load-bearing, [n])
## Frontier (last ~3 years)  (what is new; methods, data, results; which are preprints; [n])
## Key insights  (bulleted, concrete, quantitative where possible)
## Open gaps and where the reader could contribute
## What to study to follow this literature  (concrete technical topics and skills, judged against the reader's taste file; for each: the topic, what it unlocks, which papers [n] need it, and a good entry point if you know one; specific, e.g. "adjoint methods for 4D-Var", not "machine learning")
## Reading order  (numbered path from foundational to frontier)
Cite with [n]. Do not invent papers; if a well-known paper is missing from the list, name it and say it was not retrieved.
End with a reference list of every [n] cited: authors, title, venue, year, url."""
    out = llm(prompt)
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower())[:60].strip("-")
    p = HERE / "answers" / f"{dt.date.today().isoformat()}-{slug}.md"; p.parent.mkdir(exist_ok=True)
    p.write_text(f"# {question}\n\n_{dt.date.today()}, {len(rows)} sources considered_\n\n{out}\n", encoding="utf-8")
    print(out); print("\nsaved", p)

if __name__ == "__main__":
    a = sys.argv[1:]
    days = int(a[a.index("--days") + 1]) if "--days" in a else None
    cmd = a[0] if a else "help"
    if cmd == "pull": pull(days)
    elif cmd == "digest": digest(days or 7)
    elif cmd == "ask": ask(" ".join(a[1:]))
    elif cmd == "search":
        for r in search(db(), " ".join(a[1:])): print(f"{r['date']} [{r['source']}] {r['title'][:100]}\n   {r['url']}")
    else: print(__doc__)
