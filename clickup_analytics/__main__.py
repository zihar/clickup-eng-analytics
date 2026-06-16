"""Entry point CLI: tarik data ClickUp -> hitung metrik -> tulis laporan Markdown.

Contoh:
    python -m clickup_analytics --config config.yaml --days 30 --deep -o reports/juni.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .client import ClickUpClient, ClickUpError
from .config import Config, ConfigError, load_config
from .db import DBError, fetch_commit_stats
from .metrics import build_report_data
from .report import render_markdown


def parse_date(text: str, tz_offset: float, *, end_of_day: bool = False) -> int:
    tz = timezone(timedelta(hours=tz_offset))
    dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=tz)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp() * 1000)


def resolve_targets(config: Config, members: list[dict]) -> tuple[set[int], dict[int, str]]:
    """Petakan engineer di config -> id ClickUp + nama tampilan."""
    email_to_member = {(m.get("email") or "").lower(): m for m in members}
    target_ids: set[int] = set()
    id_to_name: dict[int, str] = {}
    unresolved: list[str] = []

    for eng in config.engineers:
        uid = eng.id
        if uid is None and eng.email:
            member = email_to_member.get(eng.email.lower())
            if member:
                uid = member.get("id")
        if uid is None:
            unresolved.append(eng.name)
            continue
        target_ids.add(uid)
        id_to_name[uid] = eng.name

    if unresolved:
        print(
            f"[!] Engineer berikut tidak ketemu di workspace (cek email/id): {', '.join(unresolved)}",
            file=sys.stderr,
        )
    return target_ids, id_to_name


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clickup_analytics", description=__doc__)
    p.add_argument("--config", default="config.yaml", help="Path ke config.yaml")
    p.add_argument("--since", help="Tanggal mulai YYYY-MM-DD (default: --days lalu)")
    p.add_argument("--until", help="Tanggal akhir YYYY-MM-DD (default: hari ini)")
    p.add_argument("--days", type=int, default=30, help="Lookback hari bila --since kosong (default 30)")
    p.add_argument("--tz", type=float, default=7.0, help="Offset zona waktu untuk bucket minggu (default 7 = WIB)")
    p.add_argument("--deep", action="store_true", help="Ambil time_in_status per task (cycle time & bottleneck; lebih banyak API call)")
    p.add_argument("--max-age", type=int, default=None, metavar="HARI", help="Abaikan task basi: lead time (dibuat->selesai) lebih dari N hari")
    p.add_argument("--no-commits", action="store_true", help="Lewati aktivitas commit GitLab dari DB squad-scorecard")
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

        team_id = client.resolve_team_id(config.team_id)
        members = client.get_members(team_id)

        if args.list_members:
            for m in sorted(members, key=lambda x: (x.get("email") or "")):
                print(f"{m.get('id')}\t{m.get('email')}\t{m.get('username')}")
            return 0

        target_ids, id_to_name = resolve_targets(config, members)
        if not target_ids:
            print("[!] Tidak ada engineer yang ter-resolve. Periksa config.yaml.", file=sys.stderr)
            return 2

        now = datetime.now(timezone(timedelta(hours=args.tz)))
        until_str = args.until or now.strftime("%Y-%m-%d")
        if args.since:
            since_str = args.since
        else:
            since_str = (now - timedelta(days=args.days)).strftime("%Y-%m-%d")

        date_done_gt = parse_date(since_str, args.tz)
        date_done_lt = parse_date(until_str, args.tz, end_of_day=True)

        print(f"[*] Menarik task {len(target_ids)} engineer, {since_str} s/d {until_str} ...")
        tasks = list(
            client.iter_team_tasks(
                team_id,
                assignee_ids=sorted(target_ids),
                date_done_gt=date_done_gt,
                date_done_lt=date_done_lt,
            )
        )
        print(f"[*] {len(tasks)} task selesai ditemukan.")

        time_in_status = None
        if args.deep:
            time_in_status = {}
            print(f"[*] Mode --deep: mengambil riwayat status {len(tasks)} task ...")
            for i, task in enumerate(tasks, 1):
                try:
                    time_in_status[task["id"]] = client.get_time_in_status(task["id"])
                except ClickUpError as exc:
                    print(f"    [!] gagal time_in_status {task['id']}: {exc}", file=sys.stderr)
                if i % 25 == 0:
                    print(f"    ... {i}/{len(tasks)}")

        print("[*] Menarik time entries ...")
        try:
            time_entries = list(
                client.iter_time_entries(
                    team_id,
                    start_date=date_done_gt,
                    end_date=date_done_lt,
                    assignee_ids=sorted(target_ids),
                )
            )
        except ClickUpError as exc:
            # Membaca time entry orang lain butuh izin admin/owner workspace.
            # Kalau token tak punya akses, lewati metrik time-tracked (bukan fatal).
            print(
                f"    [!] Time entries dilewati (metrik 'time tracked' kosong): {exc}",
                file=sys.stderr,
            )
            time_entries = []

        commit_stats = None
        if config.db_dsn and not args.no_commits:
            print("[*] Menarik aktivitas commit dari DB squad-scorecard ...")
            try:
                commit_stats = fetch_commit_stats(
                    config.db_dsn, sorted(target_ids), since_str, until_str
                )
            except DBError as exc:
                print(f"    [!] Commit dilewati (DB tak terjangkau): {exc}", file=sys.stderr)
                commit_stats = None

        data = build_report_data(
            tasks,
            id_to_name=id_to_name,
            target_ids=target_ids,
            time_in_status=time_in_status,
            time_entries=time_entries,
            since=since_str,
            until=until_str,
            tz_offset=args.tz,
            max_age_days=args.max_age,
            commit_stats=commit_stats,
        )

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
