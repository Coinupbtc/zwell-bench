#!/usr/bin/env python3
"""coinupbtc personal eval — does this model help with THIS box's real work?

Complements bench_zwell.py (general capability). Every task here is drawn from
work that actually runs on sparkmax-10ef, and every check is objective: executed
code, exact value, or strict membership. No LLM judging, no vibes.

  O. ops-diagnosis   (30%) — read real log/stat shapes, name the root cause
  S. structured      (25%) — emit strictly-parseable JSON for the alert/digest pipelines
  D. domain-code     (25%) — code against this stack's real data shapes (arb, crypto, vault)
  R. recall-reason   (20%) — multi-constraint reasoning over stack facts

Usage:
  ZWELL_BASE=http://127.0.0.1:8000 ZWELL_MODEL=glm-5.2 python3 bench_coinupbtc.py --tag glm52
"""
import argparse, json, os, re, subprocess, tempfile, time, urllib.request

BASE = os.environ.get("ZWELL_BASE", "http://127.0.0.1:8000")
MODEL = os.environ.get("ZWELL_MODEL", "glm-5.2")
MULT = float(os.environ.get("ZWELL_MAXTOK_MULT", "1"))
THINKING = os.environ.get("ZWELL_THINKING", "off") == "on"
# Vendor sampling (GLM-5.2 generation_config.json): temp 1.0 / top_p 0.95. Low temp makes
# reasoning models loop until the budget dies, which measures the sampler, not the model.
TEMP = float(os.environ.get("ZWELL_TEMP", "1.0"))
TOP_P = float(os.environ["ZWELL_TOP_P"]) if os.environ.get("ZWELL_TOP_P") else 0.95
RESULTS, TPS = [], {}

WEIGHTS = {"ops": 0.30, "structured": 0.25, "domain": 0.25, "recall": 0.20}


def chat(messages, max_tokens=1200, temperature=None):
    temperature = TEMP if temperature is None else temperature
    body = {"model": MODEL, "messages": messages, "temperature": temperature,
            "top_p": TOP_P,
            "max_tokens": int(max_tokens * MULT),
            "chat_template_kwargs": {"enable_thinking": THINKING}}
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=3600) as r:
        d = json.load(r)
    wall = time.time() - t0
    m = d["choices"][0]["message"]
    content = m.get("content") or ""
    reasoning = m.get("reasoning_content") or m.get("reasoning")
    if not reasoning and "</think>" in content:
        content = content.rsplit("</think>", 1)[-1]
    n = d.get("usage", {}).get("completion_tokens")
    return {"content": content.strip(),
            "finish": d["choices"][0]["finish_reason"],
            "tps": (n / wall) if n and wall > 0 else None, "n": n}


def check(name, cat, ok, detail=""):
    RESULTS.append({"name": name, "cat": cat, "ok": bool(ok), "detail": detail[:200]})
    # flush: stdout is redirected to a log file, so block buffering otherwise hides all
    # progress until the process exits — a silent run looks identical to a hung one.
    print(f"  [{'PASS' if ok else 'FAIL'}] {cat}/{name}  {detail[:150]}", flush=True)


def note(cat, r):
    if r.get("tps"):
        TPS.setdefault(cat, []).append(r["tps"])
    if r.get("finish") == "length":
        print(f"      !! finish_reason=length ({r['n']} tok) — budget-limited, not capability",
              flush=True)


def extract_code(t):
    """Return (code, how): 'fenced' | 'bare' | 'none'. Never hand prose to python3 —
    that turns 'model wrote no code' into a fake NameError from the test harness."""
    m = re.findall(r"```(?:python)?\s*\n(.*?)```", t, re.S)
    if m:
        return m[-1], "fenced"
    if re.search(r"^\s*(def|class|import|from)\s+\w", t, re.M):
        return t, "bare"
    return "", "none"


def extract_json(t):
    m = re.findall(r"```(?:json)?\s*\n(.*?)```", t, re.S)
    cand = m[-1] if m else t
    i, j = cand.find("{"), cand.rfind("}")
    if i >= 0 and j > i:
        cand = cand[i:j + 1]
    try:
        return json.loads(cand)
    except Exception:
        return None


DIAG = ""   # why the last take_code() produced nothing runnable


