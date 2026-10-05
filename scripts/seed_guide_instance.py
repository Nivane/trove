"""Seed an isolated docs-capture instance with real, screenshot-worthy state.

This is the recipe behind ``frontend/scripts/capture-guide-shots.mjs``: it
fills an **isolated** Trove instance (its own ``home`` + its own project
``.trove/``, never the user's ``~/.trove``) with the sessions, users, jobs,
subscriptions, decision verdicts and action proposals the guide screenshots
show. ``scripts/guide_instance.example.yml`` is the matching config (copy it,
fix the ``home`` path, start ``trove serve --config`` from the instance dir).

Chat turns are real LLM calls; everything else (users/jobs/subscriptions/
skills/templates/ratings/decision runs) goes through the product's own HTTP
API and is zero-LLM. Re-running is safe: each step is keyed on something
stable (username, job name, question, template name) and skips what exists.

  uv run python scripts/seed_guide_instance.py --base http://127.0.0.1:8000

Steps that call the LLM (the seeded chat turns) are skipped when the target
``docs-guide`` user already has sessions — pass ``--reset-chat`` to wipe and
re-ask them.

One step edits a file rather than calling the API: the demo KB's
``decisions.yml`` gets an ``action:`` block wired to the confirmed
``loan-baseline-alert`` template (text insertion, so the rest of the file —
and the rule-file editor screenshot — stays byte-identical). Without it the
decision job would fire but never propose, and the 行动 screenshots would be
empty.

A **seed snapshot** (``--snapshot-out`` writes one, ``--restore`` applies one)
freezes what the LLM produced: the registered datasources' KB YAMLs
(``root/.trove/kb/``) plus the seeded chat/memory stores (``home/``). The
committed snapshot under ``scripts/guide_snapshot/`` carries the KB YAMLs and
``seed.json`` only — the session/memory stores are private instance state and
stay out of the repo. To restore: copy the snapshot onto a fresh instance
**while the backend is stopped**, start ``trove serve`` from the instance
dir, then re-run this script without ``--restore`` — the chat step is skipped
because sessions exist, and everything else is zero-LLM.

State the screenshots need, and where it comes from:

  ✓ 4 user sessions (hero + follow-up + 3 side)   — LLM (skippable)
  ✓ 2 org users + grants                          — API
  ✓ pending lesson from the hero upvote           — API (rating)
  ✓ 2 skills (1 confirmed / 1 pending)            — API
  ✓ 2 jobs + run history                          — API
  ✓ subscriptions (每期 / 仅告警) + deliveries     — API
  ✓ ≥2 verdicts (zero-LLM decision runs)          — API (job run)
  ✓ 1 pending action proposal (rule action ref)   — decision run + template
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import httpx

# Curated questions, picked because these paths actually work on the
# financial KB (verified by probe): zh + metric Chinese synonym → semantic
# compile; en + KB template phrasing → exact KB reuse (and auto-chart).
#
# NOT here: metadata-intent questions ("loan 表有哪些字段?"). They can trip
# an unbounded answer_metadata ↔ metadata_check loop (stale error_feedback
# never cleared; ~one real LLM call per round). See the memory-loop-runaway
# regression test.
HERO_Q = "贷款金额最高是多少?"
HERO_Q2 = "How many loan records are there for each Loan status code (A, B, D)?"
SIDE_QS = [
    "How many records are in the account table?",
    "How many records are in the client table?",
    "How many records are in the disp table?",
]

# throwaway credentials for the isolated instance only (created below if
# missing; overridable via CLI flags)
ADMIN = ("admin", "Trove-Guide-2026")
GUIDE = ("docs-guide", "Guide-2026-pass")
GUIDE2 = ("docs-user", "User-2026-pass")

# The decision job the 行动/订阅/判定历史 screenshots center on.
DECISION_JOB = "贷款总额基线巡检"
CRON_JOB = "每周贷款金额巡检"
DECISION_RULE = "loan-high"
ACTION_TEMPLATE = "loan-baseline-alert"
ACTION_TEMPLATE_BODY = {
    "title": "贷款总额基线告警 · 运营通知",
    "description": "判定触发时通知运营值班：当期/基期/变化量 + 触发消息原文。",
    "action_type": "notify",
    "target": {"channel": "ops-alerts", "resource": "#ops-alerts"},
    "risk": "medium",
    "payload_template": (
        '{"rule":"{{rule_id}}","metric":"{{metric}}","datasource":"{{datasource}}",'
        '"current":"{{current}}","baseline":"{{baseline}}","delta":"{{delta}}",'
        '"message":"{{message}}","anchor_date":"{{anchor_date}}"}'
    ),
}

# The MySQL BIRD datasource the seeded chats were asked against. Registered
# before demo so it becomes the instance default (registration order is the
# default semantics); its KB ships in the repo/snapshot, not from kb init.
FINANCIAL_URL = "mysql://root:root@127.0.0.1:3306/financial"

# Snapshot layout mirrors the instance: KB YAMLs under root/, stores under
# home/. SNAPSHOT_HOME entries are the LLM-bearing / seed-run by-products —
# written by --snapshot-out and applied by --restore, but not committed to
# the repo (private instance state).
SNAPSHOT_KB = ("demo", "financial")
SNAPSHOT_HOME = ("sessions.sqlite", "user_facts.db",
                 "memory/episodes.sqlite", "memory/preferences.sqlite")


def login(base: str, username: str, password: str) -> str:
    r = httpx.post(f"{base}/v1/auth/login",
                   json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["token"]


def H(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def ensure_user(base: str, adm: str, username: str, password: str,
                role: str, display: str) -> dict:
    r = httpx.get(f"{base}/v1/admin/users", params={"q": username}, headers=H(adm))
    r.raise_for_status()
    hit = next((u for u in r.json()["users"] if u["username"] == username), None)
    if hit:
        httpx.patch(f"{base}/v1/admin/users/{hit['id']}",
                    json={"password": password, "role": role, "display_name": display},
                    headers=H(adm)).raise_for_status()
        return hit
    r = httpx.post(f"{base}/v1/admin/users",
                   json={"username": username, "password": password,
                         "role": role, "display_name": display},
                   headers=H(adm))
    r.raise_for_status()
    return r.json()


def ask(base: str, tok: str, question: str, session_id: str | None,
        datasource: str = "financial") -> dict:
    """One real chat turn (SSE); returns the done summary."""
    body = {"question": question, "datasource": datasource}
    if session_id:
        body["session_id"] = session_id
    with httpx.Client(timeout=300) as c:
        with c.stream("POST", f"{base}/v1/chat", headers=H(tok), json=body) as r:
            r.raise_for_status()
            raw = "".join(r.iter_text())
    events = []
    for block in raw.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if name and data is not None:
            try:
                events.append((name, json.loads(data)))
            except json.JSONDecodeError:
                pass
    done = next((d for n, d in events if n == "done"), {})
    s = done.get("summary") or {}
    print(f"    · {question[:52]:<54} source={s.get('answer_source') or '-':<9} "
          f"rows={s.get('row_count')} chart={'y' if s.get('chart') else 'n'}")
    return s


def ensure_action_template(base: str, adm: str) -> None:
    """Create + confirm the loan-baseline-alert template (idempotent)."""
    r = httpx.get(f"{base}/v1/admin/actions/templates", headers=H(adm))
    r.raise_for_status()
    existing = next((t for t in r.json().get("templates", [])
                     if t.get("name") == ACTION_TEMPLATE), None)
    if existing is None:
        r = httpx.post(f"{base}/v1/admin/actions/templates", headers=H(adm),
                       json={"name": ACTION_TEMPLATE, **ACTION_TEMPLATE_BODY})
        r.raise_for_status()
        print("action template:", r.status_code, r.json().get("status"))
    if (existing or {}).get("status") != "confirmed":
        r = httpx.post(
            f"{base}/v1/admin/actions/templates/{ACTION_TEMPLATE}/confirm",
            headers=H(adm))
        r.raise_for_status()
        print("action template confirm:", r.status_code)


def wire_rule_action(project_root: Path) -> None:
    """Point the demo loan-high rule at the confirmed template.

    Text insertion after the rule's ``conditions:`` block — a YAML round-trip
    would reformat the whole file, and this file is also the subject of the
    rule-editor screenshot.
    """
    decisions = project_root / ".trove" / "kb" / "demo" / "decisions.yml"
    text = decisions.read_text(encoding="utf-8")
    if ACTION_TEMPLATE in text:
        print("rule action ref: already present")
        return
    anchor = "  conditions:\n  - delta > 0\n"
    if anchor not in text:
        sys.exit(f"cannot wire action: anchor not found in {decisions}")
    block = (
        "  action:\n"
        f"    template: {ACTION_TEMPLATE}\n"
        "    autonomy: propose\n"
    )
    decisions.write_text(text.replace(anchor, anchor + block, 1), encoding="utf-8")
    print(f"rule action ref: wired into {decisions}")


def ensure_subscriptions(base: str, adm: str, job_id: str) -> None:
    """docs-guide 每期 / docs-user 仅告警, on the decision job."""
    wanted = [("docs-guide", "always"), ("docs-user", "alert_only")]
    r = httpx.get(f"{base}/v1/admin/subscriptions", headers=H(adm))
    r.raise_for_status()
    have = {(s.get("subscriber"), s.get("job_id")) for s in r.json().get("subscriptions", [])}
    for subscriber, mode in wanted:
        if (subscriber, job_id) in have:
            print(f"subscription: {subscriber} already present")
            continue
        r = httpx.post(f"{base}/v1/admin/jobs/{job_id}/subscriptions", headers=H(adm),
                       json={"subscriber": subscriber, "channel": "", "mode": mode})
        r.raise_for_status()
        print(f"subscription: {subscriber} ({mode})", r.status_code)


def write_snapshot(dest: Path, project_root: Path, home: Path) -> None:
    """Freeze the LLM-produced instance state into ``dest`` (mirrored layout)."""
    for ds in SNAPSHOT_KB:
        src = project_root / ".trove" / "kb" / ds
        out = dest / "root" / ".trove" / "kb" / ds
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(src, out,
                        ignore=shutil.ignore_patterns(".locks", ".generated"))
        print(f"snapshot: kb/{ds} ({len(list(out.glob('*.yml')))} yml)")
    for rel in SNAPSHOT_HOME:
        src, out = home / rel, dest / "home" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        print(f"snapshot: home/{rel} ({src.stat().st_size} B)")


def restore_snapshot(src: Path, project_root: Path, home: Path | None) -> None:
    """Apply a snapshot onto the instance (backend must be stopped first)."""
    for ds in SNAPSHOT_KB:
        s = src / "root" / ".trove" / "kb" / ds
        if not s.exists():
            print(f"restore: kb/{ds} — not in snapshot, skipped")
            continue
        out = project_root / ".trove" / "kb" / ds
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(s, out, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".locks", ".generated"))
        print(f"restore: kb/{ds} → {out}")
    for rel in SNAPSHOT_HOME:
        s = src / "home" / rel
        if not s.exists():
            print(f"restore: home/{rel} — not in snapshot, skipped")
            continue
        if home is None:
            sys.exit("--restore needs --home to place home/ stores")
        out = home / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, out)
        print(f"restore: home/{rel} → {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--project-root", default=".",
                    help="dir holding the instance's .trove/ (default: cwd)")
    ap.add_argument("--reset-chat", action="store_true",
                    help="wipe docs-guide sessions and re-ask the curated questions (LLM)")
    ap.add_argument("--decision-runs", type=int, default=3,
                    help="how many zero-LLM decision runs to record (verdict history)")
    ap.add_argument("--out", default="", help="write a seed.json log to this path")
    ap.add_argument("--home", default="",
                    help="instance home dir (required by --snapshot-out / --restore)")
    ap.add_argument("--financial-url", default=FINANCIAL_URL,
                    help="MySQL BIRD datasource URL (default: local BIRD export)")
    ap.add_argument("--snapshot-out", default="",
                    help="after seeding, write a seed snapshot (KB + chat/memory "
                         "stores) to this dir; needs --home")
    ap.add_argument("--restore", default="",
                    help="apply a seed snapshot to the instance and exit; run with "
                         "the backend STOPPED, then start it and re-run without "
                         "--restore")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    project_root = Path(args.project_root).resolve()

    if args.restore:
        print("restoring — the backend must not be running while home/ stores "
              "are replaced")
        restore_snapshot(Path(args.restore).expanduser().resolve(), project_root,
                         Path(args.home).expanduser().resolve() if args.home else None)
        print("\nrestore done. Start the backend on this instance, then re-run "
              "this script WITHOUT --restore: chat turns are skipped and every "
              "remaining step is zero-LLM.")
        return

    log: dict = {}
    adm = login(base, *ADMIN)

    # 0. the MySQL BIRD datasource the seeded chats target — register before
    #    demo so it becomes the default (registration order = default
    #    semantics). Its KB comes from the snapshot/repo, not from kb init.
    have = {d.get("name") for d in
            httpx.get(f"{base}/v1/admin/datasources", headers=H(adm))
            .json().get("datasources", [])}
    if "financial" in have:
        print("financial datasource: already present")
    else:
        r = httpx.post(f"{base}/v1/admin/datasources",
                       json={"name": "financial", "url": args.financial_url},
                       headers=H(adm))
        print("financial datasource:", r.status_code,
              r.json().get("datasource", {}).get("status")
              if r.status_code < 400 else r.text[:160])

    # 1. demo datasource (its KB ships in the repo with decisions.yml; also
    #    gives the datasources page a second row and the decisions page content).
    r = httpx.post(f"{base}/v1/admin/datasources", json={"name": "demo", "url": "demo"},
                   headers=H(adm))
    print("demo datasource:", r.status_code,
          r.json().get("datasource", {}).get("status") if r.status_code < 400 else r.text[:160])

    # 2. two org users (analyst + plain user) for the users page and grants
    g1 = ensure_user(base, adm, *GUIDE, role="analyst", display="陈晓")
    g2 = ensure_user(base, adm, *GUIDE2, role="user", display="李然")
    print("users:", g1["id"], g1["role"], "|", g2["id"], g2["role"])
    for uid in (g1["id"], g2["id"]):
        httpx.put(f"{base}/v1/admin/users/{uid}/datasources",
                  json={"datasources": ["financial", "demo"]},
                  headers=H(adm)).raise_for_status()

    # 3. chat history — the paid step, skipped when it already exists
    tok = login(base, *GUIDE)
    sessions = httpx.get(f"{base}/v1/sessions", headers=H(tok)).json().get("sessions", [])
    if args.reset_chat:
        for s in sessions:
            httpx.delete(f"{base}/v1/sessions/{s['session_id']}", headers=H(tok))
        print(f"cleared {len(sessions)} old sessions")
        sessions = []
    if sessions:
        print(f"chat: reusing {len(sessions)} existing sessions (--reset-chat to re-ask)")
        # replay state: the upvote only needs the question text
        hero_sql, hero_run = "", ""
    else:
        print("seeding chat turns (real LLM):")
        hero = ask(base, tok, HERO_Q, None)
        sid = hero.get("session_id")
        if not sid:
            sys.exit("hero turn returned no session_id — check the backend run log "
                     "before re-running")
        log["hero_session"] = sid
        ask(base, tok, HERO_Q2, sid)
        for q in SIDE_QS:
            s = ask(base, tok, q, None)
            log.setdefault("side_sessions", []).append(s.get("session_id"))
        log["hero"] = {k: hero.get(k) for k in ("run_id", "sql", "row_count")}
        hero_sql, hero_run = hero.get("sql", ""), hero.get("run_id", "")

    # 4. one honest upvote on the hero answer → pending lesson (the KB pending
    #    queue shows 来源=用户投票) + promotion evidence when memory is on
    r = httpx.post(f"{base}/v1/kb/ratings",
                   json={"question": HERO_Q, "vote": 1,
                         "note": "口径清楚，直接复用了指标定义",
                         "sql_snippet": hero_sql[:400], "run_id": hero_run},
                   headers=H(tok))
    print("upvote:", r.status_code)

    # 5. two scheduled jobs; the decision one runs through the deterministic
    #    engine (zero LLM) and fills run/verdict/delivery history
    mine = {CRON_JOB, DECISION_JOB}
    existing = httpx.get(f"{base}/v1/admin/jobs", headers=H(adm)).json().get("jobs", [])
    by_name = {j.get("name"): j for j in existing}
    for j in existing:
        if j.get("name") in mine and args.reset_chat:
            httpx.delete(f"{base}/v1/admin/jobs/{j['id']}", headers=H(adm))
            by_name.pop(j.get("name"), None)
            print(f"  removed old job {j['name']}")
    if CRON_JOB not in by_name:
        r = httpx.post(f"{base}/v1/admin/jobs", headers=H(adm), json={
            "name": CRON_JOB, "question": HERO_Q, "datasource": "financial",
            "schedule_type": "cron", "schedule": "0 9 * * 1", "alert_channel": "console",
        })
        r.raise_for_status()
        by_name[CRON_JOB] = r.json()["job"]
    if DECISION_JOB not in by_name:
        r = httpx.post(f"{base}/v1/admin/jobs", headers=H(adm), json={
            "name": DECISION_JOB, "question": "贷款总额是多少?", "datasource": "demo",
            "schedule_type": "interval", "schedule": "1440",
            "decision_rule": DECISION_RULE, "alert_channel": "console",
        })
        r.raise_for_status()
        by_name[DECISION_JOB] = r.json()["job"]
    for name in (CRON_JOB, DECISION_JOB):
        print(f"job {name}: {by_name[name]['id']}")
    log["jobs"] = {n: by_name[n]["id"] for n in (CRON_JOB, DECISION_JOB)}

    # 6. org skills: one confirmed (available tier), one left pending so the
    #    page shows both gate states
    skill_body = (
        "## 何时使用\n\n回答涉及金额、利率、期限的指标时,先确认口径三件事:\n\n"
        "1. **聚合口径**:平均值还是总额? 平均看单笔风险,总额看规模。\n"
        "2. **时间口径**:按发放日期还是记账日期? 跨年对比时两者会差一个批次。\n"
        "3. **状态口径**:是否包含已结清/违约(D)状态的记录?\n\n"
        "## 输出约定\n\n结论先给数,再给口径说明;口径不明确时主动说明选择了哪一种。\n"
    )
    r1 = httpx.post(f"{base}/v1/admin/skills/draft", headers=H(adm), json={
        "name": "metric-caliber-check", "tier": "available", "lang": "zh",
        "description": "回答金额类指标前先确认聚合/时间/状态三项口径",
        "triggers": {"node": "gen_sql"}, "body": skill_body,
    })
    print("skill draft (mutually idempotent):", r1.status_code)
    r2 = httpx.post(f"{base}/v1/admin/skills/draft", headers=H(adm), json={
        "name": "answer-structure-zh", "tier": "available", "lang": "zh",
        "description": "中文回答的结构约定:结论 → 数据 → 口径 → 下一步",
        "triggers": {}, "body": "结论前置,一句话给答案;数据用表格;口径用一行小字;"
                               "最后给一条可继续追问的方向。",
    })
    if r2.status_code < 400:
        name = r2.json().get("name")
        rc = httpx.post(f"{base}/v1/admin/skills/{name}/confirm", headers=H(adm))
        print("skill confirm:", rc.status_code)

    # 7. action layer state — template confirmed first, then the rule may
    #    reference it (the template gate is what makes the ref legitimate)
    ensure_action_template(base, adm)
    wire_rule_action(project_root)

    # 8. subscriptions + zero-LLM decision runs: each run re-judges the rule
    #    (verdict history), delivers to subscribers, and the first firing
    #    creates the pending proposal (later runs dedupe on the idempotency
    #    key — that dedup is itself the correct, visible behavior)
    dj = by_name[DECISION_JOB]["id"]
    ensure_subscriptions(base, adm, dj)
    for i in range(args.decision_runs):
        rr = httpx.post(f"{base}/v1/admin/jobs/{dj}/run", headers=H(adm), timeout=300)
        print(f"  decision run {i + 1}: {rr.status_code}")

    # 9. state summary for the capture run
    pend_ex = httpx.get(f"{base}/v1/kb/examples/pending", headers=H(adm)).json()
    lessons = httpx.get(f"{base}/v1/kb/lessons", headers=H(adm)).json()
    props = httpx.get(f"{base}/v1/admin/actions/proposals", headers=H(adm)).json()
    subs = httpx.get(f"{base}/v1/admin/subscriptions", headers=H(adm)).json()
    sessions = httpx.get(f"{base}/v1/sessions", headers=H(tok)).json().get("sessions", [])
    print("\nstate:")
    print("  sessions:", len(sessions))
    print("  pending examples:", len(pend_ex.get("examples", pend_ex.get("pending", []))))
    print("  lessons:", len(lessons.get("lessons", [])))
    print("  proposals:", len(props.get("proposals", [])))
    print("  subscriptions:", len(subs.get("subscriptions", [])))
    log["state"] = {
        "sessions": len(sessions),
        "pending_examples": len(pend_ex.get("examples", [])),
        "lessons": len(lessons.get("lessons", [])),
        "proposals": len(props.get("proposals", [])),
    }
    if args.out:
        Path(args.out).write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n")
    if args.snapshot_out:
        if not args.home:
            sys.exit("--snapshot-out needs --home (the instance's home dir)")
        dest = Path(args.snapshot_out).expanduser().resolve()
        dest.mkdir(parents=True, exist_ok=True)
        write_snapshot(dest, project_root, Path(args.home).expanduser().resolve())
        (dest / "seed.json").write_text(
            json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"snapshot written to {dest}")
    print("seeded at", time.strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    sys.exit(main())
