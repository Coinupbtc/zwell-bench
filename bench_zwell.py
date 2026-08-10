#!/usr/bin/env python3
"""Zwell foundation bake-off harness v1 (2026-07-16).

Endpoint-agnostic eval for Zwell candidates, weighted by the Zwell mission:
  C. coding      (35%) — generated code EXECUTED against unit tests, incl. a debug-fix task
  W. web-intel   (20%) — structured extraction + comparison from messy HTML (exact JSON match)
  V. vision      (20%) — PIL-generated chart / UI screenshot / table, exact-value reads
  T. tool-call   (15%) — correct tool + args, tool choice, and knowing when NOT to call
  A. agentic     (10%) — multi-step exact-answer reasoning / plan ordering

Every check is objective (executed, exact, or strict-contains). Reports per-category
gen t/s. Vision auto-skips (score renormalized, noted) if the endpoint rejects images.

Usage:
  ZWELL_BASE=http://127.0.0.1:8889 python3 bench_zwell.py --tag miaai35-baseline
  ZWELL_BASE=http://192.168.100.11:8100 python3 bench_zwell.py --tag mimo-v25-iq2m
"""
import argparse, base64, datetime, json, os, pathlib, re, statistics, subprocess, tempfile, time, urllib.request

BASE = os.environ.get("ZWELL_BASE", "http://127.0.0.1:8889")
MODEL = os.environ.get("ZWELL_MODEL", "m")
HERE = pathlib.Path(__file__).resolve().parent
ASSETS = HERE / "assets"
RESULTS = HERE / "results"
WEIGHTS = {"coding": 0.35, "web": 0.20, "vision": 0.20, "tools": 0.15, "agentic": 0.10}

CHECKS = []
TPS = {}
CODE_DIAG = ""   # why the last ask_code() produced no runnable code

def check(name, cat, passed, detail=""):
    CHECKS.append({"name": name, "cat": cat, "pass": bool(passed), "detail": str(detail)[:200]})
    print(f"  [{'PASS' if passed else 'FAIL'}] {cat}/{name} {'' if passed else detail}", flush=True)

MAXTOK_MULT = float(os.environ.get("ZWELL_MAXTOK_MULT", "1"))

THINKING = os.environ.get("ZWELL_THINKING", "off") == "on"

# Sampling is a FAIRNESS knob, not a style knob. Reasoning models degenerate into
# repetitive think-loops at very low temperature — they burn the whole budget and emit
# no answer, which scores as incapability. GLM-5.2's own generation_config.json asks
# for temperature=1.0 / top_p=0.95. Default to the vendor value; override to compare.
TEMP = float(os.environ.get("ZWELL_TEMP", "1.0"))
TOP_P = float(os.environ["ZWELL_TOP_P"]) if os.environ.get("ZWELL_TOP_P") else 0.95

def chat(messages, max_tokens=1200, tools=None, temperature=None, thinking=None):
    thinking = THINKING if thinking is None else thinking
    temperature = TEMP if temperature is None else temperature
    body = {"model": MODEL, "messages": messages, "temperature": temperature,
            "max_tokens": int(max_tokens * MAXTOK_MULT),
            "chat_template_kwargs": {"enable_thinking": thinking}}
    if TOP_P is not None:
        body["top_p"] = TOP_P
    if tools:
        body["tools"] = tools
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.load(r)
    wall = time.time() - t0
    t = d.get("timings", {})
    msg = d["choices"][0]["message"]
    content = msg.get("content") or ""
    # Thinking models: prefer the server-side split when a --reasoning-parser is wired
    # (vLLM puts the trace in reasoning_content and leaves `content` = answer only).
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if not reasoning:
        # Fallback for a server with NO reasoning parser. Templates like GLM-5.2's end with
        # '<|assistant|><think>', so the OPENING tag is in the prompt and never appears in
        # content — a paired <think>...</think> regex silently matches nothing and the whole
        # scratchpad gets graded as the answer. Cut at the last closing tag first.
        if "</think>" in content:
            content = content.rsplit("</think>", 1)[-1]
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    content = content.strip()
    tps = t.get("predicted_per_second")
    n = t.get("predicted_n") or d.get("usage", {}).get("completion_tokens")
    if tps is None and n and wall > 0:
        tps = n / wall
    # finish/reasoning are forensics: without them an empty `content` is indistinguishable
    # from a wrong answer, and a budget-limited run scores as incapability.
    return {"content": content, "tool_calls": msg.get("tool_calls"), "tps": tps, "n": n,
            "finish": d["choices"][0].get("finish_reason"),
            "reasoning_len": len(reasoning or ""), "budget": int(max_tokens * MAXTOK_MULT)}