def take_code(r, msgs, budget):
    """Extract code, and give a reasoning model ONE fair retry at 3x budget if it spent
    its whole allowance thinking and never emitted an answer."""
    global DIAG
    code, how = extract_code(r["content"])
    if how == "none":
        print(f"      !! no answer emitted (finish={r['finish']} n={r['n']} "
              f"content={len(r['content'])}ch) — retry at {budget*3}", flush=True)
        r = chat(msgs, max_tokens=budget * 3)
        note("domain", r)
        code, how = extract_code(r["content"])
    DIAG = ("" if how != "none" else
            f"NO CODE EMITTED after retry (finish={r['finish']} n={r['n']} "
            f"content={len(r['content'])}ch): {r['content'][:100]!r}")
    return code


def run_code(code, test):
    if not code.strip():
        return False, DIAG
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code + "\n\n" + test)
        p = f.name
    try:
        r = subprocess.run(["python3", p], capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stderr or r.stdout)[-200:]
    except subprocess.TimeoutExpired:
        return False, "timeout"


# ---------------------------------------------------------------- O. ops
def suite_ops():
    print("== ops-diagnosis ==")

    # The real cascade from CLAUDE.md failure-mode #2: many agent crons fail at once.
    r = chat([{"role": "user", "content":
        "Six scheduled agent jobs on one server all failed within the same minute, every one with "
        "`APIConnectionError`. A seventh job, which runs a plain shell script and calls no language model, "
        "succeeded at the same time. Name the single most likely root cause in one short sentence, "
        "and state the FIRST command you would run to confirm it."}], max_tokens=700)
    c = r["content"].lower()
    ok = (any(k in c for k in ["inference", "model server", "llm server", "api server", "endpoint", "backend"])
          and any(k in c for k in ["down", "not running", "crashed", "unavailable", "offline", "stopped"]))
    check("cron_cascade_root_cause", "ops", ok, r["content"][:150].replace("\n", " "))
    note("ops", r)

    # Residency/thrash reasoning — the exact bug hit during this trial.
    r = chat([{"role": "user", "content":
        "A model server's weights are memory-mapped files totalling 79 GB per node. Decode ran at 15 tokens/s. "
        "After raising a KV-cache setting, available page cache dropped from 83 GB to 77 GB, decode fell to 4.8 tokens/s, "
        "and the disk began reading ~1.4 GB every 20 seconds. Explain the mechanism in one sentence and give the fix."}],
        max_tokens=700)
    c = r["content"].lower()
    ok = (any(k in c for k in ["page cache", "cache", "resident", "residency", "fit"])
          and any(k in c for k in ["fault", "evict", "thrash", "disk", "swap", "re-read", "reread"]))
    check("plane_residency_thrash", "ops", ok, r["content"][:150].replace("\n", " "))
    note("ops", r)

    # Knowing when NOT to blame the obvious thing.
    r = chat([{"role": "user", "content":
        "A nightly backup job reports SUCCESS every night and the snapshot count grows. "
        "But a restore test produced a repository with far fewer files than the source. "
        "In one sentence: what invariant was never actually being checked?"}], max_tokens=600)
    c = r["content"].lower()
    ok = any(k in c for k in ["integrity", "verif", "content", "restore", "complete", "actually", "valid", "checksum"])
    check("backup_presence_vs_integrity", "ops", ok, r["content"][:150].replace("\n", " "))
    note("ops", r)


