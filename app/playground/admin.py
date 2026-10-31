"""Operator CLI for the playground's data directory (no network, works while the server runs):

    python -m app.playground.admin [--dir TRY_DIR] stats
    python -m app.playground.admin [--dir TRY_DIR] expire-now [--all]   # TTL sweep now (--all: every sandbox)
    python -m app.playground.admin [--dir TRY_DIR] purge-expired        # drop expired rows + orphan DB files

A running server notices an expired sandbox on its next request for it (the registry is the
source of truth) and answers "expired".
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import PlaygroundConfig
from .manager import delete_sandbox_files
from .registry import Registry


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.playground.admin")
    p.add_argument("--dir", default=os.environ.get("TRY_DIR") or str(PlaygroundConfig().dir))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    ex = sub.add_parser("expire-now")
    ex.add_argument("--all", action="store_true", help="expire every active sandbox, not only idle ones")
    sub.add_parser("purge-expired")
    args = p.parse_args(argv)
    d = Path(args.dir)
    if not (d / "registry.db").exists():
        print(f"no playground registry in {d}", file=sys.stderr)
        return 1
    reg = Registry(d / "registry.db")
    try:
        if args.cmd == "stats":
            print(json.dumps(reg.stats()))
        elif args.cmd == "expire-now":
            ttl_h = int(os.environ.get("TRY_TTL_HOURS") or 24)
            ids = reg.active_ids() if args.all else reg.idle(time.time() - ttl_h * 3600)
            done = [sid for sid in ids if reg.mark_expired(sid)]
            for sid in done:
                delete_sandbox_files(d, sid)
            print(json.dumps({"expired": len(done)}))
        elif args.cmd == "purge-expired":
            expired = reg.expired_ids()
            for sid in expired:
                delete_sandbox_files(d, sid)
                reg.purge(sid)
            known = set(reg.active_ids())
            orphans = 0
            for f in d.glob("*.db"):
                if f.name != "registry.db" and f.stem not in known:
                    delete_sandbox_files(d, f.stem)
                    orphans += 1
            print(json.dumps({"purged": len(expired), "orphan_files": orphans}))
    finally:
        reg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