def note_tps(cat, r):
    if r.get("tps"):
        TPS.setdefault(cat, []).append(r["tps"])

def extract_code(text):
    """Return (code, how). `how` is 'fenced' | 'bare' | 'none'.

    Never return prose as code: an unfenced English answer used to be handed to python3
    verbatim, which crashes as `NameError: name '<fn>' is not defined` — a message that
    looks exactly like the model writing a broken implementation. Unfenced text is only
    accepted when it actually looks like Python.
    """
    m = re.findall(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    if m:
        return m[-1], "fenced"
    if re.search(r"^\s*(def|class|import|from)\s+\w", text, re.M):
        return text, "bare"
    return "", "none"

def extract_json(text):
    m = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    cands = m or [text]
    for c in cands:
        for pat in (r"\{.*\}", r"\[.*\]"):
            j = re.search(pat, c, re.S)
            if j:
                try:
                    return json.loads(j.group(0))
                except Exception:
                    pass
    return None

def run_python(code, test_code):
    # An empty extraction must report WHY, not fake a NameError from the test harness.
    if not code.strip():
        return False, CODE_DIAG
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code + "\n\n" + test_code + "\nprint('ALL_TESTS_PASS')\n")
        path = f.name
    try:
        p = subprocess.run(["python3", path], capture_output=True, text=True, timeout=20)
        return "ALL_TESTS_PASS" in p.stdout, (p.stderr or p.stdout)[-300:]
    except Exception as e:
        return False, str(e)[:300]
    finally:
        pathlib.Path(path).unlink(missing_ok=True)

def ask_code(prompt, budget=1600):
    global CODE_DIAG
    msgs = [{"role": "system", "content": "You are an expert Python engineer. Reply with a single ```python code block containing only the implementation (no example usage, no tests)."},
            {"role": "user", "content": prompt}]
    r = chat(msgs, max_tokens=budget)
    note_tps("coding", r)
    code, how = extract_code(r["content"])
    # Fair retry: a reasoning model that spent its whole budget thinking has been measured
    # on its budget, not its ability. Give it one clean pass with 3x the room.
    if how == "none":
        print(f"      !! no answer emitted (finish={r['finish']} n={r['n']}/{r['budget']} "
              f"reasoning={r['reasoning_len']}ch content={len(r['content'])}ch) — retry at {budget*3}",
              flush=True)
        r = chat(msgs, max_tokens=budget * 3)
        note_tps("coding", r)
        code, how = extract_code(r["content"])
    CODE_DIAG = (f"NO CODE EMITTED after retry (finish={r['finish']} n={r['n']}/{r['budget']} "
                 f"reasoning={r['reasoning_len']}ch content={len(r['content'])}ch): {r['content'][:120]!r}"
                 if how == "none" else f"[{how}] finish={r['finish']} n={r['n']}/{r['budget']}")
    return code