# ------------------------------------------------------- S. structured output
def suite_structured():
    print("== structured-output ==")

    r = chat([{"role": "user", "content":
        "Return ONLY a JSON object (no prose) for this alert with keys exactly: "
        "service (string), severity (one of: info, warn, critical), restart_required (boolean), "
        "failed_checks (array of strings).\n"
        "Event: the inference service on port 8889 is unreachable, two health probes failed "
        "(models_endpoint, systemd_active), and it must be restarted."}], max_tokens=700)
    j = extract_json(r["content"])
    ok = (isinstance(j, dict)
          and j.get("severity") in ("critical", "warn")
          and j.get("restart_required") is True
          and isinstance(j.get("failed_checks"), list) and len(j["failed_checks"]) == 2
          and isinstance(j.get("service"), str))
    check("alert_json_schema", "structured", ok, json.dumps(j)[:150] if j else r["content"][:120])
    note("structured", r)

    r = chat([{"role": "user", "content":
        "Return ONLY a JSON array. Each element must have keys: card (string), raw_price (number), "
        "slab_price (number), spread_pct (number, rounded to 1 decimal).\n"
        "spread_pct = (slab_price - raw_price) / raw_price * 100.\n"
        "Rows:\n"
        "Charizard raw 120.00 slab 300.00\n"
        "Pikachu raw 40.00 slab 55.00\n"
        "Mewtwo raw 80.00 slab 210.00"}], max_tokens=900)
    j = extract_json(r["content"]) or (lambda t: (lambda m: json.loads(m.group(0)) if m else None)(
        re.search(r"\[.*\]", t, re.S)))(r["content"])
    ok = False
    if isinstance(j, list) and len(j) == 3:
        try:
            by = {d["card"].split()[0].lower(): d for d in j}
            ok = (abs(by["charizard"]["spread_pct"] - 150.0) < 0.15
                  and abs(by["pikachu"]["spread_pct"] - 37.5) < 0.15
                  and abs(by["mewtwo"]["spread_pct"] - 162.5) < 0.15)
        except Exception:
            ok = False
    check("arb_spread_json_math", "structured", ok, json.dumps(j)[:150] if j else r["content"][:120])
    note("structured", r)


# ------------------------------------------------------------ D. domain code
def suite_domain():
    print("== domain-code ==")

    sysmsg = {"role": "system", "content":
              "You are an expert Python engineer. Reply with a single ```python code block "
              "containing only the implementation. No example usage, no tests, no explanation."}

    msgs = [sysmsg, {"role": "user", "content":
        "Write parse_journal_rows(rows, header) -> list[dict].\n"
        "`header` is a list of column names. `rows` is a list of lists. "
        "If a row has MORE fields than the header, ignore the extra trailing fields. "
        "If a row has FEWER, fill the missing keys with None. "
        "Skip rows that are entirely empty. Return a list of dicts."}]
    r = chat(msgs, max_tokens=1400)
    code = take_code(r, msgs, 1400)
    test = (
        "h=['ts','action','pnl']\n"
        "rows=[['1','buy','5','EXTRA'],['2','sell'],[],['3','buy','7']]\n"
        "o=parse_journal_rows(rows,h)\n"
        "assert len(o)==3, o\n"
        "assert o[0]=={'ts':'1','action':'buy','pnl':'5'}, o[0]\n"
        "assert o[1]=={'ts':'2','action':'sell','pnl':None}, o[1]\n"
        "assert o[2]=={'ts':'3','action':'buy','pnl':'7'}, o[2]\n"
        "print('ok')\n")
    ok, err = run_code(code, test)
    check("journal_ragged_rows", "domain", ok, err.replace("\n", " "))
    note("domain", r)

    msgs = [sysmsg, {"role": "user", "content":
        "Write bounded_position(equity, price, max_position_pct) -> int.\n"
        "Return the largest whole number of units such that units*price <= equity*(max_position_pct/100). "
        "Return 0 if price <= 0, equity <= 0, or max_position_pct <= 0. Never return a negative number."}]
    r = chat(msgs, max_tokens=1200)
    code = take_code(r, msgs, 1200)
    test = ("assert bounded_position(1000,10,20)==20, bounded_position(1000,10,20)\n"
            "assert bounded_position(1000,300,20)==0, bounded_position(1000,300,20)\n"
            "assert bounded_position(1000,0,20)==0\n"
            "assert bounded_position(-5,10,20)==0\n"
            "assert bounded_position(1000,10,0)==0\n"
            "assert bounded_position(999,10,20)==19, bounded_position(999,10,20)\n"
            "print('ok')\n")
    ok, err = run_code(code, test)
    check("risk_position_cap", "domain", ok, err.replace("\n", " "))
    note("domain", r)

    msgs = [sysmsg, {"role": "user", "content":
        "Write find_orphans(pages, links) -> sorted list of str.\n"
        "`pages` is a set of page names. `links` is a dict mapping page name -> set of page names it links TO. "
        "A page is an orphan if NO other page links to it. A self-link does not count. "
        "Return the sorted list of orphan page names."}]
    r = chat(msgs, max_tokens=1200)
    code = take_code(r, msgs, 1200)
    test = ("p={'a','b','c','d'}\n"
            "l={'a':{'b'},'b':{'b','c'},'c':set(),'d':set()}\n"
            "o=find_orphans(p,l)\n"
            "assert o==['a','d'], o\n"
            "print('ok')\n")
    ok, err = run_code(code, test)
    check("vault_orphan_detect", "domain", ok, err.replace("\n", " "))
    note("domain", r)


