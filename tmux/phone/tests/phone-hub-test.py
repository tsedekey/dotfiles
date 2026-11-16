#!/usr/bin/env python3
"""Tests for phone-hub. Runs a real server on a random loopback port with fake agents. The TEST PIN below is a throwaway
value used only in a temporary config folder; it is not related to any real PIN."""
import http.client, json, os, re, shutil, socket, subprocess, sys, tempfile, time, stat

sys.dont_write_bytecode = True
HUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "phone-hub")
ROOT = tempfile.mkdtemp(prefix="phtest-", dir=os.environ.get("TMPDIR", "/tmp"))
CFGD, STATED, CAPD = f"{ROOT}/cfg", f"{ROOT}/state", f"{ROOT}/cap"
os.makedirs(CAPD)
HOST, LOGIN = "hub.example.ts.net", "me@example.com"      # fake: the hub has NO built-in identity, every config below sets these
TESTPIN = "483920"
S = socket.socket(); S.bind(("127.0.0.1", 0)); PORT = S.getsockname()[1]; S.close()
STATUS = f"{ROOT}/status.json"

TD2 = os.path.join(os.environ.get("TMPDIR", "/tmp").rstrip("/"), "phhub-tmux2")
PANEPATHS = f"{ROOT}/panepaths.json"
ENV = dict(os.environ, PHONE_HUB_CONFIG_DIR=CFGD, PHONE_HUB_STATE_DIR=STATED, PHONE_HUB_STATUS_JSON=STATUS, PHONE_HUB_FAKE_CAPTURE_DIR=CAPD,
           PHONE_HUB_FAKE_PANE_PATHS=PANEPATHS, TMUX_TMPDIR=TD2, STUB_MODE_FILE=f"{ROOT}/stub-mode", STUB_ARGS_LOG=f"{ROOT}/stub-args.log")
ENV.pop("TMUX", None); ENV.pop("TMUX_PANE", None)
passed = failed = 0
def ck(name, expected, actual):
    global passed, failed
    ok = expected == actual
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"   (expected {expected!r}, got {actual!r})"), flush=True)
    passed += ok; failed += (not ok)

def setcfg(**kw):
    p = f"{CFGD}/config.json"
    d = json.load(open(p)) if os.path.exists(p) else {}
    d.setdefault("login", LOGIN); d.setdefault("host", HOST)
    d.update(kw)
    os.makedirs(CFGD, exist_ok=True)
    json.dump(d, open(p, "w"))

def cli(*args, stdin=None, env=None):
    r = subprocess.run([sys.executable, HUB, *args], capture_output=True, text=True, input=stdin, env=env or ENV, timeout=60)
    return r.returncode, r.stdout + r.stderr

def write_status(agents):
    json.dump({"agents": agents}, open(STATUS, "w"))

def agent(pane, status="waiting", el=120, kind="claude", session="review", window="pr-1", title="A title"):
    return {"pane_id": pane, "status": status, "elapsed_secs": el, "agent_kind": kind, "session": session, "window_name": window, "title": title}

class Resp:
    def __init__(self, r, body): self.status = r.status; self.h = {k.lower(): v for k, v in r.getheaders()}; self.cookies = r.msg.get_all("Set-Cookie") or []; self.text = body.decode("utf-8", "replace"); self.raw = body

def req(method, path, *, host=HOST, login=LOGIN, cookies=None, body=None, ctype="application/x-www-form-urlencoded", extra=None, headers=None):
    h = {"Host": host}
    if login is not None: h["Tailscale-User-Login"] = login
    if cookies: h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if body is not None: h["Content-Type"] = ctype; h["Content-Length"] = str(len(body))
    h.update(extra or {}); h.update(headers or {})
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in h.items(): c.putheader(k, v)
    c.endheaders(body.encode() if isinstance(body, str) else body)
    r = c.getresponse(); out = Resp(r, r.read()); c.close()
    return out

def cookie_value(resp, name):
    for c in resp.cookies:
        if c.startswith(name + "="): return c.split(";")[0].split("=", 1)[1]
    return None

def cookie_line(resp, name):
    for c in resp.cookies:
        if c.startswith(name + "="): return c
    return ""

