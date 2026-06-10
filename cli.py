"""synth CLI. `sync` is wired now; analyze/report land in the follow-on plan."""

from __future__ import annotations

import argparse
import sys

from config import get_settings
from ingest.strava import sync_strava
from store import db


def _cmd_sync(args: argparse.Namespace) -> int:
    s = get_settings()
    print("config:", s.safe_summary())  # redacted — never prints secrets
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    n = sync_strava(s, conn, force_refresh=args.refresh)
    print(f"synced {n} Strava activities; db now holds {db.count_activities(conn)}")
    return 0


def _cmd_stub(name: str):
    def run(_args: argparse.Namespace) -> int:
        print(f"`{name}` arrives in the follow-on plan (sheet/join/metrics/agent).")
        return 0
    return run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="synth")
    sub = p.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="pull Strava (+ sheet, later) into SQLite")
    sync.add_argument("--refresh", action="store_true",
                      help="force a token refresh before syncing")
    sync.set_defaults(func=_cmd_sync)

    for name in ("analyze", "report"):
        sub.add_parser(name).set_defaults(func=_cmd_stub(name))

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