# --------------------------------------------------------- R. recall/reason
def suite_recall():
    print("== recall-reason ==")

    r = chat([{"role": "user", "content":
        "A machine has 121 GB of memory shared between CPU and GPU. Currently resident: "
        "a 79 GB memory-mapped weight set, and 16 GB of other processes. "
        "You want to load an additional model needing 21 GB, and you must keep at least 5 GB free. "
        "Answer with ONLY the single word YES or NO on the first line, then one sentence of arithmetic."}],
        max_tokens=700)
    first = r["content"].strip().split("\n")[0].strip().upper().strip(".:*# ")
    ok = first.startswith("NO")
    check("memory_budget_arithmetic", "recall", ok, r["content"][:150].replace("\n", " "))
    note("recall", r)

    r = chat([{"role": "user", "content":
        "Three schedulers exist on one machine: OS cron, systemd timers, and an agent job runner. "
        "A task must run every 4 hours, needs no language model, and must alert a human if it fails. "
        "Which scheduler should it use, and what is the single most common failure mode for such a job? "
        "Answer in two short sentences."}], max_tokens=700)
    c = r["content"].lower()
    ok = (any(k in c for k in ["cron", "systemd", "timer"])
          and "agent" not in c.split(".")[0].lower()
          and any(k in c for k in ["silent", "unnotic", "no alert", "fail quietly", "goes unnoticed",
                                   "environment", "path", "notification"]))
    check("scheduler_layer_choice", "recall", ok, r["content"][:150].replace("\n", " "))
    note("recall", r)

    r = chat([{"role": "user", "content":
        "A benchmark reports a metric that has been exactly 0.00 on every run for three months, "
        "across many different inputs. The code computing it has no reported errors. "
        "In one sentence, what should you conclude and why?"}], max_tokens=600)
    c = r["content"].lower()
    ok = any(k in c for k in ["not being computed", "never computed", "broken", "not measuring",
                              "placeholder", "hardcoded", "hard-coded", "bug", "not actually",
                              "never set", "not wired", "stub"])
    check("metric_always_zero", "recall", ok, r["content"][:150].replace("\n", " "))
    note("recall", r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    print(f"coinupbtc personal eval · base={BASE} · model={MODEL} · "
          f"thinking={'on' if THINKING else 'off'} · maxtok_mult={MULT} · tag={args.tag}")
    try:
        with urllib.request.urlopen(BASE + "/v1/models", timeout=10) as r:
            json.load(r)
    except Exception as e:
        raise SystemExit(f"endpoint down: {BASE} ({e})")

    t0 = time.time()
    suite_ops(); suite_structured(); suite_domain(); suite_recall()

    cats = {}
    for r in RESULTS:
        cats.setdefault(r["cat"], []).append(r["ok"])
    print("\n=== SCORE ===")
    total = 0.0
    for cat, w in WEIGHTS.items():
        got = cats.get(cat, [])
        frac = (sum(got) / len(got)) if got else 0.0
        total += frac * w
        tps = TPS.get(cat, [])
        tp = f"{sum(tps)/len(tps):.1f} tok/s" if tps else "n/a"
        print(f"  {cat:11s} {sum(got)}/{len(got)}  ({frac*100:5.1f}%)  weight {w:.2f}   {tp}")
    print(f"\n  WEIGHTED TOTAL: {total*100:.1f}%   elapsed {time.time()-t0:.0f}s")

    out = os.path.expanduser(f"~/logs/glm52-trial/results/personal-eval-{args.tag}-"
                             f"{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(out, "w") as f:
        json.dump({"tag": args.tag, "base": BASE, "model": MODEL, "thinking": THINKING,
                   "maxtok_mult": MULT, "weighted_total": total, "results": RESULTS}, f, indent=2)
    print(f"  saved: {out}")


if __name__ == "__main__":
    main()
