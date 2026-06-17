"""Entry point CLI: tarik data ClickUp -> hitung metrik -> tulis laporan Markdown.

Contoh:
    python -m engineering_productivity --config config.yaml --days 30 --deep -o reports/juni.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import ClickUpClient, ClickUpError
from .config import ConfigError, load_config
from .pipeline import GatherOptions, gather_report
from .report import render_markdown


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engineering_productivity", description=__doc__)
    p.add_argument("--config", default="config.yaml", help="Path ke config.yaml")
    p.add_argument("--since", help="Tanggal mulai YYYY-MM-DD (default: --days lalu)")
    p.add_argument("--until", help="Tanggal akhir YYYY-MM-DD (default: hari ini)")
    p.add_argument("--days", type=int, default=30, help="Lookback hari bila --since kosong (default 30)")
    p.add_argument("--tz", type=float, default=7.0, help="Offset zona waktu untuk bucket minggu (default 7 = WIB)")
    p.add_argument("--deep", action="store_true", help="Ambil time_in_status per task (cycle time & bottleneck; lebih banyak API call)")
    p.add_argument("--max-age", type=int, default=None, metavar="HARI", help="Abaikan task basi: lead time (dibuat->selesai) lebih dari N hari")
    p.add_argument("--no-commits", action="store_true", help="Lewati aktivitas commit sepenuhnya")
    p.add_argument("--commits-source", choices=["auto", "gitlab", "db", "none"], default="auto",
                   help="Sumber commit: gitlab (live API), db (scorecard, bisa basi), auto (gitlab > db), none")
    p.add_argument("--no-discover", action="store_true",
                   help="(GitLab) jangan auto-discover repo per engineer; pakai daftar gitlab.projects saja")
    p.add_argument("--exclude-noise", action="store_true",
                   help="(GitLab) hitung +/- baris tanpa file noise (vendor/lock/generated); ambil diff tiap commit (lambat)")
    p.add_argument("--last-done", action="store_true",
                   help="Tampilkan tanggal task terakhir selesai per engineer (query ekstra lintas periode)")
    p.add_argument("--utilization", action="store_true",
                   help="Analisis engineer underutilized (WIP + story point + skor relatif tim; query open task ekstra)")
    p.add_argument("--last-done-lookback", type=int, default=365, metavar="HARI",
                   help="Batas mundur pencarian last-done (default 365)")
    p.add_argument("-o", "--output", default="reports/report.md", help="File output Markdown")
    p.add_argument("--list-teams", action="store_true", help="Tampilkan workspace/team yang bisa diakses lalu keluar")
    p.add_argument("--list-members", action="store_true", help="Tampilkan member workspace lalu keluar")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2

    client = ClickUpClient(config.token)

    try:
        if args.list_teams:
            for t in client.get_teams():
                print(f"{t['id']}\t{t['name']}")
            return 0

        if args.list_members:
            team_id = client.resolve_team_id(config.team_id)
            for m in sorted(client.get_members(team_id), key=lambda x: (x.get("email") or "")):
                print(f"{m.get('id')}\t{m.get('email')}\t{m.get('username')}")
            return 0

        opts = GatherOptions(
            since=args.since,
            until=args.until,
            days=args.days,
            tz=args.tz,
            deep=args.deep,
            max_age=args.max_age,
            commits_source=args.commits_source,
            no_discover=args.no_discover,
            exclude_noise=args.exclude_noise,
            no_commits=args.no_commits,
            last_done=args.last_done,
            last_done_lookback=args.last_done_lookback,
            utilization=args.utilization,
        )
        data = gather_report(config, opts, client=client, progress=lambda m: print(m, file=sys.stderr))

        now = datetime.now(timezone(timedelta(hours=args.tz)))
        markdown = render_markdown(data, generated_at=now.strftime("%Y-%m-%d %H:%M %Z"))
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown, encoding="utf-8")

        print(f"[✓] Laporan ditulis ke {out_path}")
        print(f"    Total task selesai: {data.total_tasks} | engineer: {len(data.engineers)}")
        return 0

    except ClickUpError as exc:
        print(f"[clickup] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
