"""synth CLI. `sync` is wired now; analyze/report land in the follow-on plan."""

from __future__ import annotations

import argparse
import sys

from config import get_settings
from ingest.sheet import sync_sheet
from ingest.strava import sync_strava
from store import db


def _cmd_sync(args: argparse.Namespace) -> int:
    s = get_settings()
    print("config:", s.safe_summary())  # redacted — never prints secrets
    conn = db.connect(s.synth_db_path)
    db.init_db(conn)
    synced_any = False
    if s.strava_client_id and s.strava_client_secret:
        n = sync_strava(s, conn, force_refresh=args.refresh)
        print(f"strava: synced {n} activities")
        synced_any = True
    else:
        print("strava: skipped (STRAVA_CLIENT_ID/STRAVA_CLIENT_SECRET not set)")
    if s.sheet_activities_path is not None:
        n = sync_sheet(s, conn)
        print(f"sheet: synced {n} activities")
        synced_any = True
    else:
        print("sheet: skipped (SHEET_ACTIVITIES_PATH not set)")
    if not synced_any:
        print("nothing to sync: set Strava creds and/or SHEET_ACTIVITIES_PATH in .env")
        return 1
    print(f"db now holds {db.count_activities(conn)} activities")
    return 0


def _cmd_stub(name: str):
    def run(_args: argparse.Namespace) -> int:
        print(f"`{name}` arrives in the follow-on plan (sheet/join/metrics/agent).")
        return 0
    return run


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="synth")
    sub = p.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="pull configured sources (Strava, sheet) into SQLite")
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
