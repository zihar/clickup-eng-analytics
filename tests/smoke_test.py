"""Smoke test offline: validasi metrik & render Markdown tanpa memanggil API.

Jalankan: ./.venv/bin/python tests/smoke_test.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engineering_productivity.db import CommitStats
from engineering_productivity.gitlab import (
    DEFAULT_NOISE_PATTERNS,
    clean_diff_stats,
    discover_project_ids,
    is_noise,
)
from engineering_productivity.gitlab import fetch_commit_stats as gl_fetch_commit_stats
from engineering_productivity.config import Config, Engineer, GitlabConfig
from engineering_productivity.metrics import build_report_data
from engineering_productivity.pipeline import GatherOptions, resolve_commit_source, resolve_targets
from engineering_productivity.report import render_markdown

# Dua engineer dummy.
ID_BUDI, ID_SARI = 101, 202
id_to_name = {ID_BUDI: "Budi", ID_SARI: "Sari"}

# date_created / date_done dalam epoch ms (string, seperti respons ClickUp asli).
DAY = 86_400_000
BASE = 1_716_000_000_000  # ~Mei 2024

tasks = [
    {
        "id": "t1",
        "date_created": str(BASE),
        "date_done": str(BASE + 2 * DAY),
        "time_estimate": str(8 * 3_600_000),  # 8 jam
        "assignees": [{"id": ID_BUDI, "email": "budi@x.com"}],
    },
    {
        "id": "t2",
        "date_created": str(BASE + 3 * DAY),
        "date_done": str(BASE + 4 * DAY),
        "time_estimate": str(4 * 3_600_000),
        "assignees": [{"id": ID_BUDI}, {"id": ID_SARI}],  # shared credit
    },
    {
        "id": "t3",
        "date_created": str(BASE + 10 * DAY),
        "date_done": str(BASE + 15 * DAY),
        "time_estimate": "0",
        "assignees": [{"id": ID_SARI}],
    },
    {
        # Task belum selesai -> harus diabaikan.
        "id": "t4",
        "date_created": str(BASE),
        "date_done": None,
        "assignees": [{"id": ID_BUDI}],
    },
]

time_in_status = {
    "t1": {
        # Status terminal dengan durasi besar -> HARUS dikecualikan dari bottleneck.
        "current_status": {"status": "done", "type": "closed", "total_time": {"by_minute": 99999}},
        "status_history": [
            {"status": "to do", "type": "open", "total_time": {"by_minute": 1440}},      # 1 hari
            {"status": "in progress", "type": "custom", "total_time": {"by_minute": 2880}},  # 2 hari aktif
            {"status": "review", "type": "custom", "total_time": {"by_minute": 720}},     # 0.5 hari
        ],
    },
    "t2": {
        "current_status": {"status": "done", "type": "closed", "total_time": {"by_minute": 0}},
        "status_history": [
            {"status": "in progress", "type": "custom", "total_time": {"by_minute": 1440}},
        ],
    },
    "t3": {
        "current_status": {"status": "done", "type": "closed", "total_time": {"by_minute": 0}},
        "status_history": [
            {"status": "review", "type": "custom", "total_time": {"by_minute": 5760}},  # 4 hari nyangkut di review
        ],
    },
}

time_entries = [
    {"user": {"id": ID_BUDI}, "duration": str(10 * 3_600_000)},  # 10 jam
    {"user": {"id": ID_SARI}, "duration": str(6 * 3_600_000)},
    {"user": {"id": ID_SARI}, "duration": "-5000"},  # timer berjalan / negatif -> diabaikan
    {"user": {"id": 999}, "duration": str(3_600_000)},  # bukan target -> diabaikan
]

commit_stats = {
    ID_BUDI: CommitStats(commits=20, additions=100, deletions=10, active_days=5, repos=2),
    999: CommitStats(commits=99),  # bukan target -> diabaikan
}

data = build_report_data(
    tasks,
    id_to_name=id_to_name,
    target_ids={ID_BUDI, ID_SARI},
    time_in_status=time_in_status,
    time_entries=time_entries,
    since="2024-05-01",
    until="2024-05-31",
    tz_offset=7,
    commit_stats=commit_stats,
    commit_through="2024-05-15",  # < until 2024-05-31 -> harus memicu peringatan basi
)

# --- Asersi inti ---
by_name = {e.name: e for e in data.engineers}
assert data.total_tasks == 4, f"total_tasks salah: {data.total_tasks}"  # t1,t2(budi) + t2,t3(sari)
assert by_name["Budi"].completed == 2, by_name["Budi"].completed
assert by_name["Sari"].completed == 2, by_name["Sari"].completed
assert by_name["Budi"].tracked_hours == 10.0, by_name["Budi"].tracked_hours
assert by_name["Sari"].tracked_hours == 6.0, by_name["Sari"].tracked_hours
# Budi lead times: t1=2hari, t2=1hari -> median 1.5
assert by_name["Budi"].lead_median == 1.5, by_name["Budi"].lead_median
# Cycle time Budi t1 = 2+0.5 = 2.5 hari (status 'custom' saja), t2 = 1 hari -> median 1.75
assert by_name["Budi"].cycle_median == 1.75, by_name["Budi"].cycle_median
# Bottleneck: 'Review' harus muncul dengan median tertinggi (t3 = 4 hari)
top = data.status_flow[0]
assert top.status == "Review", top.status
assert top.median_hours > 0 and top.p90_hours >= top.median_hours, (top.median_hours, top.p90_hours)
# Status terminal (Done/Closed) harus dikecualikan walau durasinya besar.
flow_names = {b.status for b in data.status_flow}
assert "Done" not in flow_names, flow_names
# Estimasi akurasi Budi: tracked 10j / estimate 12j = 0.83
assert by_name["Budi"].estimate_accuracy == 0.83, by_name["Budi"].estimate_accuracy
# Commit GitLab dari DB: Budi 20 commit / 5 hari aktif / 2 repo; Sari 0.
assert data.has_commit_data is True
assert by_name["Budi"].commits == 20 and by_name["Budi"].active_days == 5, by_name["Budi"].commits
assert by_name["Budi"].repos_touched == 2, by_name["Budi"].repos_touched
assert by_name["Sari"].commits == 0, by_name["Sari"].commits


# --- Sumber GitLab API langsung (pakai fake client, tanpa network) ---
class _FakeGL:
    def __init__(self, by_project):
        self.by_project = by_project

    def iter_commits(self, project, since_iso, until_iso, with_stats=True):
        yield from self.by_project.get(str(project), [])


gl_commits = {
    "100": [
        {"id": "a1", "author_email": "Budi@x.com", "committed_date": "2024-05-02T10:00:00Z", "stats": {"additions": 10, "deletions": 2}},
        {"id": "a2", "author_email": "budi@x.com", "committed_date": "2024-05-02T12:00:00Z", "stats": {"additions": 5, "deletions": 0}},
        {"id": "a1", "author_email": "budi@x.com", "committed_date": "2024-05-02T10:00:00Z", "stats": {"additions": 10, "deletions": 2}},  # dup sha
        {"id": "x9", "author_email": "unknown@x.com", "committed_date": "2024-05-03", "stats": {}},  # bukan engineer
    ],
    "200": [
        {"id": "b1", "author_email": "dewa@gmail.com", "committed_date": "2024-05-04T09:00:00Z", "stats": {"additions": 3, "deletions": 1}},  # alias Budi
    ],
}
gl_email_map = {"budi@x.com": ID_BUDI, "dewa@gmail.com": ID_BUDI}
gl_stats = gl_fetch_commit_stats(_FakeGL(gl_commits), ["100", "200"], gl_email_map, "2024-05-01", "2024-05-31")
gb = gl_stats[ID_BUDI]
assert gb.commits == 3, gb.commits          # a1, a2, b1 (dup a1 & unknown diabaikan)
assert gb.additions == 18 and gb.deletions == 3, (gb.additions, gb.deletions)
assert gb.active_days == 2, gb.active_days   # 05-02 & 05-04
assert gb.repos == 2, gb.repos              # project 100 & 200


# --- Auto-discover repo per engineer (fake client) ---
class _FakeDiscover:
    def find_user_id(self, *, email=None, name=None):
        return {"budi@x.com": 11, "sari@x.com": 22}.get((email or "").lower())

    def iter_push_events(self, user_id, after, before):
        return {
            11: [{"project_id": 100}, {"project_id": 3149}, {"project_id": 100}],
            22: [{"project_id": 200}],
        }.get(user_id, [])


disc = discover_project_ids(
    _FakeDiscover(),
    [("budi@x.com", "Budi"), ("sari@x.com", "Sari"), ("nobody@x.com", "Nobody")],
    "2024-05-01", "2024-05-31",
)
assert disc == {"100", "3149", "200"}, disc  # union, dedup, user tak dikenal dilewati


# --- Filter file noise ---
assert is_noise("vendor/github.com/x/y.go", DEFAULT_NOISE_PATTERNS)
assert is_noise("go.sum", DEFAULT_NOISE_PATTERNS)
assert is_noise("api/service.pb.go", DEFAULT_NOISE_PATTERNS)
assert not is_noise("internal/foo.go", DEFAULT_NOISE_PATTERNS)
diffs = [
    {"new_path": "main.go", "diff": "@@ -1 +1 @@\n+a\n+b\n-c\n"},  # 2 add, 1 del
    {"new_path": "go.sum", "diff": "@@ -1 +1 @@\n+x\n+y\n"},      # noise -> dilewati
]
assert clean_diff_stats(diffs, DEFAULT_NOISE_PATTERNS) == (2, 1)


class _FakeNoiseGL:
    commits = {"100": [{"id": "c1", "author_email": "budi@x.com", "committed_date": "2024-05-02T01:00:00Z"}]}
    diffs = {"c1": diffs}

    def iter_commits(self, project, since_iso, until_iso, with_stats=True):
        return iter(self.commits.get(str(project), []))

    def get_commit_diff(self, project, sha):
        return self.diffs.get(sha, [])


nstats = gl_fetch_commit_stats(_FakeNoiseGL(), ["100"], {"budi@x.com": ID_BUDI}, "2024-05-01", "2024-05-31", exclude_noise=True)
nb = nstats[ID_BUDI]
assert (nb.commits, nb.additions, nb.deletions) == (1, 2, 1), (nb.commits, nb.additions, nb.deletions)

# --- Filter --max-age: t3 (lead 5 hari) harus diabaikan bila max_age=3 ---
data_capped = build_report_data(
    tasks,
    id_to_name=id_to_name,
    target_ids={ID_BUDI, ID_SARI},
    time_in_status=time_in_status,
    time_entries=time_entries,
    since="2024-05-01",
    until="2024-05-31",
    tz_offset=7,
    max_age_days=3,
)
capped = {e.name: e for e in data_capped.engineers}
assert data_capped.filtered_stale == 1, data_capped.filtered_stale  # t3
assert capped["Sari"].completed == 1, capped["Sari"].completed       # tinggal t2
assert capped["Budi"].completed == 2, capped["Budi"].completed       # t1,t2 tetap

# --- Pipeline: resolve target & pemilihan sumber commit (offline) ---
_cfg = Config(
    token="x",
    engineers=[Engineer(name="Budi", email="budi@x.com"), Engineer(name="Sari", id=202)],
    gitlab=GitlabConfig(url="u", token="t", projects=["1"]),
)
_members = [{"id": 101, "email": "budi@x.com", "username": "budi"}]
_ids, _names = resolve_targets(_cfg, _members)
assert _ids == {101, 202}, _ids                       # Budi via email, Sari via id
assert _names[101] == "Budi" and _names[202] == "Sari"
assert resolve_commit_source("auto", _cfg) == "gitlab"  # gitlab terkonfigurasi -> diutamakan
assert resolve_commit_source("db", _cfg) == "none"      # db_dsn kosong
assert resolve_commit_source("none", _cfg) == "none"
assert GatherOptions().days == 30 and GatherOptions().commits_source == "auto"

# --- last_done: tanggal task terakhir selesai (lintas periode) ---
data_ld = build_report_data(
    tasks,
    id_to_name=id_to_name,
    target_ids={ID_BUDI, ID_SARI},
    time_in_status=None,
    time_entries=[],
    since="2024-05-01",
    until="2024-05-31",
    tz_offset=7,
    last_done_ms={ID_BUDI: BASE + 100 * DAY},  # ~Agustus 2024
    last_done_lookback_days=365,
)
ld = {e.name: e for e in data_ld.engineers}
_expected_ld = datetime.fromtimestamp((BASE + 100 * DAY) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
assert data_ld.has_last_done is True
assert ld["Budi"].last_done_date == _expected_ld, ld["Budi"].last_done_date
assert ld["Sari"].last_done_date is None, ld["Sari"].last_done_date
md_ld = render_markdown(data_ld, generated_at="2024-08-31 09:00 WIB")
assert "Selesai terakhir" in md_ld and _expected_ld in md_ld


# --- Utilisasi: skor relatif tim + auto-skip sinyal kosong ---
def _ct(uid, pts, idx):
    return {"id": f"u{uid}-{idx}", "date_created": str(BASE), "date_done": str(BASE + DAY),
            "time_estimate": "0", "points": pts, "assignees": [{"id": uid}],
            "status": {"status": "done", "type": "closed"}}

util_tasks = [_ct(1, 3, 0), _ct(1, 3, 1), _ct(1, 3, 2), _ct(2, 2, 0), _ct(2, 2, 1)]
util_commits = {1: CommitStats(commits=50, active_days=15), 2: CommitStats(commits=20, active_days=8), 3: CommitStats(commits=2, active_days=2)}
util = build_report_data(
    util_tasks, id_to_name={1: "A", 2: "B", 3: "C"}, target_ids={1, 2, 3},
    time_in_status=None, time_entries=[], since="2024-05-01", until="2024-05-31", tz_offset=7,
    commit_stats=util_commits, open_tasks={1: 10, 2: 5, 3: 1},
    open_story_points={1: 20.0, 2: 10.0, 3: 2.0}, utilization=True,
)
um = {e.name: e for e in util.engineers}
assert util.has_utilization is True
assert set(util.utilization_signals) == {"WIP", "hari aktif", "throughput", "story point"}, util.utilization_signals
assert (um["A"].utilization_score, um["B"].utilization_score, um["C"].utilization_score) == (100.0, 50.0, 0.0)
assert um["A"].story_points == 29 and um["A"].open_tasks == 10, (um["A"].story_points, um["A"].open_tasks)
assert set(um["C"].low_signals) == {"WIP", "hari aktif", "throughput", "story point"}
assert um["A"].low_signals == []
assert "Engineer Underutilized" in render_markdown(util, generated_at="2024-05-31 09:00 WIB")

# Sinyal SP kosong -> "story point" di-skip
util2 = build_report_data(
    [_ct(1, 0, 0), _ct(2, 0, 0)], id_to_name={1: "A", 2: "B", 3: "C"}, target_ids={1, 2, 3},
    time_in_status=None, time_entries=[], since="2024-05-01", until="2024-05-31", tz_offset=7,
    commit_stats=util_commits, open_tasks={1: 10, 2: 5, 3: 1}, utilization=True,
)
assert "story point" not in util2.utilization_signals, util2.utilization_signals

md = render_markdown(data, generated_at="2024-05-31 09:00 WIB")
assert "Selesai terakhir" not in md  # kolom hanya muncul bila fitur aktif
assert "# Laporan Produktivitas Engineering" in md
assert "Budi" in md and "Sari" in md
assert "Bottleneck" in md
assert "Aktivitas Commit" in md
assert "Matriks Task vs Commit" in md
assert "lebih lama dari periode" in md  # peringatan commit basi (through < until)

out = Path(__file__).resolve().parent / "sample_report.md"
out.write_text(md, encoding="utf-8")

print("OK - semua asersi lolos.")
print(f"Contoh laporan ditulis ke {out}")
print("\n----- cuplikan laporan -----\n")
print("\n".join(md.splitlines()[:18]))