# ---------------------------------------------------------------- C. coding
def suite_coding():
    print("== coding ==")
    code = ask_code(
        "Implement class TTLCache with __init__(self, capacity, ttl), get(key, now) and put(key, value, now). "
        "`now` is a float timestamp passed explicitly. Entries older than ttl (now - insert_time > ttl) are expired "
        "and get returns None. When over capacity, evict the least-recently-used non-expired entry. "
        "get on a live entry refreshes recency but NOT its insert time. put on existing key updates value, insert time, recency.")
    ok, why = run_python(code, """
c = TTLCache(2, 10)
c.put('a', 1, 0.0); c.put('b', 2, 1.0)
assert c.get('a', 2.0) == 1
c.put('c', 3, 3.0)                     # evicts b (a was refreshed at t=2)
assert c.get('b', 4.0) is None
assert c.get('c', 4.0) == 3
assert c.get('a', 11.0) is None        # a inserted t=0, expired at t=11
c.put('d', 4, 12.0)
assert c.get('d', 12.5) == 4
""")
    check("ttl_lru_cache", "coding", ok, why)

    code = ask_code(
        "Write a function summarize_log(text) that parses lines like "
        "'2026-07-16 05:41:02 [ERROR] hermes-gateway-light: exit 2' (level is INFO, WARN or ERROR; "
        "service name precedes the colon). Ignore lines that do not match. Return a dict "
        "{'errors': <count of ERROR lines>, 'by_service': {service: count of ERROR lines for that service}, "
        "'first_error': <full first ERROR line or None>}.")
    ok, why = run_python(code, """
t = '''2026-07-16 05:41:02 [ERROR] hermes-gateway-light: exit 2
garbage line
2026-07-16 05:42:10 [WARN] ollama: slow
2026-07-16 06:00:00 [ERROR] hermes-gateway-light: exit 2
2026-07-16 06:01:00 [ERROR] restic: lock timeout'''
r = summarize_log(t)
assert r['errors'] == 3, r
assert r['by_service'] == {'hermes-gateway-light': 2, 'restic': 1}, r
assert r['first_error'] == '2026-07-16 05:41:02 [ERROR] hermes-gateway-light: exit 2', r
""")
    check("log_summarize", "coding", ok, why)

    code = ask_code(
        "This function has bugs. Fix it and return the corrected version (same name/signature):\n"
        "```python\n"
        "def merge_intervals(intervals):\n"
        "    intervals.sort()\n"
        "    out = [intervals[0]]\n"
        "    for s, e in intervals:\n"
        "        if s < out[-1][1]:\n"
        "            out[-1] = (out[-1][0], e)\n"
        "        else:\n"
        "            out.append((s, e))\n"
        "    return out\n"
        "```\n"
        "Required behavior: merge overlapping OR touching intervals ([1,2] and [2,3] merge); "
        "result end must be max of ends (not last seen); empty input returns []; input list must not be mutated.")
    ok, why = run_python(code, """
inp = [(5, 7), (1, 3), (2, 6), (8, 8), (7, 8)]
orig = list(inp)
assert merge_intervals(inp) == [(1, 7), (7, 8)] or merge_intervals(inp) == [(1, 8)], merge_intervals(inp)
assert merge_intervals([(1,4),(2,3)]) == [(1,4)]
assert merge_intervals([]) == []
assert inp == orig, 'input mutated'
r = merge_intervals([(1,2),(2,3),(5,6)])
assert r == [(1,3),(5,6)], r
""")
    check("debug_fix_intervals", "coding", ok, why)

    code = ask_code(
        "Write dedupe_products(items): items is a list of dicts with keys 'title', 'price', 'url'. "
        "Two items are duplicates when their normalized titles match (lowercase, strip whitespace, "
        "collapse internal runs of whitespace, remove trailing text in parentheses like ' (2-pack)'). "
        "Keep the cheapest item of each duplicate group; ties keep the first seen. "
        "Return the kept items in order of first appearance of their group.")
    ok, why = run_python(code, """
items = [
 {'title':'Pikachu Plush  (2-pack)','price':29.99,'url':'a'},
 {'title':'pikachu plush','price':19.99,'url':'b'},
 {'title':'Charizard Card','price':99.0,'url':'c'},
 {'title':'PIKACHU   PLUSH','price':19.99,'url':'d'},
]
r = dedupe_products(items)
assert len(r) == 2, r
assert r[0]['url'] == 'b', r
assert r[1]['url'] == 'c', r
""")
    check("dedupe_products", "coding", ok, why)

