from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

from yachat.policy.compiler import build_file
from yachat.policy.errors import PolicyValidationError
from yachat.policy.schema import policy_json_schema
from yachat.registry.core import REGISTRY


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser()  # prog: the command that ran it (yachat or yachat)
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="validate and compile an agent.yaml")
    b.add_argument("path")
    b.add_argument("--tenant", default="default")
    nw = sub.add_parser("new", help="starter files: an agent, or a capability package with a tool, step and guardrail")
    nw.add_argument("kind", choices=["agent", "package"])
    nw.add_argument("name", help="agent: returns-desk; package: acme_caps")
    nw.add_argument("--out", default=".", help="directory to write into (default: here)")
    sub.add_parser("schema", help="JSON Schema of the policy, generated from the registry")
    sub.add_parser("registry", help="print the registry manifest")
    s = sub.add_parser("serve", help="run the runtime API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    ev = sub.add_parser("evals", help="evaluation harness (DeepEval, Ragas); install yachat[evals]")
    evs = ev.add_subparsers(dest="evals_cmd", required=True)
    evr = evs.add_parser("run", help="run eval sets (default: every evals/<agent>/ here); CI gate")
    evr.add_argument("agent_dirs", nargs="*")
    evc = evs.add_parser("check", help="run eval sets against the checks every conversation must pass (judged ones with EVALS_JUDGE_MODEL)")
    evc.add_argument("agent_dirs", nargs="*")
    evg = evs.add_parser("generate", help="generate an eval set from a policy: one case per target reached, failures shortened")
    evg.add_argument("policy", help="policy YAML")
    evg.add_argument("--dir", default=None, help="eval set folder (default: evals/<agent>/)")
    evg.add_argument("--only", nargs="*", default=None, help="targets whose id starts with these, e.g. rule: handed_off")
    evg.add_argument("--rounds", type=int, default=None, help="tries per target")
    evx = evs.add_parser("export", help="export a live session as a replay test case")
    evx.add_argument("session_id")
    evx.add_argument("--out", default=None, help="replay.yaml to append to (prints when omitted)")
    evx.add_argument("--state-file", default=None, help="JSON dump of the session (from GET /v1/sessions/{id}/state)")
    sim = sub.add_parser("simulate", help="replay scripted conversations against a policy")
    sim.add_argument("policy")
    sim.add_argument("script")
    kn = sub.add_parser("knowledge", help="knowledge index microservice")
    kns = kn.add_subparsers(dest="knowledge_cmd", required=True)
    knv = kns.add_parser("serve", help="run the Knowledge Service API (POST /documents, POST /search)")
    knv.add_argument("--host", default="127.0.0.1")
    knv.add_argument("--port", type=int, default=8001)
    kna = kns.add_parser("add", help="index text/markdown files through a running Knowledge Service")
    kna.add_argument("paths", nargs="+")
    kna.add_argument("--url", default=None, help="service base URL (default: $KNOWLEDGE_URL or http://127.0.0.1:8001)")
    an = sub.add_parser("analytics", help="conversational analytics microservice")
    ans = an.add_subparsers(dest="analytics_cmd", required=True)
    anv = ans.add_parser("serve", help="run the Analytics Service API (POST /datasets, POST /analyze)")
    anv.add_argument("--host", default="127.0.0.1")
    anv.add_argument("--port", type=int, default=8002)
    args = p.parse_args(argv)

    if args.cmd == "build":
        try:
            compiled = build_file(args.path, tenant=args.tenant)
        except PolicyValidationError as e:
            print("policy invalid:", file=sys.stderr)
            for err in e.errors:
                print(f"  {err.path}: {err.message}", file=sys.stderr)
            return 1
        print(json.dumps(compiled.model_dump(mode="json"), indent=2))
        return 0
    if args.cmd == "new":
        from pathlib import Path

        from yachat.scaffold import ScaffoldError, new_agent, new_package

        try:
            written = (new_agent if args.kind == "agent" else new_package)(args.name, Path(args.out))
        except ScaffoldError as e:
            print(e, file=sys.stderr)
            return 1
        for path in written:
            print("created", path)
        if args.kind == "agent":
            print(f"next: yachat build {written[0]}")
        else:
            print(f"next: cd {args.out} && PYTHONPATH=. YACHAT_CAPABILITY_PACKAGES={args.name} yachat build {written[4].name}")
        return 0
    if args.cmd == "schema":
        print(json.dumps(policy_json_schema(), indent=2))
        return 0
    if args.cmd == "registry":
        REGISTRY.discover()
        print(json.dumps(REGISTRY.manifest().model_dump(mode="json"), indent=2))
        return 0
    if args.cmd == "evals":
        from yachat.evals.run import main as evals_main

        if args.evals_cmd in ("run", "check"):
            return evals_main(list(args.agent_dirs), scores=args.evals_cmd == "run")
        if args.evals_cmd == "generate":
            from yachat.evals.generate import main as generate_main

            return generate_main(args.policy, args.dir, args.only, args.rounds)
        from yachat.evals.online import export_replay
        from yachat.state.models import SessionState

        if not args.state_file:
            print("provide --state-file with the session JSON (GET /v1/sessions/{id}/state)", file=sys.stderr)
            return 2
        import yaml

        state = SessionState.model_validate_json(open(args.state_file, encoding="utf-8").read())
        case = export_replay(state, name=f"live-{args.session_id}")
        if args.out:
            data = yaml.safe_load(open(args.out, encoding="utf-8").read()) or {"cases": []}
            data.setdefault("cases", []).append(case)
            with open(args.out, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, sort_keys=False)
            print(f"appended {case['name']} to {args.out}")
        else:
            print(yaml.safe_dump({"cases": [case]}, sort_keys=False))
        return 0
    if args.cmd == "simulate":
        import asyncio

        from yachat.sim.harness import run_script_file

        report = asyncio.run(run_script_file(args.policy, args.script))
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("yachat.api.app:create_app", factory=True, host=args.host, port=args.port)
        return 0
    if args.cmd == "analytics":
        import uvicorn

        uvicorn.run("yachat.analytics.api:create_app", factory=True, host=args.host, port=args.port)
        return 0
    if args.cmd == "knowledge":
        if args.knowledge_cmd == "serve":
            import uvicorn

            uvicorn.run("yachat.knowledge.api:create_app", factory=True, host=args.host, port=args.port)
            return 0
        import os
        from pathlib import Path

        import httpx

        url = (args.url or os.getenv("KNOWLEDGE_URL") or "http://127.0.0.1:8001").rstrip("/")
        docs = [{"title": Path(x).stem, "text": Path(x).read_text(encoding="utf-8"), "source": str(x)} for x in args.paths]
        try:
            headers = {"authorization": f"Bearer {k}"} if (k := os.getenv("YACHAT_API_KEY")) else {}
            r = httpx.post(url + "/documents", json={"documents": docs}, timeout=300, headers=headers)
        except httpx.HTTPError as e:
            print(f"{url}: {e.__class__.__name__}: {e}", file=sys.stderr)
            return 1
        if r.status_code != 201:
            print(f"{url}/documents -> {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return 1
        for d in r.json():
            print(f"{d['id']}  {d['chunks']:>4} chunks  {d['title']}")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
