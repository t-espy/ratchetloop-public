"""The `ratchetloop` command line (CONTRACT.md §1). Exit codes are the disposition's (§6); a task
refused at admit exits 2 with no run directory."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .build_cmds import approve, cmd_build
from .detach import detach
from .report import render, results, stats, status
from .review_only import cmd_review
from .run import PROVIDER_CLI, admit, run_task, runs_root
from .state import DISPOSITIONS, REFUSED_EXIT, Refused
from .trees import cmd_trees


def _not_a_flag(value: str) -> str:
    # `pin.sh --help` became a directory named `--help` (FAILURE_MODES.md §9).
    if value.startswith("-"):
        raise argparse.ArgumentTypeError(f"{value!r} looks like a flag, not a path")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ratchetloop", description=(
        "A task file goes in; a branch, a disposition and a run directory come out."))
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run one task file to a disposition")
    run.add_argument("task", type=_not_a_flag, help="the task file (CONTRACT.md §2)")
    run.add_argument("--coder", choices=sorted(PROVIDER_CLI))
    run.add_argument("--reviewer", choices=sorted(PROVIDER_CLI))
    run.add_argument("--policy", type=Path, help="policy file with model ladders")
    run.add_argument("--check", action="store_true",
                     help="validate the task, policy and CLIs, then exit without running")
    run.add_argument("--continue", dest="continue_", action="store_true",
                     help="pick up the task's branch and worktree after a run that ended short of "
                          "ACCEPT or died (D15)")
    run.add_argument("--detach", action="store_true",
                     help="run in its own session, outliving this command; print where it logs")
    build = sub.add_parser("build", help="take an idea through a plan and its phases (§9)")
    build.add_argument("idea", type=_not_a_flag, help="the idea file (CONTRACT.md §9.1)")
    build.add_argument("--gate", choices=["plan"], help="pause after the plan until approved")
    build.add_argument("--continue", dest="continue_", action="store_true",
                       help="skip the stages already accepted and pick up the first that was not")
    build.add_argument("--policy", type=Path, help="policy file with model ladders")
    build.add_argument("--coder", choices=sorted(PROVIDER_CLI))
    build.add_argument("--reviewer", choices=sorted(PROVIDER_CLI))
    build.add_argument("--detach", action="store_true",
                       help="build in its own session, outliving this command")
    rv = sub.add_parser("review", help="one review round on an existing branch against a base")
    rv.add_argument("task", type=_not_a_flag, help="the task file the branch implements")
    rv.add_argument("--branch", required=True, type=_not_a_flag)
    rv.add_argument("--base", type=_not_a_flag, help="default: the branch's merge-base with the "
                                                     "task's base_branch")
    rv.add_argument("--reviewer", choices=sorted(PROVIDER_CLI))
    rv.add_argument("--policy", type=Path, help="policy file with model ladders")
    st = sub.add_parser("status", help="the latest run of a task or idea: its result, or its "
                                       "last event")
    st.add_argument("key", type=_not_a_flag, help="a task_key or idea_key")
    st.add_argument("--run", dest="run_id", help="a run id instead of the latest")
    sp = sub.add_parser("stats", help="usage and velocity summed from result.json files")
    sp.add_argument("--since", help="only runs started on or after this ISO date or time")
    sp.add_argument("--repo", type=Path, help="only runs on this target repository")
    sp.add_argument("--json", action="store_true")
    approve = sub.add_parser("approve", help="release an idea paused at --gate plan")
    approve.add_argument("idea_key", type=_not_a_flag)
    trees = sub.add_parser("trees", help="list preserved worktrees, or discard one")
    trees.add_argument("--discard", type=_not_a_flag, metavar="PATH")
    trees.add_argument("--yes", action="store_true")
    return parser


def _status(args: argparse.Namespace) -> int:
    found = status(runs_root(), args.key, args.run_id)
    if found is None:
        print(f"ratchetloop: no run of {args.key!r} under {runs_root()}", file=sys.stderr)
        return REFUSED_EXIT
    print(json.dumps(found, indent=1))
    return 0


def _stats(args: argparse.Namespace) -> int:
    report = stats(results(runs_root(), since=args.since, repo=args.repo))
    print(json.dumps(report, indent=1) if args.json else render(report), end="" if not args.json else "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.command == "trees":
        return cmd_trees(args)
    if args.command == "build":
        return cmd_build(args, argv)
    if args.command == "approve":
        return approve(args.idea_key)
    if args.command == "review":
        return cmd_review(args)
    if args.command == "status":
        return _status(args)
    if args.command == "stats":
        return _stats(args)
    try:
        adm = admit(Path(args.task), coder=args.coder, reviewer=args.reviewer,
                    policy_path=args.policy, continue_=args.continue_,
                    dry=args.check or args.detach)
    except Refused as e:
        print(f"ratchetloop: refused: {e}", file=sys.stderr)
        return REFUSED_EXIT
    if args.detach and not args.check:
        return detach(argv, adm.task.task_key, adm.root)
    if args.check:
        print(json.dumps({"ok": True, "task_key": adm.task.task_key, "coder": adm.coder,
                          "reviewer": adm.reviewer,
                          **({"continues": adm.continued_from} if adm.continued_from else {})}))
        return 0
    result = run_task(Path(args.task), adm, policy_path=args.policy)
    print(json.dumps({k: result[k] for k in ("task_key", "run_id", "disposition", "reason",
                                             "branch", "head_sha")}))
    return DISPOSITIONS[result["disposition"]]


if __name__ == "__main__":
    raise SystemExit(main())