# ---------------------------------------------------------------- T. tools
TOOLS = [
 {"type": "function", "function": {
   "name": "browse_url", "description": "Fetch a web page and return its text",
   "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
 {"type": "function", "function": {
   "name": "run_shell", "description": "Run a shell command on the local DGX and return stdout",
   "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
 {"type": "function", "function": {
   "name": "search_web", "description": "Web search, returns result titles and URLs",
   "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
]

def first_call(r):
    tc = r.get("tool_calls") or []
    if not tc:
        return None, None
    f = tc[0].get("function", {})
    try:
        args = json.loads(f.get("arguments") or "{}")
    except Exception:
        args = {}
    return f.get("name"), args

def suite_tools():
    print("== tools ==")
    r = chat([{"role": "user", "content": "Check whether the systemd user service llama-miaai35 is active on this machine."}],
             tools=TOOLS, max_tokens=700)
    note_tps("tools", r)
    name, args = first_call(r)
    ok = name == "run_shell" and "llama-miaai35" in (args.get("command") or "") and "systemctl" in (args.get("command") or "")
    check("pick_shell_tool", "tools", ok, f"call={name} args={args} content={r['content'][:80]}")

    r = chat([{"role": "user", "content": "Open https://mempool.space and tell me what it says the current fastest fee rate is."}],
             tools=TOOLS, max_tokens=700)
    note_tps("tools", r)
    name, args = first_call(r)
    ok = name == "browse_url" and "mempool.space" in (args.get("url") or "")
    check("pick_browse_tool", "tools", ok, f"call={name} args={args} content={r['content'][:80]}")

    r = chat([{"role": "user", "content": "What is 17 * 23? Answer directly."}], tools=TOOLS, max_tokens=600)
    note_tps("tools", r)
    name, _ = first_call(r)
    ok = name is None and "391" in r["content"]
    check("no_tool_when_direct", "tools", ok, f"call={name} content={r['content'][:80]}")

# ---------------------------------------------------------------- A. agentic
def suite_agentic():
    print("== agentic ==")
    r = chat([{"role": "user", "content":
        "A scan job starts at 02:00 and takes 150 minutes. A backup needs the scan finished and takes 45 minutes. "
        "A report needs the backup finished, takes 20 minutes, and can only START at or after 05:30 (it waits if earlier). "
        "At what time (HH:MM, 24h) does the report finish? Reply with the time only."}], max_tokens=900)
    note_tps("agentic", r)
    check("schedule_math", "agentic", "05:50" in r["content"], r["content"][:80])

    r = chat([{"role": "user", "content":
        "Tasks and dependencies: deploy needs build+tests; tests needs build; build needs lint; docs needs nothing. "
        "You can run at most ONE task at a time. Give one valid order of all 5 tasks as a comma-separated list, lowercase."}],
             max_tokens=900)
    note_tps("agentic", r)
    txt = r["content"].lower()
    seq = [w.strip() for w in re.split(r"[,\n]", txt) if w.strip() in {"lint", "build", "tests", "deploy", "docs"}]
    ok = (len(set(seq)) == 5 and seq.index("lint") < seq.index("build") < seq.index("tests") < seq.index("deploy"))
    check("plan_order", "agentic", ok, txt[:100])

# ---------------------------------------------------------------- W. web-intel
PAGE1 = """<div class=srp><h3>Results — graphics cards</h3>
<div class=item><a href="/p/91">MSI RTX 5070 Gaming X 12GB</a><span class="pr">$629.99</span><span class=st>In stock</span></div>
<div class=item><a href="/p/92">ASUS RTX 5070 Dual 12GB</a><span class="pr">$599.00</span><span class=st>Out of stock</span></div>
<div class=item><a href="/p/93">Gigabyte RTX 5070 WindForce 12GB</a><span class="pr">$619.50</span><span class=st>In stock</span></div>
<div class=ad><a href="/sp/99">SPONSORED: RTX 5090 SuperDeal</a><span class="pr">$29.99/mo lease</span></div>
<a class=next href="/page/2">Next</a></div>"""
PAGE2 = """<div class=srp>
<div class=item><a href="/p/94">PNY RTX 5070 Verto 12GB</a><span class="pr">$609.00</span><span class=st>In stock</span></div>
<div class=item><a href="/p/93">Gigabyte RTX 5070 WindForce 12GB</a><span class="pr">$619.50</span><span class=st>In stock</span></div>
<div class=item><a href="/p/95">Zotac RTX 5070 Twin Edge 12GB</a><span class="pr">$589.99</span><span class=st>Preorder — ships Aug 30</span></div>
</div>"""

def suite_web():
    print("== web ==")
    r = chat([{"role": "user", "content":
        "Here are two result pages from the same shop (page 2 repeats one item). Extract the UNIQUE real products "
        "(ignore sponsored/lease ads) as JSON: {\"products\": [{\"brand\": str, \"price\": float, \"in_stock\": bool}], "
        "\"cheapest_in_stock_brand\": str}. in_stock is true only for 'In stock' (not preorder/out of stock). "
        "Reply with JSON only.\n\nPAGE 1:\n" + PAGE1 + "\n\nPAGE 2:\n" + PAGE2}], max_tokens=1400)
    note_tps("web", r)
    d = extract_json(r["content"]) or {}
    prods = d.get("products") or []
    brands = {str(p.get("brand", "")).lower() for p in prods}
    ok = (len(prods) == 5 and {"msi", "asus", "gigabyte", "pny", "zotac"} <= brands)
    check("extract_dedupe_5", "web", ok, f"n={len(prods)} brands={sorted(brands)}")
    gb = next((p for p in prods if str(p.get("brand", "")).lower() == "gigabyte"), {})
    zt = next((p for p in prods if str(p.get("brand", "")).lower() == "zotac"), {})
    ok = gb.get("price") == 619.50 and gb.get("in_stock") is True and zt.get("in_stock") is False
    check("fields_exact", "web", ok, f"gigabyte={gb} zotac={zt}")
    ok = str(d.get("cheapest_in_stock_brand", "")).lower() == "pny"
    check("cheapest_in_stock", "web", ok, f"got={d.get('cheapest_in_stock_brand')} (zotac 589.99 is preorder; asus OOS; answer=PNY 609)")

# ---------------------------------------------------------------- V. vision
def make_assets():
    from PIL import Image, ImageDraw, ImageFont
    ASSETS.mkdir(exist_ok=True)
    try:
        big = ImageFont.load_default(size=30); mid = ImageFont.load_default(size=24)
    except TypeError:
        big = mid = ImageFont.load_default()
    # bar chart: tokens/sec by model
    img = Image.new("RGB", (900, 620), "white"); d = ImageDraw.Draw(img)
    d.text((240, 20), "Decode speed (tokens/sec)", fill="black", font=big)
    bars = [("Qwen35B", 169, "#4477aa"), ("MiMo", 27, "#ee6677"), ("DSpark", 55, "#228833"), ("MiniMax", 25, "#ccbb44")]
    for i, (label, v, col) in enumerate(bars):
        x = 90 + i * 200
        d.rectangle([x, 540 - v * 2.6, x + 130, 540], fill=col)
        d.text((x + 10, 550), label, fill="black", font=mid)
        d.text((x + 35, 540 - v * 2.6 - 38), str(v), fill="black", font=mid)
    img.save(ASSETS / "chart.png")
    # UI screenshot: deploy dialog with disabled button + error
    img = Image.new("RGB", (900, 560), "#e8e8ee"); d = ImageDraw.Draw(img)
    d.rectangle([60, 40, 840, 500], fill="white", outline="#888")
    d.rectangle([60, 40, 840, 96], fill="#334455"); d.text((80, 55), "Deploy Console", fill="white", font=big)
    d.text((90, 130), "Target: production-cluster-2", fill="black", font=mid)
    d.text((90, 180), "Error: config.yaml missing — deploy blocked", fill="#cc2222", font=mid)
    d.rectangle([420, 380, 600, 445], fill="#dddddd", outline="#999"); d.text((455, 398), "Deploy", fill="#aaaaaa", font=mid)
    d.rectangle([640, 380, 810, 445], fill="#3366cc"); d.text((680, 398), "Cancel", fill="white", font=mid)
    img.save(ASSETS / "ui.png")

def img_msg(path, question):
    b64 = base64.b64encode(pathlib.Path(path).read_bytes()).decode()
    return [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        {"type": "text", "text": question}]}]

def suite_vision():
    print("== vision ==")
    make_assets()
    try:
        r = chat(img_msg(ASSETS / "chart.png",
                         "From this bar chart: which model is fastest and what is its tokens/sec value? Then: what is the value for DSpark? Answer briefly."),
                 max_tokens=900)
    except Exception as e:
        print(f"  vision unsupported on this endpoint ({str(e)[:120]}) — suite skipped")
        return False
    note_tps("vision", r)
    txt = r["content"].lower()
    check("chart_read", "vision", ("qwen" in txt and "169" in txt and "55" in txt), r["content"][:120])
    r = chat(img_msg(ASSETS / "ui.png",
                     "Look at this app screenshot. 1) What exact error is shown? 2) Which button appears disabled (greyed out)? Answer briefly."),
             max_tokens=900)
    note_tps("vision", r)
    txt = r["content"].lower()
    check("ui_error_read", "vision", "config.yaml" in txt, r["content"][:120])
    check("ui_disabled_btn", "vision", "deploy" in txt.split("2)")[-1] if "2)" in txt else "deploy" in txt, r["content"][:120])
    return True

# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--vision", choices=["auto", "off"], default="auto")
    args = ap.parse_args()
    print(f"Zwell bench v1 · base={BASE} · tag={args.tag}")
    try:
        with urllib.request.urlopen(BASE + "/v1/models", timeout=10) as r:
            json.load(r)
    except Exception as e:
        raise SystemExit(f"endpoint down: {BASE} ({e})")
    t0 = time.time()
    suite_coding(); suite_tools(); suite_agentic(); suite_web()
    vision_ran = suite_vision() if args.vision == "auto" else False

    cats = {}
    for c in CHECKS:
        cats.setdefault(c["cat"], []).append(c["pass"])
    w = dict(WEIGHTS)
    if not vision_ran:
        w.pop("vision"); s = sum(w.values()); w = {k: v / s for k, v in w.items()}
    score = sum(w[k] * (sum(v) / len(v)) for k, v in cats.items() if k in w) * 100
    print("\n== scorecard ==")
    for k in ("coding", "web", "vision", "tools", "agentic"):
        if k in cats:
            tps = statistics.median(TPS[k]) if TPS.get(k) else None
            print(f"  {k:8s} {sum(cats[k])}/{len(cats[k])}  median {tps:.1f} t/s" if tps else f"  {k:8s} {sum(cats[k])}/{len(cats[k])}")
    print(f"  WEIGHTED SCORE: {score:.1f}/100{'' if vision_ran else '  (vision skipped, renormalized)'}")
    print(f"  total checks: {sum(len(v) for v in cats.values())} · wall {time.time()-t0:.0f}s")
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{args.tag}.json"
    out.write_text(json.dumps({"tag": args.tag, "base": BASE, "ts": datetime.datetime.now().isoformat(),
                               "score": round(score, 1), "vision_ran": vision_ran, "checks": CHECKS,
                               "tps_median": {k: round(statistics.median(v), 1) for k, v in TPS.items()}}, indent=1))
    print(f"  saved {out}")

if __name__ == "__main__":
    main()