server = None
def start():
    global server
    server = subprocess.Popen([sys.executable, HUB, "serve"], env=ENV, stdout=open(f"{ROOT}/srv.log", "ab"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1); c.request("GET", "/healthz"); r = c.getresponse()
            if r.status == 200: c.close(); return
        except OSError: pass
        time.sleep(0.15)
    raise SystemExit("server did not start: " + open(f"{ROOT}/srv.log").read())
def stop():
    global server
    if server: server.terminate(); server.wait(5); server = None

def pair(label="iPhone"):
    rc, out = cli("pair")
    code = re.search(r"code:\s+(\S+)", out).group(1)
    r = req("POST", "/pair", body=f"code={code}&label={label}")
    return cookie_value(r, "ph_dev"), r, code

def login(dev, pin=TESTPIN):
    r = req("POST", "/login", cookies={"ph_dev": dev}, body=f"pin={pin}")
    return cookie_value(r, "ph_sess"), r

try:
    setcfg(port=PORT, host=HOST, login=LOGIN, lockout_secs=2, capture_max_chars=20000)
    write_status([agent("%127", "waiting", 300, "claude", "review", "pr-1", "Review of PR 1"),
                  agent("%128", "done", 100, "copilot", "change", "c-2", "✳ Change thing - GitHub Copilot"),
                  agent("%129", "working", 10, "opencode", "epic", "e-3", "Epic"),
                  agent("%130", "done", 200000, "claude", "daily", "d-4", "Old one"),
                  agent("%131", "waiting", 5, "claude", "xss", '"><img src=x onerror=alert(1)>', "<script>alert('t')</script>"),
                  {"pane_id": "bad;id", "status": "waiting", "elapsed_secs": 1}])
    open(f"{CAPD}/127.txt", "w").write("x" * 30000 + "\nline one\n\x1b[31mred text\x1b[0m\n<script>alert('pane')</script>\n\x07bell\x00nul\nlast line")
    open(f"{CAPD}/131.txt", "w").write("hello")
    open(f"{CAPD}/55.txt", "w").write("TOP-SECRET-FROM-A-NON-AGENT-PANE")
    start()

    print("== A network and identity layers fail closed =="); H = {}
    r = req("GET", "/healthz", host="evil.example", login=None); ck("healthz needs no headers", (200, b"ok"), (r.status, r.raw))
    ck("wrong Host header", 403, req("GET", "/", host="evil.example").status)
    ck("Host with other port is still the right host", True, req("GET", "/pair", host=HOST + ":443").status in (200, 303))
    ck("no identity header", 403, req("GET", "/", login=None).status)
    ck("wrong identity", 403, req("GET", "/", login="someone@else.com").status)
    r = req("GET", "/whoami", login=None); ck("whoami says MISSING, shows no secrets", (200, True), (r.status, "MISSING" in r.text))
    r = req("GET", "/whoami"); ck("whoami says matches", True, "present, matches" in r.text)
    r = req("GET", "/whoami", host="evil.example"); ck("whoami with a wrong Host still answers, and SAYS what arrived", (200, True, True), (r.status, "evil.example" in r.text, "NOT what the hub expects" in r.text))
    r = req("GET", "/whoami", extra={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"}); ck("whoami shows the whole forwarded chain and the entry it would use", (True, True), ("1.1.1.1, 2.2.2.2" in r.text, "would use: <b>2.2.2.2</b>" in r.text))
    r = req("GET", "/whoami", host="<b>x</b>.evil"); ck("whoami escapes a hostile Host value", (False, True), ("<b>x</b>" in r.text, "&lt;b&gt;x&lt;/b&gt;" in r.text))
    # no built-in identity: an empty `login` would equal a MISSING identity header, so an unconfigured hub must answer nothing
    setcfg(login="")
    r1 = req("GET", "/whoami", login=None); r2 = req("GET", "/", login=None); r3 = req("GET", "/whoami", login="")
    ck("login unset: a request with no identity header is refused (503), not treated as a match", (503, 503, 503), (r1.status, r2.status, r3.status))
    setcfg(login=LOGIN, host="")
    r1 = req("GET", "/whoami", host=""); r2 = req("GET", "/login", host="evil.example")
    ck("host unset: refused too", (503, 503), (r1.status, r2.status))
    setcfg(login="not-an-email", host=HOST)
    ck("a login that is not an address is refused as misconfigured", 503, req("GET", "/whoami").status)
    setcfg(login=LOGIN, host=HOST + ":443")
    ck("a host written with a port is refused as misconfigured", 503, req("GET", "/whoami").status)
    setcfg(login=LOGIN, host=HOST)
    ck("with both set again the hub answers normally", (200, True), (req("GET", "/whoami").status, "present, matches" in req("GET", "/whoami").text))
    bad_env = dict(ENV, PHONE_HUB_CONFIG_DIR=f"{ROOT}/cfg-unset", PHONE_HUB_STATE_DIR=f"{ROOT}/state-unset")
    os.makedirs(f"{ROOT}/cfg-unset", exist_ok=True); json.dump({"port": PORT + 1}, open(f"{ROOT}/cfg-unset/config.json", "w"))
    rc_serve, out_serve = cli("serve", env=bad_env); rc_ens, out_ens = cli("ensure", env=bad_env); rc_st, out_st = cli("status", env=bad_env)
    ck("`serve` refuses to start without login and host, and says what to set", (2, True), (rc_serve, "login" in out_serve and "config.json" in out_serve))
    ck("`ensure` refuses too and starts nothing", (1, True, False), (rc_ens, "not starting" in out_ens, os.path.exists(f"{ROOT}/state-unset/supervisor.pid")))
    ck("`status` shows the configuration problem", True, "config      : set" in out_st)
    setcfg(allowed_sources=["100.64.0.10"])
    ck("forwarded address not on the list", 403, req("GET", "/pair", extra={"X-Forwarded-For": "100.1.1.1"}).status)
    ck("forwarded address on the list", True, req("GET", "/pair", extra={"X-Forwarded-For": "100.64.0.10"}).status in (200, 303))
    ck("no forwarded address: not enforced", True, req("GET", "/pair").status in (200, 303))
    ck("client-supplied FIRST entry cannot spoof the allowed address (rightmost wins)", 403, req("GET", "/pair", extra={"X-Forwarded-For": "100.64.0.10, 100.1.1.1"}).status)
    ck("proxy-appended LAST entry on the list is accepted", True, req("GET", "/pair", extra={"X-Forwarded-For": "100.1.1.1, 100.64.0.10"}).status in (200, 303))
    setcfg(allowed_sources=[])
    r = req("GET", "/icon.png"); ck("icon is a PNG without pairing", (200, b"\x89PNG"), (r.status, r.raw[:4])); ck("icon still needs the identity", 403, req("GET", "/icon.png", login=None).status)

    print("== B pairing (device cookie) ==");
    r = req("GET", "/"); ck("unpaired GET / goes to /pair", (303, "/pair"), (r.status, r.h.get("location")))
    ck("unpaired POST /login refused", 403, req("POST", "/login", body="pin=1").status)
    ck("unpaired agent page goes to /pair", 303, req("GET", "/a/p127").status)
    r = req("GET", "/pair"); ck("no pairing in progress says so", True, "No pairing is in progress" in r.text)
    ck("the page tells you to pair inside the Home Screen app", True, "Home Screen" in r.text)
    rc, out = cli("pair"); code = re.search(r"code:\s+(\S+)", out).group(1)
    ck("code is three words", 3, len(code.split("-")))
    r = req("GET", "/pair"); ck("form appears while a code is active", True, 'name="code"' in r.text)
    r = req("POST", "/pair", body="code=wrong-wrong-wrong&label=x"); ck("wrong code refused", 403, r.status)
    r = req("POST", "/pair", body=f"code={code}&label=iPhone<>X"); dev1 = cookie_value(r, "ph_dev")
    ck("right code pairs and redirects to /login", (303, "/login"), (r.status, r.h.get("location")))
    cl = cookie_line(r, "ph_dev")
    ck("device cookie flags", True, all(x in cl for x in ("Secure", "HttpOnly", "SameSite=Lax", "Max-Age=31536000", "Path=/")))
    ck("device token is long", True, dev1 is not None and len(dev1) >= 40)
    r = req("GET", "/pair", cookies={"ph_dev": dev1}); ck("a paired device opening /pair is sent to / (Home Screen relaunch)", (303, "/"), (r.status, r.h.get("location")))
    r = req("POST", "/pair", body=f"code={code}&label=again"); ck("code is single-use", 403, r.status)
    sec = open(f"{CFGD}/secrets.json").read()
    ck("secrets.json is mode 600", "600", oct(os.stat(f"{CFGD}/secrets.json").st_mode)[-3:])
    ck("config dir is mode 700", "700", oct(os.stat(CFGD).st_mode)[-3:])
    ck("the raw device token is NOT stored", False, dev1 in sec)
    rc, out = cli("devices"); ck("devices lists it, label sanitised", True, "iPhoneX" in out and "<>" not in out)
    setcfg(pair_ttl_secs=1); cli("pair"); time.sleep(2.2)
    ck("expired code refused", 403, req("POST", "/pair", body="code=anything&label=x").status)
    setcfg(pair_ttl_secs=600, max_failures=5, lockout_secs=2)
    rc, out = cli("pair"); code2 = re.search(r"code:\s+(\S+)", out).group(1)
    for _ in range(5): req("POST", "/pair", body="code=guess-guess-guess&label=x")
    ck("5 wrong guesses lock pairing, even the right code is refused", 403, req("POST", "/pair", body=f"code={code2}&label=x").status)
    r = req("GET", "/pair"); ck("locked page says too many tries", True, "Too many tries" in r.text)
    rc, out = cli("pair"); code3 = re.search(r"code:\s+(\S+)", out).group(1)
    time.sleep(2.2)
    ck("after the lock passes a new code works", 303, req("POST", "/pair", body=f"code={code3}&label=Second").status)

    print("== C PIN login ==");
    r = req("GET", "/login", cookies={"ph_dev": dev1}); ck("PIN not set is explained", True, "PIN not set" in r.text)
    for bad in ("123456", "111111", "12ab56", "123", ""):
        rc, out = cli("set-pin", "--stdin", stdin=bad + "\n"); ck(f"weak or invalid PIN {bad!r} rejected", 1, rc)
    rc, out = cli("set-pin", "--stdin", stdin=TESTPIN + "\n"); ck("good PIN accepted", (0, True), (rc, "PIN set" in out))
    sec = json.load(open(f"{CFGD}/secrets.json"))
    ck("PIN is PBKDF2 with 600000 iterations", 600000, sec["pin"]["iters"]); ck("plaintext PIN not stored", False, TESTPIN in open(f"{CFGD}/secrets.json").read())
    r = req("GET", "/login", cookies={"ph_dev": dev1}); ck("login form shown", True, 'type="password"' in r.text)
    s, r = login(dev1, "000111"); ck("wrong PIN refused", (403, None), (r.status, s))
    s, r = login(dev1, TESTPIN); ck("right PIN gives a session", (303, True), (r.status, s is not None))
    cl = cookie_line(r, "ph_sess"); ck("session cookie flags", True, all(x in cl for x in ("Secure", "HttpOnly", "SameSite=Lax")))
    sess1 = s
    r = req("GET", "/", cookies={"ph_dev": dev1, "ph_sess": sess1}); ck("session opens the list", 200, r.status)
    r = req("GET", "/login", cookies={"ph_dev": dev1, "ph_sess": sess1}); ck("/login with a valid session skips the PIN form", (303, "/"), (r.status, r.h.get("location")))
    r = req("GET", "/login", cookies={"ph_dev": dev1}); ck("/login without a session still shows the PIN form", True, 'type="password"' in r.text)
    r = req("GET", "/", cookies={"ph_sess": sess1}); ck("session without the device cookie is useless", 303, r.status)
    for _ in range(5): login(dev1, "999000")
    s, r = login(dev1, TESTPIN); ck("5 wrong PINs lock the device; the right PIN is refused too", (429, None), (r.status, s))
    time.sleep(2.3); s, r = login(dev1, TESTPIN); ck("lock expires", (303, True), (r.status, s is not None)); sess1 = s
    dev2, _, _ = pair("Other")
    for _ in range(5): login(dev1, "999000")
    s2, r = login(dev2, TESTPIN); ck("the global counter locks other devices too", 429, r.status)
    time.sleep(2.3); s2, r = login(dev2, TESTPIN); sess2 = s2; ck("second device can log in", True, s2 is not None)
    r = req("GET", "/", cookies={"ph_dev": dev2, "ph_sess": sess1}); ck("a session cannot be used by a different device", 303, r.status)
    setcfg(session_idle_secs=2); s, r = login(dev1, TESTPIN)
    ck("fresh session works", 200, req("GET", "/?r=0", cookies={"ph_dev": dev1, "ph_sess": s}).status); time.sleep(2.5)
    ck("idle timeout ends the session", 303, req("GET", "/", cookies={"ph_dev": dev1, "ph_sess": s}).status)
    setcfg(session_idle_secs=900, session_max_secs=2); s, r = login(dev1, TESTPIN); time.sleep(2.5)
    ck("absolute lifetime ends the session", 303, req("GET", "/", cookies={"ph_dev": dev1, "ph_sess": s}).status)
    setcfg(session_max_secs=43200); sess1, r = login(dev1, TESTPIN)
    C1 = {"ph_dev": dev1, "ph_sess": sess1}
    r = req("GET", "/", cookies=C1); csrf = re.search(r'name="t" value="([^"]+)"', r.text).group(1)
    ck("logout without the CSRF token refused", 403, req("POST", "/logout", cookies=C1, body="").status)
    ck("logout with a wrong token refused", 403, req("POST", "/logout", cookies=C1, body="t=nope").status)
    ck("logout with the token works", 303, req("POST", "/logout", cookies=C1, body=f"t={csrf}").status)
    ck("the session is dead afterwards", 303, req("GET", "/", cookies=C1).status)
    sess1, r = login(dev1, TESTPIN); C1 = {"ph_dev": dev1, "ph_sess": sess1}

    print("== D content: list, agent page, escaping, injection ==")
    r = req("GET", "/", cookies=C1); t = r.text
    ck("list 200", 200, r.status); ck("auto-refresh meta present", True, 'http-equiv="refresh"' in t)
    ck("waiting agent listed before done and working", True, t.index("review:pr-1") < t.index("change:c-2") < t.index("epic:e-3"))
    ck("stale one is folded into <details>", True, "older (done for over" in t and t.index("<details>") < t.index("daily:d-4"))
    ck("Copilot suffix and leading symbol removed from the title", True, "Change thing</div>" in t)
    ck("bad pane id in the data is ignored", False, "bad;id" in t)
    ck("script in a title is escaped", (False, True), ("<script>alert('t')" in t, "script&gt;alert(&#x27;t&#x27;)&lt;/script&gt;" in t))
    ck("attribute-breaking window name is escaped", (False, True), ('"><img' in t, "&quot;&gt;&lt;img src=x" in t))
    ck("no script element anywhere", False, "<script" in t.replace("&lt;script", ""))
    ck("?r=0 turns auto-refresh off", False, 'http-equiv="refresh"' in req("GET", "/?r=0", cookies=C1).text)
    r = req("GET", "/a/p127", cookies=C1); t = r.text
    ck("agent page 200", 200, r.status); ck("no auto-refresh on the agent page", False, 'http-equiv="refresh"' in t)
    ck("ANSI colour codes stripped", (False, True), ("\x1b" in t, "red text" in t)); ck("control characters stripped", (False, False), ("\x07" in t, "\x00" in t))
    ck("script in pane text is escaped", (False, True), ("<script>alert('pane')" in t, "&lt;script&gt;alert(&#x27;pane&#x27;)&lt;/script&gt;" in t))
    ck("pane text is capped (30000 x's sent)", True, t.count("x") < 21000)
    ck("keeps the END of the output (the latest lines) and opens at the bottom", (True, True), ("last line" in t, 'id="end"' in t))
    ck("list links open the agent page at the bottom (#end)", True, 'href="/a/p127#end"' in req("GET", "/?r=0", cookies=C1).text)
    r = req("GET", "/a/p127?v=exact&n=60", cookies=C1)
    ck("exact view: a single scrolling block, colours kept, no reflow", (200, 1, False), (r.status, r.text.count('<pre class="blk">'), 'class="l' in r.text.split("<main>")[1].split('<div class="term">')[1]))
    ck("view toggle and size links are offered", True, "Reading view" in r.text and "250 lines" in r.text and "?v=smart&amp;n=60" in r.text)
    r = req("GET", "/a/p127?v=%3Cscript%3E&n=99999", cookies=C1)
    ck("hostile or unknown view/size values are ignored (defaults used)", (200, False), (r.status, "<script" in r.text))
    ck("a size that is not one of the offered values falls back", True, "Last 120 lines" in r.text)
    ck("an offered size is honoured", True, "Last 250 lines" in req("GET", "/a/p127?n=250", cookies=C1).text)
    for bad in ("/a/p127;ls", "/a/%25127", "/a/%127", "/a/p999", "/a/../../etc/passwd", "/a/p55", "/a/", "/a/p", "/a/P127", "/a/p127/extra"):
        r = req("GET", bad, cookies=C1); ck(f"{bad} -> 404", 404, r.status)
    ck("pane 55 exists on disk but is not an agent: its text is never served", False, "TOP-SECRET" in req("GET", "/a/p55", cookies=C1).text)
    ck("404 text is identical for unknown and non-agent panes", req("GET", "/a/p999", cookies=C1).raw, req("GET", "/a/p55", cookies=C1).raw)

    print("== E methods, sizes, headers ==")
    for m in ("PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
        ck(f"{m} refused", 405, req(m, "/", cookies=C1).status)
    ck("POST to / is refused (read-only)", True, req("POST", "/", cookies=C1, body="x=1").status in (403, 404))
    ck("POST to an agent page is refused (read-only)", True, req("POST", "/a/p127", cookies=C1, body="x=1").status in (403, 404))
    ck("oversize body refused", 400, req("POST", "/login", cookies={"ph_dev": dev1}, body="pin=" + "1" * 3000).status)
    ck("wrong content type refused", 400, req("POST", "/login", cookies={"ph_dev": dev1}, body="pin=1", ctype="application/json").status)
    ck("overlong path refused", 414, req("GET", "/" + "a" * 400, cookies=C1).status)
    for name, resp in (("200 page", req("GET", "/", cookies=C1)), ("403 denial", req("GET", "/", login=None)), ("303 redirect", req("GET", "/"))):
        h = resp.h
        ck(f"security headers on a {name}", True, h.get("cache-control") == "no-store" and h.get("x-content-type-options") == "nosniff"
           and h.get("referrer-policy") == "no-referrer" and "default-src 'none'" in h.get("content-security-policy", "") and "frame-ancestors 'none'" in h.get("content-security-policy", ""))
    ck("CSP allows no scripts", False, "script-src" in req("GET", "/", cookies=C1).h.get("content-security-policy", ""))
    ck("no server version disclosed", True, "Python" not in req("GET", "/", login=None).h.get("server", ""))

    print("== F kill switch, revoke, restart, rate limit ==")
    cli("off"); ck("OFF: pages answer 503", 503, req("GET", "/", cookies=C1).status); ck("OFF: healthz still answers", 200, req("GET", "/healthz").status)
    ck("OFF: even the pairing page is 503", 503, req("GET", "/pair").status)
    cli("on"); ck("ON again: works", 200, req("GET", "/?r=0", cookies=C1).status)
    stop(); start()
    ck("restart keeps the paired device but ends sessions", (303, "/login"), (lambda r: (r.status, r.h.get("location")))(req("GET", "/", cookies=C1)))
    sess1, r = login(dev1, TESTPIN); C1 = {"ph_dev": dev1, "ph_sess": sess1}
    ck("log in again after the restart", 200, req("GET", "/?r=0", cookies=C1).status)
    sess2n, _r = login(dev2, TESTPIN)
    rc, out = cli("devices"); did = [l.split()[0] for l in out.splitlines() if "iPhoneX" in l][0]
    rc, out = cli("revoke", did); ck("revoke works", 0, rc)
    ck("revoked device is sent back to pairing", (303, "/pair"), (lambda r: (r.status, r.h.get("location")))(req("GET", "/", cookies=C1)))
    ck("another device is unaffected", 200, req("GET", "/?r=0", cookies={"ph_dev": dev2, "ph_sess": sess2n}).status)
    setcfg(rate_per_minute=10)
    codes = [req("GET", "/pair").status for _ in range(16)]; ck("rate limit kicks in (429 appears)", True, 429 in codes)
    setcfg(rate_per_minute=240); time.sleep(0.1)


    print("== I annotate from the phone (step D): files, jobs, PIN freshness, guarded send ==")
    shutil.rmtree(TD2, ignore_errors=True); os.makedirs(TD2)
    tenv2 = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}; tenv2["TMUX_TMPDIR"] = TD2
    def T2(*a): return subprocess.run(["tmux", "-f", "/dev/null", *a], env=tenv2, capture_output=True, text=True)
    open(f"{ROOT}/bp-recorder.py", "w").write(
        "import sys, os, tty, time, select\nfd = sys.stdin.fileno(); tty.setraw(fd)\nsys.stdout.write('\\x1b[?2004h'); sys.stdout.flush()\n"
        "end = time.time() + float(sys.argv[2])\nwhile time.time() < end:\n    r, _, _ = select.select([fd], [], [], 0.1)\n    if r:\n        d = os.read(fd, 65536)\n"
        "        open(sys.argv[1], 'ab').write(d)\n")
    RECV = f"{ROOT}/recv.bin"; open(RECV, "wb").close()
    T2("new-session", "-d", "-s", "t", "-x", "120", "-y", "30", f"python3 {ROOT}/bp-recorder.py {RECV} 600")
    time.sleep(1.2)
    PREC = T2("display-message", "-p", "-t", "t:0.0", "#{pane_id}").stdout.strip(); PB = f"/a/p{PREC[1:]}"
    FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
    def screen(pane, fixture=None, text=None):
        """What the (fake) capture of this pane shows: a REAL captured agent screen from the fixtures, or literal text."""
        open(f"{CAPD}/{pane.lstrip('%')}.txt", "w").write(open(f"{FIX}/{fixture}.txt", errors="replace").read() if fixture else text)
    screen(PREC, "claude-idle-empty")
    ART = f"{ROOT}/art"; CASE1 = f"{ART}/case1"; CASE2 = f"{ART}/case2"; REPO = f"{ART}/repo"; OUT = f"{ROOT}/outside"
    for d in (CASE1, f"{CASE1}/sub", f"{CASE1}/node_modules", f"{CASE1}/.hidden", CASE2, f"{REPO}/.git", OUT): os.makedirs(d, exist_ok=True)
    for name, body in (("a.md", "alpha"), ("b.html", "<h1>b</h1>"), ("notes.txt", "n"), (".env", "SECRET=1"), ("server.pem", "k"), ("run.sh", "x"),
                       ("sub/deep.md", "deep"), ("node_modules/x.md", "x"), (".hidden/h.md", "h"), ("<img src=x onerror=1>.md", "xss name")):
        open(f"{CASE1}/{name}", "w").write(body)
    open(f"{CASE1}/big.md", "wb").write(b"a" * (6 * 1024 * 1024)); open(f"{CASE2}/other.md", "w").write("other agent's file"); open(f"{REPO}/README.md", "w").write("code")
    open(f"{OUT}/o.md", "w").write("outside"); os.symlink(f"{OUT}/o.md", f"{CASE1}/link.md")
    now_ = time.time()
    for name, age in (("a.md", 50), ("b.html", 10), ("notes.txt", 300), ("sub/deep.md", 100)): os.utime(f"{CASE1}/{name}", (now_ - age, now_ - age))
    json.dump({"%127": [{"pane": "%127", "path": REPO, "cmd": "claude"}, {"pane": "%900", "path": CASE1, "cmd": "zsh"}, {"pane": "%901", "path": "/etc", "cmd": "workmux"}],
               "%128": [{"pane": "%128", "path": CASE2, "cmd": "copilot"}],
               PREC: [{"pane": PREC, "path": CASE1, "cmd": "python3"}]}, open(PANEPATHS, "w"))
    def agents(rec_state="done", a127="waiting"):
        write_status([agent("%127", a127, 300, "claude", "review", "pr-1", "Review"), dict(agent("%128", "done", 10, "copilot", "change", "c-2", "C"), workdir=CASE2),
                      dict(agent("%129", "done", 10, "opencode", "epic", "e-3", "E"), workdir=""), dict(agent(PREC, rec_state, 20, "claude", "scratch", "rec", "Recorder"), workdir=CASE1)])
        s = json.load(open(STATUS))
        for a in s["agents"]:
            if a["pane_id"] == "%127": a["workdir"] = REPO
        json.dump(s, open(STATUS, "w"))
    agents()
    stub = f"{ROOT}/stub-annotate"
    open(stub, "w").write("""#!/bin/bash
echo "$* REPOROOTS=$PH_ANNOTATE_REPO_ROOTS" >> "$STUB_ARGS_LOG"
file="$1"; shift; R=""; while [ $# -gt 0 ]; do case "$1" in --result-file) R="$2"; shift ;; esac; shift; done
mode=$(cat "$STUB_MODE_FILE" 2>/dev/null || echo ok)
[ "$mode" = fail ] && { echo "ph-annotate: outside the allowed folders: $file" >&2; exit 3; }
if [ "$mode" = badurl ]; then echo "URL https://evil.example:9999"; else echo "URL https://hub.example.ts.net:8444"; echo "PORT 8444"; fi
G="${R%.result.json}.go"; trap 'exit 0' TERM
while [ ! -s "$G" ]; do sleep 0.2; done
cp "$G" "$R"; echo "DECISION $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["decision"])' "$R")"
""")
    os.chmod(stub, 0o755); open(f"{ROOT}/stub-mode", "w").write("ok")
    setcfg(artifact_roots=[ART], ph_annotate_path=stub, pin_fresh_secs=300, job_max_running=3, job_max_per_hour=50, rate_per_minute=2000)
    stop(); start()
    sessI, r = login(dev2, TESTPIN); CI = {"ph_dev": dev2, "ph_sess": sessI}
    csrfI = re.search(r'name="t" value="([^"]+)"', req("GET", "/?r=0", cookies=CI).text).group(1)
    def post(path, cookies=None, tok=None, extra=""):
        return req("POST", path, cookies=cookies or CI, body=f"t={tok if tok is not None else csrfI}{extra}")
    def art_id(path): import hashlib; return hashlib.sha256(os.path.realpath(path).encode()).hexdigest()[:12]
    def jobid(r): m = re.match(r"^/job/([A-Za-z0-9_-]+)$", r.h.get("location", "")); return m.group(1) if m else None
    def go(jid, payload): open(f"{STATED}/jobs/{jid}.go", "w").write(json.dumps(payload))
    def recv(): return open(RECV, "rb").read()

    print("-- which files are offered")
    r = req("GET", "/a/p127/files", cookies=CI); t = r.text
    ck("files page 200", 200, r.status)
    for name in ("a.md", "b.html", "notes.txt", "sub/deep.md"): ck(f"offers {name}", True, f">{name}</div>" in t.replace("/", "/"))
    for name in (".env", "server.pem", "run.sh", "big.md", "node_modules", "link.md", "h.md", "README.md", "other.md", "o.md"): ck(f"does NOT offer {name}", False, name in t.replace("\n", " ").split("Files</h1>")[1])
    ck("newest first (b.html before a.md before sub/deep.md before notes.txt)", True, t.index("b.html") < t.index("a.md") < t.index("sub/deep.md") < t.index("notes.txt"))
    ck("a file named <img src=x onerror=1>.md is escaped", (False, True), ("<img src=x" in t, "&lt;img src=x onerror=1&gt;.md" in t))
    ck("links carry an id, never a path", True, all(re.fullmatch(r"/a/p127/f/[0-9a-f]{12}", h) for h in re.findall(r'href="(/a/p127/f/[^"]*)"', t)) and "case1" not in re.sub(r"<[^>]+>", " ", " ".join(re.findall(r'href="[^"]*"', t))))
    ck("the code checkout (has .git) and the workmux sidebar folder were not scanned", False, "README" in t)
    r = req("GET", "/a/p129/files", cookies=CI); ck("an agent with no folders gets an explanation, not an error", (200, True), (r.status, "No .md, .html or .txt files found" in r.text))
    r = req("GET", "/a/p128/files", cookies=CI); ck("another agent sees only ITS folder", (True, False), ("other.md" in r.text, "a.md</div>" in r.text))
    ck("unknown agent -> 404", 404, req("GET", "/a/p999/files", cookies=CI).status)
    fid_a = art_id(f"{CASE1}/a.md")
    r = req("GET", f"/a/p127/f/{fid_a}", cookies=CI); ck("file page 200 with a Start form and CSRF", (200, True, True), (r.status, "Start annotating" in r.text, csrfI in r.text))
    ck("an id of an excluded file (.env) -> 404", 404, req("GET", f"/a/p127/f/{art_id(f'{CASE1}/.env')}", cookies=CI).status)
    ck("an id of another agent's file -> 404", 404, req("GET", f"/a/p127/f/{art_id(f'{CASE2}/other.md')}", cookies=CI).status)
    ck("an id that is not hex / traversal -> 404", (404, 404, 404), tuple(req("GET", p, cookies=CI).status for p in ("/a/p127/f/ZZZZZZZZZZZZ", "/a/p127/f/../../etc/passwd", f"/a/p127/f/{fid_a}/../../..")))
    ck("GET on the start URL does nothing (POST only)", 404, req("GET", f"/a/p127/f/{fid_a}/start", cookies=CI).status)

    print("-- starting a session")
    ck("start without CSRF refused", 403, req("POST", f"/a/p127/f/{fid_a}/start", cookies=CI, body="").status)
    ck("start with a wrong CSRF refused", 403, post(f"/a/p127/f/{fid_a}/start", tok="nope").status)
    ck("start from an unpaired browser refused", 403, req("POST", f"/a/p127/f/{fid_a}/start", body=f"t={csrfI}").status)
    open(f"{ROOT}/stub-args.log", "w").close()
    r = post(f"/a/p127/f/{fid_a}/start"); J1 = jobid(r); ck("start with a fresh PIN redirects to a job page", (303, True), (r.status, J1 is not None))
    time.sleep(1.0); args = open(f"{ROOT}/stub-args.log").read()
    ck("ph-annotate got the real file path and a result file", True, os.path.realpath(f"{CASE1}/a.md") in args and "--result-file" in args)
    ck("ph-annotate was NOT given --pane (the hub types at Send time)", False, "--pane" in args)
    r = req("GET", f"/job/{J1}", cookies=CI); t = r.text
    ck("job page shows the link, opens in a new tab safely, and refreshes by itself", (200, True, True, True), (r.status, 'href="https://hub.example.ts.net:8444"' in t, 'rel="noopener noreferrer"' in t and 'target="_blank"' in t, 'http-equiv="refresh"' in t))
    dev3, _, _ = pair("Third"); s3, _ = login(dev3, TESTPIN)
    ck("another device cannot see the job", 404, req("GET", f"/job/{J1}", cookies={"ph_dev": dev3, "ph_sess": s3}).status)
    ck("a made-up job id -> 404", (404, 404), (req("GET", "/job/abcdefghijkl", cookies=CI).status, req("GET", "/job/../../etc", cookies=CI).status))
    ck("job file is private (600) in a private folder (700)", ("600", "700"), (oct(os.stat(f"{STATED}/jobs/{J1}.json").st_mode)[-3:], oct(os.stat(f"{STATED}/jobs").st_mode)[-3:]))
    open(f"{ROOT}/stub-mode", "w").write("badurl"); r = post(f"/a/p127/f/{art_id(f'{CASE1}/b.html')}/start"); Jb = jobid(r); time.sleep(0.8)
    t = req("GET", f"/job/{Jb}", cookies=CI).text; ck("an address outside 8444-8450 is never turned into a link", (False, True), ("evil.example" in t, "not allowed" in t))
    post(f"/job/{Jb}/cancel"); open(f"{ROOT}/stub-mode", "w").write("fail")
    r = post(f"/a/p127/f/{art_id(f'{CASE1}/notes.txt')}/start"); Jf = jobid(r); time.sleep(1.0)
    t = req("GET", f"/job/{Jf}", cookies=CI).text; ck("a helper that fails to start is reported with its message", True, "could not start" in t.lower() and "allowed folders" in t)
    open(f"{ROOT}/stub-mode", "w").write("ok")

    print("-- cancel, limits")
    ck("cancel without CSRF refused", 403, req("POST", f"/job/{J1}/cancel", cookies=CI, body="").status)
    setcfg(job_max_running=1); r = post(f"/a/p127/f/{art_id(f'{CASE1}/sub/deep.md')}/start")
    ck("a second session while one is open is refused (limit 1)", 429, r.status)
    ck("cancel works", 303, post(f"/job/{J1}/cancel").status); time.sleep(1.0)
    ck("cancelled job shows it ended without a decision", True, "without a decision" in req("GET", f"/job/{J1}", cookies=CI).text)
    setcfg(job_max_running=3, job_max_per_hour=1); r = post(f"/a/p127/f/{fid_a}/start"); ck("hourly limit refused", 429, r.status); setcfg(job_max_per_hour=50)

    print("-- PIN freshness (re-enter the PIN before a write)")
    setcfg(pin_fresh_secs=1); time.sleep(2.2)
    r = post(f"/a/p127/f/{fid_a}/start"); ck("stale PIN: start redirects to /reauth for the same file page", (303, f"/reauth?next=/a/p127/f/{fid_a}"), (r.status, r.h.get("location")))
    r = req("GET", f"/reauth?next=/a/p127/f/{fid_a}", cookies=CI); ck("reauth page asks for the PIN and carries the next step", True, 'type="password"' in r.text and f'value="/a/p127/f/{fid_a}"' in r.text)
    for evil in ("//evil.com", "https://evil.com", "/job/x", "/a/p127/../../etc", "javascript:alert(1)", "/a/p127/f/ZZZ", "/logout"):
        r = req("GET", f"/reauth?next={evil}", cookies=CI); ck(f"reauth refuses next={evil!r} (falls back to /)", True, 'name="next" value="/"' in r.text)
    r = req("POST", "/reauth", cookies=CI, body=f"t={csrfI}&next=/a/p127/f/{fid_a}&pin=000111"); ck("wrong PIN on reauth refused", 403, r.status)
    ck("reauth without CSRF refused", 403, req("POST", "/reauth", cookies=CI, body=f"next=/&pin={TESTPIN}").status)
    setcfg(pin_fresh_secs=300)
    r = req("POST", "/reauth", cookies=CI, body=f"t={csrfI}&next=/a/p127/f/{fid_a}&pin={TESTPIN}"); ck("right PIN on reauth continues to the file page", (303, f"/a/p127/f/{fid_a}"), (r.status, r.h.get("location")))
    r = post(f"{PB}/f/{fid_a}/start"); J2 = jobid(r); ck("after reauth the start works", True, J2 is not None)

    print("-- finishing and sending (typed into a recorder pane, never into a real agent)")
    FB = "Please rename the helper.\n> a quoted comment with `backticks` and $dollar <script>alert(1)</script>"
    go(J2, {"decision": "annotated", "feedback": FB, "annotationCount": 1}); time.sleep(1.2)
    r = req("GET", f"/job/{J2}", cookies=CI); t = r.text
    ck("the page shows the decision and the exact text, escaped", (True, True, False), ("annotated" in t, "&lt;script&gt;alert(1)&lt;/script&gt;" in t, "<script>alert(1)" in t))
    ck("it names the target agent and offers a Send form", True, "scratch:rec" in t and "Send to the agent" in t)
    ck("nothing has been typed yet", 0, len(recv()))
    # the target agent opens a question dialog between the start and the decision: nothing may be typed
    agents(rec_state="done")
    J4 = jobid(post(f"{PB}/f/{art_id(f'{CASE1}/notes.txt')}/start")); time.sleep(0.8)
    go(J4, {"decision": "annotated", "feedback": "late feedback", "annotationCount": 1}); time.sleep(1.2)
    agents(rec_state="waiting"); screen(PREC, "claude-permission")      # the dialog appeared AFTER the session started
    t = req("GET", f"/job/{J4}", cookies=CI).text
    ck("waiting agent: the page says it cannot be sent and shows no Send button", (True, False), ("cannot be sent right now" in t, "Send to the agent" in t))
    r = post(f"/job/{J4}/send"); ck("forcing the POST is refused too (409), nothing typed", (409, 0), (r.status, len(recv())))
    agents(rec_state="done"); screen(PREC, "claude-idle-empty")
    ck("send without CSRF refused", 403, req("POST", f"/job/{J2}/send", cookies=CI, body="").status)
    setcfg(pin_fresh_secs=1); time.sleep(2.2)
    r = post(f"/job/{J2}/send"); ck("stale PIN: send redirects to reauth, nothing typed", (303, f"/reauth?next=/job/{J2}", 0), (r.status, r.h.get("location"), len(recv())))
    setcfg(pin_fresh_secs=300); req("POST", "/reauth", cookies=CI, body=f"t={csrfI}&next=/job/{J2}&pin={TESTPIN}")
    agents(rec_state="done")
    r = post(f"/job/{J2}/send"); ck("send with a fresh PIN: redirect to the job page", (303, f"/job/{J2}"), (r.status, r.h.get("location")))
    time.sleep(1.0); data = recv()
    expect = ("Plannotator feedback on " + os.path.realpath(f"{CASE1}/a.md") + ":\n\n" + FB).replace("\n", "\r").encode()
    ck("the agent received a BRACKETED PASTE of exactly the shown text", True, data.startswith(b"\x1b[200~" + expect + b"\x1b[201~"))
    ck("followed by exactly one Enter", b"\r", data[len(b"\x1b[200~" + expect + b"\x1b[201~"):])
    ck("job page now says Sent", True, "Sent to the agent" in req("GET", f"/job/{J2}", cookies=CI).text)
    before = len(recv()); r = post(f"/job/{J2}/send"); time.sleep(0.5)
    ck("a second Send is refused and types nothing", (409, before), (r.status, len(recv())))
    print("-- the guards on the pane")
    rows = json.load(open(PANEPATHS)); rows[PREC][0]["cmd"] = "zsh"; json.dump(rows, open(PANEPATHS, "w"))
    before = len(recv()); r = post(f"/job/{J4}/send")
    ck("a pane that has become a shell is refused and nothing is typed", (409, before), (r.status, len(recv())))
    rows[PREC][0]["cmd"] = "python3"; json.dump(rows, open(PANEPATHS, "w"))
    agents(rec_state="done"); write_status([agent("%127", "done")]); 
    before = len(recv()); r = post(f"/job/{J4}/send")
    ck("a pane that is no longer a registered agent is refused", (409, before), (r.status, len(recv())))
    print("-- the guard reads the SCREEN, not only workmux's status (workmux says `done` in every case below)")
    agents(rec_state="done")
    for fx, txt, expect_in_page in (("claude-draft", None, "unsent text"), ("claude-question-menu", None, "dialog"), ("claude-permission", None, "dialog"),
                                    ("claude-working", None, "busy"), (None, "just some output\nnothing like a prompt\n", "not recognised")):
        screen(PREC, fx, txt); before = len(recv()); label = fx or "unrecognised screen"
        t = req("GET", f"/job/{J4}", cookies=CI).text
        ck(f"[{label}] the page says why it cannot be sent and shows no Send button", (True, False), (expect_in_page in t, "Send to the agent" in t))
        r = post(f"/job/{J4}/send"); time.sleep(0.4)
        ck(f"[{label}] forcing the POST is refused (409) and nothing is typed", (409, before), (r.status, len(recv())))
    screen(PREC, "claude-idle-empty")
    agents(rec_state="done"); r = post(f"/job/{J4}/send"); time.sleep(0.8)
    ck("once the agent is back at its prompt, the SAME job can be sent (a refusal does not use up the one send)", (303, True), (r.status, b"late feedback" in recv()))
    huge = jobid(post(f"{PB}/f/{art_id(f'{CASE1}/a.md')}/start")); time.sleep(0.8); go(huge, {"decision": "annotated", "feedback": "x" * 25000, "annotationCount": 1}); time.sleep(1.2)
    t = req("GET", f"/job/{huge}", cookies=CI).text; ck("feedback over 20000 characters cannot be sent from the phone", (True, False), ("too long" in t, "Send to the agent" in t))
    appr = jobid(post(f"{PB}/f/{art_id(f'{CASE1}/a.md')}/start")); time.sleep(0.8); go(appr, {"decision": "approved", "feedback": "", "annotationCount": 0}); time.sleep(1.2)
    t = req("GET", f"/job/{appr}", cookies=CI).text; ck("approved with no comments: nothing to send", (True, False), ("Nothing to send" in t, "Send to the agent" in t))

    print("-- a hub restart does not strand a job")
    stop(); start()
    s2, r = login(dev2, TESTPIN); CI = {"ph_dev": dev2, "ph_sess": s2}
    t = req("GET", f"/job/{J2}", cookies=CI).text; ck("after a restart the finished job is still shown (state comes from files)", (True, True), ("annotated" in t, "Sent to the agent" in t))
    Jr = jobid(req("POST", f"/a/p127/f/{fid_a}/start", cookies=CI, body="t=" + re.search(r'name="t" value="([^"]+)"', req("GET", "/?r=0", cookies=CI).text).group(1)))
    time.sleep(0.8); stop(); start(); s2, r = login(dev2, TESTPIN); CI = {"ph_dev": dev2, "ph_sess": s2}
    ck("a job still waiting when the hub restarts is still shown as waiting, with its link", True, "Open annotation" in req("GET", f"/job/{Jr}", cookies=CI).text)
    ck("a waiting job page has a Check now link back to itself (Safari pauses the auto refresh in a background tab)", True, f'href="/job/{Jr}">Check now' in req("GET", f"/job/{Jr}", cookies=CI).text)
    go(Jr, {"decision": "dismissed", "feedback": "", "annotationCount": 0}); time.sleep(1.2)
    ck("and its decision arrives afterwards", True, "Dismissed" in req("GET", f"/job/{Jr}", cookies=CI).text)
    ck("the agent page offers the Files button", True, "/a/p127/files" in req("GET", "/a/p127", cookies=CI).text)

    print("-- audit of step D")
    log = open(f"{STATED}/audit.log").read(); ev = {json.loads(l)["event"] for l in log.splitlines()}
    for e in ("view_files", "annotate_start", "annotate_cancel", "annotate_refused", "reauth_ok", "reauth_fail", "send_enter"): ck(f"audit has {e}", True, e in ev)
    ck("audit never holds the feedback text", False, any(x in log for x in ("rename the helper", "late feedback", "backticks")))
    ck("audit never holds a file path", False, CASE1 in log)

    print("-- D2: review a diff")
    RR = f"{ROOT}/repos"; os.makedirs(RR, exist_ok=True)
    GENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    def git(*a, cwd): subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=GENV)
    GR1, GOUT, GNOT = f"{RR}/proj", f"{ROOT}/outside-repo", f"{RR}/not-a-repo"
    for d in (GR1, GOUT, GNOT): os.makedirs(d, exist_ok=True)
    for d in (GR1, GOUT):
        git("init", "-q", "-b", "main", cwd=d); open(f"{d}/f.txt", "w").write("one\n"); git("add", "-A", cwd=d); git("commit", "-qm", "base", cwd=d)
    git("checkout", "-qb", "feature/x<b>", cwd=GR1); open(f"{GR1}/f.txt", "a").write("two\n"); open(f"{GOUT}/f.txt", "a").write("changed\n")
    setcfg(repo_roots=[RR], artifact_roots=[ART])
    rows = json.load(open(PANEPATHS)); rows[PREC] = [{"pane": PREC, "path": GR1, "cmd": "python3"}, {"pane": "%950", "path": GOUT, "cmd": "zsh"}]
    rows["%129"] = [{"pane": "%129", "path": GNOT, "cmd": "claude"}]; json.dump(rows, open(PANEPATHS, "w"))
    agents(rec_state="done")
    s = json.load(open(STATUS))
    for a in s["agents"]:
        if a["pane_id"] == PREC: a["workdir"] = GR1
        if a["pane_id"] == "%129": a["workdir"] = GNOT
    json.dump(s, open(STATUS, "w"))
    csrfJ = re.search(r'name="t" value="([^"]+)"', req("GET", "/?r=0", cookies=CI).text).group(1)
    def postJ(path, **kw): return req("POST", path, cookies=CI, body="t=" + csrfJ + "".join(f"&{k}={v}" for k, v in kw.items()))
    r = req("GET", f"{PB}/review", cookies=CI); t = r.text
    ck("review page 200 and names the repo and its branch (escaped)", (200, True, True, False), (r.status, ">proj<" in t, "feature/x&lt;b&gt;" in t, "feature/x<b>" in t))
    ck("a repo outside repo_roots is not offered (even though another pane sits in it)", False, "outside-repo" in t)
    ck("three diff choices are offered", 3, t.count('name="d" value='))
    ck("it says what the page can and cannot do", True, "cannot stage files" in t)
    ck("an agent with no git repo gets an explanation", (200, True), (lambda rr: (rr.status, "No git repository found" in rr.text))(req("GET", "/a/p129/review", cookies=CI)))
    ck("the agent page has a Review diff button", True, f"{PB}/review" in req("GET", f"{PB}", cookies=CI).text)
    ck("unknown agent -> 404", 404, req("GET", "/a/p999/review", cookies=CI).status)
    rid = re.search(r'name="r" value="([0-9a-f]{12})"', t).group(1)
    ck("review start without CSRF refused", 403, req("POST", f"{PB}/review/start", cookies=CI, body=f"r={rid}&d=uncommitted").status)
    ck("an unknown diff type is refused (400)", 400, postJ(f"{PB}/review/start", r=rid, d="x%3Brm%20-rf%20%2F").status)
    ck("an unknown repo id is refused (404)", 404, postJ(f"{PB}/review/start", r="0" * 12, d="uncommitted").status)
    import hashlib
    ck("the id of a repo outside repo_roots is refused (404)", 404, postJ(f"{PB}/review/start", r=hashlib.sha256(os.path.realpath(GOUT).encode()).hexdigest()[:12], d="uncommitted").status)
    ck("GET on the start URL does nothing", 404, req("GET", f"{PB}/review/start", cookies=CI).status)
    setcfg(pin_fresh_secs=1); time.sleep(2.2)
    r = postJ(f"{PB}/review/start", r=rid, d="branch"); ck("stale PIN: redirected to reauth for the review page", (303, f"/reauth?next={PB}/review"), (r.status, r.h.get("location")))
    ck("reauth accepts that next step", True, f'value="{PB}/review"' in req("GET", f"/reauth?next={PB}/review", cookies=CI).text)
    setcfg(pin_fresh_secs=300); r = req("POST", "/reauth", cookies=CI, body=f"t={csrfJ}&next={PB}/review&pin={TESTPIN}"); ck("after the PIN you land back on the review page", (303, f"{PB}/review"), (r.status, r.h.get("location")))
    open(f"{ROOT}/stub-args.log", "w").close(); open(f"{ROOT}/stub-mode", "w").write("ok")
    r = postJ(f"{PB}/review/start", r=rid, d="uncommitted"); JR = jobid(r); ck("a review session starts and redirects to its job page", (303, True), (r.status, JR is not None)); time.sleep(1.0)
    args = open(f"{ROOT}/stub-args.log").read()
    ck("the helper got --review with the repo's real path and the chosen diff type", True, f"--review {os.path.realpath(GR1)} --diff-type uncommitted" in args)
    ck("it was NOT given --pane, and was given the repo roots", (False, True), ("--pane" in args, f"REPOROOTS={os.path.realpath(RR)}" in args))
    t = req("GET", f"/job/{JR}", cookies=CI).text
    ck("the job page names the repo and what is shown, with the session link", (True, True), ("proj: Uncommitted changes" in t, "Open annotation" in t))
    FBR = "## 1. (src/f.txt:2) comment\n> please rename this <script>alert(1)</script>\n"
    go(JR, {"decision": "annotated", "feedback": FBR, "annotationCount": 1, "label": "Uncommitted changes", "kind": "review", "diff_type": "uncommitted"}); time.sleep(1.2)
    t = req("GET", f"/job/{JR}", cookies=CI).text
    ck("finished review shows the exact text it would type, escaped, with a Send button", (True, True, False, True), ("Code review feedback on " in t and "(Uncommitted changes)" in t, "&lt;script&gt;" in t, "<script>alert" in t, "Send to the agent" in t))
    before = len(recv()); r = postJ(f"/job/{JR}/send"); time.sleep(1.0); data = recv()[before:]
    expect = ("Code review feedback on " + os.path.realpath(GR1) + " (Uncommitted changes):\n\n" + FBR).replace("\n", "\r").encode()
    if not data.startswith(b"\x1b[200~" + expect + b"\x1b[201~"):
        print("    DEBUG expected:", repr(expect[:260])); print("    DEBUG received:", repr(data[:300]))
    ck("Send pastes exactly that text as one bracketed paste, then Enter", (303, True, True), (r.status, data.startswith(b"\x1b[200~" + expect + b"\x1b[201~"), data.endswith(b"\x1b[201~\r")))
    ck("and the job is marked sent", True, "Sent to the agent" in req("GET", f"/job/{JR}", cookies=CI).text)
    agents(rec_state="waiting"); screen(PREC, "claude-question-menu"); s = json.load(open(STATUS))
    for a in s["agents"]:
        if a["pane_id"] == PREC: a["workdir"] = GR1
    json.dump(s, open(STATUS, "w"))
    JW = jobid(postJ(f"{PB}/review/start", r=rid, d="last-commit")); time.sleep(0.8); go(JW, {"decision": "annotated", "feedback": "later", "annotationCount": 1, "label": "Last commit"}); time.sleep(1.2)
    before = len(recv()); r = postJ(f"/job/{JW}/send"); time.sleep(0.5)
    ck("a review for an agent that is waiting on a question is refused and nothing is typed", (409, before), (r.status, len(recv())))
    log = open(f"{STATED}/audit.log").read(); ev = {json.loads(l)["event"] for l in log.splitlines()}
    screen(PREC, "claude-idle-empty")
    ck("audit has view_review and review_start", True, "view_review" in ev and "review_start" in ev)
    ck("audit never holds a repo path, the feedback, or the branch name", False, any(x in log for x in (GR1, "please rename", "feature/x")))
    agents(rec_state="done")


    print("== E1 Reply (a typed message for an idle agent) and E2 Keypad (answering a dialog) ==")
    import urllib.parse as up
    q = up.quote
    def fresh_pin(nxt): setcfg(pin_fresh_secs=300); req("POST", "/reauth", cookies=CI, body=f"t={csrfJ}&next={nxt}&pin={TESTPIN}")
    def newbytes(before): return recv()[before:]
    def fp_of(html_): m = re.search(r'name="fp" value="([0-9a-f]+)"', html_); return m.group(1) if m else None
    agents(rec_state="done"); screen(PREC, "claude-idle-empty"); fresh_pin(f"{PB}/reply")
    r = req("GET", f"{PB}/reply", cookies=CI)
    ck("reply form for an idle agent: a text box and a Preview button", (200, True, True), (r.status, "<textarea" in r.text, "Preview" in r.text))
    MSG = "Please also run the linter <b>now</b>\nand tell me what it says"
    before = len(recv()); r = postJ(f"{PB}/reply", text=q(MSG))
    nonce = (re.search(r'name="n" value="([^"]+)"', r.text) or [None, None])[1]
    ck("preview shows the exact text, escaped, with a Send button; nothing typed yet", (200, True, False, True, 0), (r.status, "&lt;b&gt;now&lt;/b&gt;" in r.text, "<b>now</b>" in r.text, "Send to the agent" in r.text, len(newbytes(before))))
    ck("send without CSRF refused", 403, req("POST", f"{PB}/reply/send", cookies=CI, body=f"n={nonce}&text={q(MSG)}").status)
    r = postJ(f"{PB}/reply/send", n=nonce, text=q(MSG)); time.sleep(0.8); d = newbytes(before)
    ck("send: the agent got ONE bracketed paste of exactly that text, then Enter", (200, True, True), (r.status, d.startswith(b"\x1b[200~" + MSG.replace("\n", "\r").encode() + b"\x1b[201~"), d.endswith(b"\x1b[201~\r")))
    before = len(recv()); r = postJ(f"{PB}/reply/send", n=nonce, text=q(MSG)); time.sleep(0.4)
    ck("replaying the same preview (double tap) is refused and types nothing", (409, 0), (r.status, len(newbytes(before))))
    r = postJ(f"{PB}/reply", text=q("first text")); n2 = re.search(r'name="n" value="([^"]+)"', r.text).group(1)
    r = postJ(f"{PB}/reply/send", n=n2, text=q("a DIFFERENT text")); time.sleep(0.4)
    ck("a changed text with a valid preview token is refused and types nothing", (409, 0), (r.status, len(newbytes(before))))
    for bad, why in (("!rm -rf x", "starts with !"), ("/clear", "starts with /"), ("# remember this", "starts with #"), ("  !x", "starts with ! after spaces"), ("a\x07b", "control character"),
                     ("x" * 2001, "over 2000 characters"), ("   ", "empty")):
        r = postJ(f"{PB}/reply", text=q(bad)); ck(f"a reply that is {why} is refused at the preview", (400, True), (r.status, "Send to the agent" not in r.text))
    r = postJ(f"{PB}/reply", text=q("looks fine")); n3 = re.search(r'name="n" value="([^"]+)"', r.text).group(1)
    for fx, txt, nm in (("claude-draft", None, "an unsent draft"), ("claude-permission", None, "an open dialog"), ("claude-working", None, "a busy agent")):
        screen(PREC, fx, txt); r = postJ(f"{PB}/reply", text=q("looks fine")); ck(f"preview refused with {nm} on the screen", (409, True), (r.status, "Send to the agent" not in r.text))
        r = req("GET", f"{PB}/reply", cookies=CI); ck(f"reply form shows no text box with {nm} on the screen", False, "<textarea" in r.text)
    screen(PREC, "claude-permission"); r = req("GET", f"{PB}/reply", cookies=CI); ck("with a dialog open, the reply page offers the Answer page instead", True, f"{PB}/keys" in r.text)
    before = len(recv()); r = postJ(f"{PB}/reply/send", n=n3, text=q("looks fine")); time.sleep(0.4)
    ck("a preview taken while idle is still refused at send if a dialog has opened since", (409, 0), (r.status, len(newbytes(before))))
    screen(PREC, "claude-idle-empty")
    setcfg(pin_fresh_secs=1); time.sleep(2.2)
    r = req("GET", f"{PB}/reply", cookies=CI); ck("stale PIN: the reply form redirects to reauth", (303, f"/reauth?next={PB}/reply"), (r.status, r.h.get("location")))
    r = postJ(f"{PB}/reply", text=q("x")); ck("stale PIN: preview redirects to reauth", 303, r.status)
    fresh_pin(f"{PB}/reply")
    r = postJ(f"{PB}/reply", text=q("rate one")); n4 = re.search(r'name="n" value="([^"]+)"', r.text).group(1)
    setcfg(replies_per_10min=1); before = len(recv()); r = postJ(f"{PB}/reply/send", n=n4, text=q("rate one")); setcfg(replies_per_10min=20)
    ck("more replies than the limit allows are refused (429)", (429, 0), (r.status, len(newbytes(before))))

    print("-- workmux's `waiting` can stick after a dialog is gone: the screen decides")
    agents(rec_state="waiting"); screen(PREC, "claude-after-esc-status-waiting"); fresh_pin(f"{PB}/reply")
    r = req("GET", f"{PB}/reply", cookies=CI); ck("status waiting but an empty prompt on screen (after Esc): Reply is offered", (200, True), (r.status, "<textarea" in r.text))
    r = req("GET", f"{PB}", cookies=CI); ck("... and the agent page offers Reply, not Answer", (True, False), (f'href="{PB}/reply"' in r.text, "Answer the question" in r.text))
    n5 = re.search(r'name="n" value="([^"]+)"', postJ(f"{PB}/reply", text=q("go on, but use the other approach")).text).group(1)
    before = len(recv()); r = postJ(f"{PB}/reply/send", n=n5, text=q("go on, but use the other approach")); time.sleep(0.8); d = newbytes(before)
    ck("... and the reply really is typed", (200, True, True), (r.status, b"go on, but use the other approach" in d, d.endswith(b"\r")))
    agents(rec_state="done"); screen(PREC, "claude-ghost")
    r = req("GET", f"{PB}/reply", cookies=CI); ck("a dim suggestion in the empty prompt is not a draft: Reply is offered", (200, True), (r.status, "<textarea" in r.text))
    agents(rec_state="working"); screen(PREC, "claude-idle-empty")      # el=20 s: workmux says working, the screen shows an empty prompt
    r = req("GET", f"{PB}/reply", cookies=CI); ck("status working for a while but an empty prompt on screen (after an aborted request): Reply is offered", (200, True), (r.status, "<textarea" in r.text))
    st_ = json.load(open(STATUS))
    for a_ in st_["agents"]:
        if a_["pane_id"] == PREC: a_["elapsed_secs"] = 3
    json.dump(st_, open(STATUS, "w"))
    r = req("GET", f"{PB}/reply", cookies=CI); ck("... but not in the first seconds of a turn (the spinner may not be drawn yet)", (200, False), (r.status, "<textarea" in r.text))
    agents(rec_state="waiting"); screen(PREC, text="something nobody recognises\nat all\n")
    r = req("GET", f"{PB}", cookies=CI); ck("status waiting on an unrecognised screen: the agent page offers both Reply and Answer", (True, True), (f'href="{PB}/reply"' in r.text, "Answer the question" in r.text))
    agents(rec_state="done"); screen(PREC, "claude-idle-empty")

    print("-- every dialog type seen on real agents opens the keypad (workmux says done in each case)")
    agents(rec_state="done")
    for fx in ("claude-question-menu", "claude-permission", "claude-plan-approval", "copilot-trust-dialog", "copilot-permission", "copilot-permission-mode-menu", "opencode-permission"):
        screen(PREC, fx); r = req("GET", f"{PB}/keys", cookies=CI); ck(f"[{fx}] the keypad is offered", True, "A dialog is open" in r.text)
        r = req("GET", f"{PB}/reply", cookies=CI); ck(f"[{fx}] Reply is refused", False, "<textarea" in r.text)
    screen(PREC, "claude-idle-empty")

    print("-- E2 keypad")
    agents(rec_state="waiting"); screen(PREC, "claude-permission"); fresh_pin(f"{PB}/keys")
    r = req("GET", f"{PB}/keys", cookies=CI); FP = fp_of(r.text)
    ck("keypad for a waiting agent with a dialog on screen: arrows, Enter, Esc and numbers", (200, True, True, True, True), (r.status, FP is not None, "A dialog is open" in r.text, f"{PB}/keys/confirm?k=3" in r.text, f"{PB}/keys/confirm?k=Enter" in r.text))
    agents(rec_state="done"); r = req("GET", f"{PB}/keys", cookies=CI)
    ck("the screen decides: workmux says done but a dialog is open, the keypad is still offered", True, "A dialog is open" in r.text)
    FP = fp_of(r.text); before = len(recv())
    ck("press without CSRF refused", 403, req("POST", f"{PB}/keys/press", cookies=CI, body=f"k=Down&fp={FP}").status)
    r = postJ(f"{PB}/keys/press", k="Down", fp=FP); time.sleep(0.3); d = newbytes(before)
    ck("an arrow key is pressed once, with no confirm needed", (303, True), (r.status, d in (b"\x1b[B", b"\x1bOB")))
    before = len(recv()); r = postJ(f"{PB}/keys/press", k="Down", fp="0" * 16); ck("a stale fingerprint is refused and nothing is pressed", (409, 0), (r.status, len(newbytes(before))))
    for k in ("Enter", "Esc", "3"):
        before = len(recv()); r = postJ(f"{PB}/keys/press", k=k, fp=FP); ck(f"{k} without the confirm step is refused", (400, 0), (r.status, len(newbytes(before))))
    r = req("GET", f"{PB}/keys/confirm?k=3", cookies=CI)
    ck("the confirm page shows the dialog again and names what will be pressed", (200, True, True, True), (r.status, "Press 3?" in r.text, "term" in r.text, 'name="ok" value="1"' in r.text))
    FP2 = fp_of(r.text); before = len(recv()); r = postJ(f"{PB}/keys/press", k="3", ok="1", fp=FP2); time.sleep(0.3)
    ck("confirmed: the digit is pressed once", (303, b"3"), (r.status, newbytes(before)))
    before = len(recv()); r = postJ(f"{PB}/keys/press", k="Enter", ok="1", fp=FP2); time.sleep(0.3); ck("confirmed Enter is pressed", (303, b"\r"), (r.status, newbytes(before)))
    before = len(recv()); r = postJ(f"{PB}/keys/press", k="Esc", ok="1", fp=FP2); time.sleep(0.3); ck("confirmed Esc is pressed", (303, b"\x1b"), (r.status, newbytes(before)))
    for k in ("q", "Ctrl-C", "C-c", "Down;Enter", "10", "0", ""):
        before = len(recv()); r = postJ(f"{PB}/keys/press", k=q(k), ok="1", fp=FP); ck(f"key {k!r} is not on the allowed list", (400, 0), (r.status, len(newbytes(before))))
    r = req("GET", f"{PB}/keys/confirm?k=Backspace", cookies=CI); ck("the confirm page refuses a key that is not allowed", 404, r.status)
    r = req("GET", "/a/p129/keys/confirm?k=3", cookies=CI); ck("an agent that ignores number keys (OpenCode) refuses digits", 404, r.status)
    before = len(recv()); r = postJ(f"{PB}/keys/text", text=q("purple"), fp=FP); time.sleep(0.4); d = newbytes(before)
    ck("text for a dialog's own text line is pasted (bracketed) with NO Enter", (303, b"\x1b[200~purple\x1b[201~"), (r.status, d))
    for bad, why in (("!x", "starts with !"), ("a\nb", "has a newline"), ("y" * 301, "is over 300 characters")):
        before = len(recv()); r = postJ(f"{PB}/keys/text", text=q(bad), fp=FP); ck(f"dialog text that {why} is refused", (400, 0), (r.status, len(newbytes(before))))
    before = len(recv()); r = postJ(f"{PB}/keys/text", text=q("ok"), fp="1" * 16); ck("dialog text with a stale fingerprint is refused", (409, 0), (r.status, len(newbytes(before))))
    r = req("GET", f"{PB}", cookies=CI); ck("the agent page offers 'Answer the question' while a dialog is open", (True, False), ("Answer the question" in r.text, f'href="{PB}/reply"' in r.text))
    screen(PREC, "claude-idle-empty"); agents(rec_state="waiting"); before = len(recv())
    r = req("GET", f"{PB}/keys", cookies=CI); ck("workmux says waiting but the screen shows an empty prompt: no keypad", (200, True, False), (r.status, "Nothing to answer" in r.text, "keys/press" in r.text))
    r = postJ(f"{PB}/keys/press", k="Down", fp=FP); ck("a key is refused when no dialog is on the screen", (409, 0), (r.status, len(newbytes(before))))
    agents(rec_state="done"); r = req("GET", f"{PB}", cookies=CI); ck("the agent page offers Reply (not Answer) at an empty prompt", (True, False), (f'href="{PB}/reply"' in r.text, "Answer the question" in r.text))
    old = open(f"{FIX}/claude-permission.txt", errors="replace").read(); idle = open(f"{FIX}/claude-idle-empty.txt", errors="replace").read()
    screen(PREC, text="Esc to cancel · Tab to amend\n" + "\n".join(f"output line {i}" for i in range(40)) + "\n" + idle)
    r = req("GET", f"{PB}/keys", cookies=CI); ck("a dialog phrase far up in old output does not unlock the keypad", (True, False), ("Nothing to answer" in r.text, "keys/press" in r.text))
    screen(PREC, "claude-permission"); r = req("GET", f"{PB}/keys", cookies=CI); FP = fp_of(r.text)
    setcfg(pin_fresh_secs=1); time.sleep(2.2); before = len(recv())
    r = req("GET", f"{PB}/keys", cookies=CI); ck("stale PIN: the keypad redirects to reauth", (303, f"/reauth?next={PB}/keys"), (r.status, r.h.get("location")))
    r = postJ(f"{PB}/keys/press", k="Down", fp=FP); ck("stale PIN: a key press redirects to reauth and presses nothing", (303, 0), (r.status, len(newbytes(before))))
    fresh_pin(f"{PB}/keys"); setcfg(keys_per_minute=1); before = len(recv())
    r = postJ(f"{PB}/keys/press", k="Down", fp=FP); setcfg(keys_per_minute=30); ck("more key presses than the per-minute limit are refused (429)", (429, 0), (r.status, len(newbytes(before))))
    log = open(f"{STATED}/audit.log").read(); evs = {json.loads(l)["event"] for l in log.splitlines()}
    ck("audit records reply_send, reply_refused, key_press, key_text and view_keys", True, {"reply_send", "reply_refused", "key_press", "key_text", "view_keys"} <= evs)
    ck("audit never holds a reply, a typed dialog text, or the fingerprint", False, any(x in log for x in ("run the linter", "purple", "looks fine", FP)))
    screen(PREC, "claude-idle-empty"); agents(rec_state="done")

    subprocess.run(["pkill", "-f", f"python3 {ROOT}/bp-recorder.py"], capture_output=True); T2("kill-server"); shutil.rmtree(TD2, ignore_errors=True)

    print("== G audit log ==");
    log = open(f"{STATED}/audit.log").read(); ev = [json.loads(l)["event"] for l in log.splitlines()]
    refused = [json.loads(l) for l in log.splitlines() if '"deny_host"' in l or '"deny_identity"' in l]
    dh = [x for x in refused if x["event"] == "deny_host"]; di = [x for x in refused if x["event"] == "deny_identity"]
    ck("deny_host records what arrived (Host, whether an identity header came)", True, any(x.get("host") == "evil.example" and x.get("has_identity") is True for x in dh))
    ck("deny_identity records the Host and that no identity header came", True, any(x.get("host") == HOST and x.get("has_identity") is False for x in di))
    ck("whoami is logged too", True, any(json.loads(l)["event"] == "whoami" for l in log.splitlines()))
    for e in ("pair_started", "pair_ok", "pair_fail", "pair_locked", "login_ok", "login_fail", "login_locked", "view_agent", "logout", "kill_switch_on", "revoke", "pin_set", "deny_identity", "deny_host", "server_start"):
        ck(f"audit has {e}", True, e in ev)
    ck("audit file is mode 600", "600", oct(os.stat(f"{STATED}/audit.log").st_mode)[-3:])
    ck("audit never holds the PIN", False, TESTPIN in log); ck("audit never holds a pairing code", False, any(c in log for c in (code, code2, code3)))
    ck("audit never holds a device token", False, dev1 in log or dev2 in log); ck("audit never holds pane text", False, "red text" in log or "TOP-SECRET" in log)

    def pid_up(pid):
        try:
            os.kill(pid, 0); return subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip() not in ("", "Z")
        except OSError: return False
    print("== H starting by `ensure` through tmux run-shell, on a scratch tmux (survives the job, can run workmux) ==")
    stop()
    S = socket.socket(); S.bind(("127.0.0.1", 0)); P2 = S.getsockname()[1]; S.close()
    CFG2, ST2 = f"{ROOT}/cfg2", f"{ROOT}/state2"; os.makedirs(CFG2); json.dump({"port": P2, "login": LOGIN, "host": HOST}, open(f"{CFG2}/config.json", "w"))
    TD = os.path.join(os.environ.get("TMPDIR", "/tmp").rstrip("/"), "phhub-tmux"); shutil.rmtree(TD, ignore_errors=True); os.makedirs(TD)
    tenv = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}; tenv["TMUX_TMPDIR"] = TD
    def T(*a): return subprocess.run(["tmux", "-f", "/dev/null", *a], env=tenv, capture_output=True, text=True)
    T("new-session", "-d", "-s", "t", "sleep 600")
    cmd = f"env -i HOME={os.path.expanduser('~')} PHONE_HUB_CONFIG_DIR={CFG2} PHONE_HUB_STATE_DIR={ST2} /usr/bin/env python3 {HUB} ensure > {ROOT}/ensure.out 2>&1; env -i HOME={os.path.expanduser('~')} PHONE_HUB_CONFIG_DIR={CFG2} PHONE_HUB_STATE_DIR={ST2} /usr/bin/env python3 {HUB} selftest >> {ROOT}/ensure.out 2>&1"
    T("run-shell", "-b", cmd)
    ok = False
    for _ in range(60):
        try:
            c = http.client.HTTPConnection("127.0.0.1", P2, timeout=1); c.request("GET", "/healthz"); ok = c.getresponse().status == 200; c.close()
            if ok: break
        except OSError: time.sleep(0.2)
    ck("hub started through tmux run-shell answers /healthz", True, ok)
    for _ in range(100):
        time.sleep(0.3)
        if re.search(r"(OK|FAIL) agents", open(f"{ROOT}/ensure.out").read() if os.path.exists(f"{ROOT}/ensure.out") else ""): break
    T("kill-server"); time.sleep(1.0)
    try:
        c = http.client.HTTPConnection("127.0.0.1", P2, timeout=2); c.request("GET", "/healthz"); alive = c.getresponse().status == 200; c.close()
    except OSError: alive = False
    ck("hub keeps running after the tmux server (its parent) is gone", True, alive)
    time.sleep(1.5); out = open(f"{ROOT}/ensure.out").read()
    ck("ensure said started", True, "started" in out or "already running" in out)
    ck("selftest read REAL agents (at least one) from that bare environment (PATH + locale handled by the hub)", True, bool(re.search(r"OK agents: [1-9]", out)))
    ck("selftest could read a real pane there too", True, "OK capture" in out)
    print("    ensure/selftest output:", out.strip().replace("\n", " | ")[:200])
    print("== H2 supervisor: the server comes back by itself, `restart` swaps it, stopping the supervisor stops everything ==")
    env2 = {"HOME": os.path.expanduser("~"), "PATH": os.environ["PATH"], "PHONE_HUB_CONFIG_DIR": CFG2, "PHONE_HUB_STATE_DIR": ST2}
    def up2():
        try:
            c = http.client.HTTPConnection("127.0.0.1", P2, timeout=1); c.request("GET", "/healthz"); r = c.getresponse().status == 200; c.close(); return r
        except OSError: return False
    def pidf(n):
        try: return int(open(f"{ST2}/{n}.pid").read())
        except Exception: return 0
    def wait_up(want, secs):
        for _ in range(int(secs / 0.2)):
            if up2() == want: return True
            time.sleep(0.2)
        return False
    sup = pidf("supervisor"); srv = pidf("server")
    ck("ensure left a supervisor running next to the server", True, bool(sup) and bool(srv) and sup != srv and pid_up(sup) and pid_up(srv))
    os.kill(srv, 9)
    ck("a hard-killed server is gone", True, wait_up(False, 2))
    ck("the supervisor brings it back by itself within 10 s", True, wait_up(True, 10))
    time.sleep(0.5); srv2 = pidf("server")
    ck("the new server is a different process, under the same supervisor", (True, sup), (srv2 != srv and pid_up(srv2), pidf("supervisor")))
    ck("the exit was written to the audit log", True, '"server_exit"' in open(f"{ST2}/audit.log").read())
    r = subprocess.run([sys.executable, HUB, "restart"], env=env2, capture_output=True, text=True, timeout=30)
    time.sleep(0.5); srv3 = pidf("server")
    ck("`restart` answers started and the server is a new process", (True, True), ("started" in r.stdout, srv3 != srv2 and up2()))
    ck("`ensure` while it is healthy does nothing and starts no second supervisor", (True, sup), ("already running" in subprocess.run([sys.executable, HUB, "ensure"], env=env2, capture_output=True, text=True, timeout=30).stdout, pidf("supervisor")))
    os.kill(sup, 15)
    ck("stopping the supervisor stops the server too", True, wait_up(False, 5))
    time.sleep(0.5)
    ck("and removes its pid file, with no process left behind", (False, False), (os.path.exists(f"{ST2}/supervisor.pid"), pid_up(srv3) or pid_up(sup)))
    time.sleep(0.5); shutil.rmtree(TD, ignore_errors=True)
finally:
    stop()
    shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\nPASS={passed} FAIL={failed}")
