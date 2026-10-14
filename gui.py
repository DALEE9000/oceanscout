"""oceanscout GUI. Stdlib only.

  python gui.py [port] [--no-browser]    serves http://127.0.0.1:8765 and opens it

Runs ocean.py commands as subprocesses (one at a time) and streams their output to the page.
Reads the same ocean.db, answers/, digests/ and taste.md as the CLI. Binds to localhost only.
"""
import datetime as dt, importlib, json, pathlib, re, sqlite3, subprocess, sys, threading, time, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ocean

HERE = ocean.HERE
KINDS = ("answers", "digests")
job = {"id": 0, "cmd": "", "arg": "", "status": "idle", "log": [], "started": 0.0, "ended": 0.0, "output": None}
job_lock = threading.Lock()  # ponytail: one job at a time; the model CLI is the bottleneck anyway


# ---------------------------------------------------------------- data
def list_files(kind):
    if kind not in KINDS:
        raise ValueError("kind must be answers or digests")
    d = HERE / kind
    out = []
    for f in sorted(d.glob("*.md"), key=lambda f: f.stat().st_mtime, reverse=True) if d.exists() else []:
        txt = f.read_text(encoding="utf-8", errors="replace")
        first = next((l for l in txt.splitlines() if l.strip()), f.name)
        out.append(dict(name=f.name, date=f.name[:10], title=first.lstrip("# ").strip(), words=len(txt.split()),
                        modified=dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes")))
    return out


def read_file(kind, name):
    if kind not in KINDS or pathlib.Path(name).name != name or not name.endswith(".md"):  # basename only: no path tricks
        raise ValueError("bad file")
    f = HERE / kind / name
    if not f.is_file():
        raise FileNotFoundError(name)
    return f.read_text(encoding="utf-8", errors="replace")


def rows(sql, *params):
    c = ocean.db()
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(sql, params)]


def with_hits(items):
    seen, out = set(), []
    for r in items:  # the same preprint arrives from arXiv and from OpenAlex: keep the first (better-ranked) copy
        key = re.sub(r"[^a-z0-9]", "", r["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        text = (r["title"] + " " + (r["abstract"] or "")).lower()
        r["hits"] = [t for t in ocean.TRACK if t in text]
        r["abstract"] = (r["abstract"] or "")[:1500]
        out.append(r)
    return out


def search(q, source="", k=50):
    if not q.strip():
        return []
    sql = ("SELECT i.* FROM fts JOIN items i ON i.rowid=fts.rowid WHERE fts MATCH ?"
           + (" AND i.source=?" if source else "") + " ORDER BY bm25(fts) LIMIT ?")
    return with_hits(rows(sql, ocean.fts_query(q), *([source] if source else []), min(k, 200)))


def recent(days=7, source=""):
    since = (dt.date.today() - dt.timedelta(days=max(1, min(days, 365)))).isoformat()
    sql = "SELECT * FROM items WHERE source!='notes' AND date>=?" + (" AND source=?" if source else "")
    return with_hits(ocean.taste_rank(rows(sql, since, *([source] if source else [])), 400))


def stats():
    c = ocean.db()
    by = dict(c.execute("SELECT source,count(*) FROM items GROUP BY source").fetchall())
    last = c.execute("SELECT max(fetched) FROM items").fetchone()[0]
    return dict(sources=by, total=sum(by.values()), last_pull=last,
                answers=len(list_files("answers")), digests=len(list_files("digests")))


# ---------------------------------------------------------------- jobs
def job_view(_=None):
    now = job["ended"] or time.time()
    return {**job, "log": job["log"][-40:], "log_len": len(job["log"]),
            "elapsed": round(now - job["started"]) if job["started"] else 0}


def start_job(cmd, arg):
    if cmd not in ("ask", "digest", "pull"):
        raise ValueError("cmd must be ask, digest or pull")
    if cmd == "ask" and not arg.strip():
        raise ValueError("empty question")
    if cmd != "ask" and arg and not (arg.isascii() and arg.isdigit()):  # isdigit() alone accepts "²", int() does not
        raise ValueError("days must be a whole number")
    if not job_lock.acquire(blocking=False):
        raise RuntimeError("a job is already running")
    job.update(id=job["id"] + 1, cmd=cmd, arg=arg, status="running", log=[], started=time.time(), ended=0.0, output=None)
    threading.Thread(target=run_job, args=(cmd, arg), daemon=True).start()
    return job_view()


def run_job(cmd, arg):
    argv = [sys.executable, "-u", str(HERE / "ocean.py"), cmd] + ([arg] if cmd == "ask" else ["--days", arg] if arg else [])
    try:
        p = subprocess.Popen(argv, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in p.stdout:
            job["log"].append(line.rstrip())
            m = re.match(r"saved (.+\.md)\s*$", line)
            if m:
                pth = pathlib.Path(m.group(1))
                job["output"] = dict(kind=pth.parent.name, name=pth.name)
        p.wait()
        job["status"] = "done" if p.returncode == 0 else "error"
    except Exception as e:
        job["log"].append(repr(e))
        job["status"] = "error"
    finally:
        job["ended"] = time.time()
        job_lock.release()


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def local(self):
        """Reject DNS-rebinding hosts; browsers always send Host."""
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def do_GET(self):
        if not self.local():
            return self.send_json({"error": "forbidden"}, 403)
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/":
                self.send((HERE / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/stats":
                self.send_json(stats())
            elif u.path == "/api/files":
                self.send_json(list_files(q.get("kind", "answers")))
            elif u.path == "/api/file":
                kind, name = q.get("kind", ""), q.get("name", "")
                text = read_file(kind, name)
                j = HERE / kind / (pathlib.Path(name).stem + ".json")  # structured brief next to its markdown, if any
                self.send_json({"text": text, "data": json.loads(j.read_text(encoding="utf-8")) if j.is_file() else None})
            elif u.path == "/api/search":
                self.send_json(search(q.get("q", ""), q.get("source", ""), int(q.get("limit", 50))))
            elif u.path == "/api/recent":
                self.send_json(recent(int(q.get("days", 7)), q.get("source", "")))
            elif u.path == "/api/taste":
                self.send_json({"text": (HERE / "taste.md").read_text(encoding="utf-8"), "track": ocean.TRACK, "ignore": ocean.IGNORE})
            elif u.path == "/api/job":
                self.send_json(job_view())
            else:
                self.send_json({"error": "not found"}, 404)
        except (KeyError, ValueError) as e:
            self.send_json({"error": str(e) or type(e).__name__}, 400)
        except FileNotFoundError as e:
            self.send_json({"error": f"not found: {e}"}, 404)
        except Exception as e:
            self.send_json({"error": repr(e)}, 500)

    def do_POST(self):
        # custom header forces a CORS preflight, which we never answer: a web page in another tab cannot trigger runs
        if not self.local() or self.headers.get("X-Oceanscout") != "1":
            return self.send_json({"error": "forbidden"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/run":
                self.send_json(start_job(str(d.get("cmd", "")), str(d.get("arg", "")).strip()))
            elif self.path == "/api/taste":
                (HERE / "taste.md").write_text(str(d["text"]), encoding="utf-8")
                importlib.reload(ocean)  # pick up new Track/Ignore lists for the firehose ranking
                self.send_json({"ok": True, "track": ocean.TRACK, "ignore": ocean.IGNORE})
            else:
                self.send_json({"error": "not found"}, 404)
        except (KeyError, ValueError, RuntimeError) as e:
            self.send_json({"error": str(e) or type(e).__name__}, 400)
        except Exception as e:
            self.send_json({"error": repr(e)}, 500)


if __name__ == "__main__":
    port = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8765)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"oceanscout GUI at {url}  (Ctrl+C to stop)", flush=True)
    if "--no-browser" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
