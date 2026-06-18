"""Smoke test offline: validasi metrik & render Markdown tanpa memanggil API.

Jalankan: ./.venv/bin/python tests/smoke_test.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engineering_productivity.models import CommitStats
from engineering_productivity.gitlab import (
    DEFAULT_NOISE_PATTERNS,
    clean_diff_stats,
    discover_project_ids,
    is_noise,
)
from engineering_productivity.gitlab import fetch_commit_stats as gl_fetch_commit_stats
from engineering_productivity.config import Config, Engineer, GitlabConfig
from engineering_productivity.metrics import build_report_data
from engineering_productivity.pipeline import (
    GatherOptions,
    _aggregate_commit_rows,
    _commits_via_store,
    _coverage_gaps,
    _fetch_time_in_status,
    _sync_tasks,
    resolve_commit_source,
    resolve_targets,
)
from engineering_productivity.report import render_markdown

# Dua engineer dummy.
ID_BUDI, ID_SARI = 101, 202
id_to_name = {ID_BUDI: "Budi", ID_SARI: "Sari"}

# Atribusi task→engineer via custom field "Developer" (bukan assignees).
DEV_FIELD = "dev-field-test"


def _dev(*ids):
    """Bangun custom_fields dengan kolom Developer berisi user id yang diberikan."""
    return [{"id": DEV_FIELD, "value": [{"id": i} for i in ids]}]

# date_created / date_done dalam epoch ms (string, seperti respons ClickUp asli).
DAY = 86_400_000
BASE = 1_716_000_000_000  # ~Mei 2024

tasks = [
    {
        "id": "t1",
        "date_created": str(BASE),
        "date_done": str(BASE + 2 * DAY),
        "time_estimate": str(8 * 3_600_000),  # 8 jam
        "custom_fields": _dev(ID_BUDI),
    },
    {
        "id": "t2",
        "date_created": str(BASE + 3 * DAY),
        "date_done": str(BASE + 4 * DAY),
        "time_estimate": str(4 * 3_600_000),
        "custom_fields": _dev(ID_BUDI, ID_SARI),  # shared credit
    },
    {
        "id": "t3",
        "date_created": str(BASE + 10 * DAY),
        "date_done": str(BASE + 15 * DAY),
        "time_estimate": "0",
        "custom_fields": _dev(ID_SARI),
    },
    {
        # Task belum selesai -> harus diabaikan.
        "id": "t4",
        "date_created": str(BASE),
        "date_done": None,
        "custom_fields": _dev(ID_BUDI),
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

commit_stats = {
    ID_BUDI: CommitStats(commits=20, additions=100, deletions=10, active_days=5, repos=2),
    999: CommitStats(commits=99),  # bukan target -> diabaikan
}

data = build_report_data(
    tasks,
    developer_field_id=DEV_FIELD,
    id_to_name=id_to_name,
    target_ids={ID_BUDI, ID_SARI},
    time_in_status=time_in_status,
    since="2024-05-01",
    until="2024-05-31",
    tz_offset=7,
    commit_stats=commit_stats,
)

# --- Asersi inti ---
by_name = {e.name: e for e in data.engineers}
assert data.total_tasks == 4, f"total_tasks salah: {data.total_tasks}"  # t1,t2(budi) + t2,t3(sari)
assert by_name["Budi"].completed == 2, by_name["Budi"].completed
assert by_name["Sari"].completed == 2, by_name["Sari"].completed
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
    developer_field_id=DEV_FIELD,
    id_to_name=id_to_name,
    target_ids={ID_BUDI, ID_SARI},
    time_in_status=time_in_status,
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
assert resolve_commit_source(_cfg) == "gitlab"  # gitlab terkonfigurasi -> aktif
assert resolve_commit_source(Config(token="x", engineers=[])) == "none"  # tanpa gitlab
assert GatherOptions().days == 30 and GatherOptions().utilization is True  # utilisasi default nyala

# --- last_done: tanggal task terakhir selesai (lintas periode) ---
data_ld = build_report_data(
    tasks,
    developer_field_id=DEV_FIELD,
    id_to_name=id_to_name,
    target_ids={ID_BUDI, ID_SARI},
    time_in_status=None,
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
            "time_estimate": "0", "points": pts, "custom_fields": _dev(uid),
            "status": {"status": "done", "type": "closed"}}

util_tasks = [_ct(1, 3, 0), _ct(1, 3, 1), _ct(1, 3, 2), _ct(2, 2, 0), _ct(2, 2, 1)]
util_commits = {1: CommitStats(commits=50, active_days=15), 2: CommitStats(commits=20, active_days=8), 3: CommitStats(commits=2, active_days=2)}
util = build_report_data(
    util_tasks, developer_field_id=DEV_FIELD, id_to_name={1: "A", 2: "B", 3: "C"}, target_ids={1, 2, 3},
    time_in_status=None, since="2024-05-01", until="2024-05-31", tz_offset=7,
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
assert "Engineer Utilization" in render_markdown(util, generated_at="2024-05-31 09:00 WIB")

# Sinyal SP kosong -> "story point" di-skip
util2 = build_report_data(
    [_ct(1, 0, 0), _ct(2, 0, 0)], developer_field_id=DEV_FIELD, id_to_name={1: "A", 2: "B", 3: "C"}, target_ids={1, 2, 3},
    time_in_status=None, since="2024-05-01", until="2024-05-31", tz_offset=7,
    commit_stats=util_commits, open_tasks={1: 10, 2: 5, 3: 1}, utilization=True,
)
assert "story point" not in util2.utilization_signals, util2.utilization_signals

# --- Cache DB (Fase 1): coverage gaps, agregasi, dan reuse time_in_status ---
assert _coverage_gaps(None, "2024-05-01", "2024-05-31") == [("2024-05-01", "2024-05-31")]
assert _coverage_gaps(("2024-05-01", "2024-05-31"), "2024-05-01", "2024-05-31") == [("2024-05-30", "2024-05-31")]  # hari terakhir selalu di-refetch
assert _coverage_gaps(("2024-05-10", "2024-05-20"), "2024-05-01", "2024-05-31") == [
    ("2024-05-01", "2024-05-10"), ("2024-05-20", "2024-05-31")]

agg = _aggregate_commit_rows([
    {"sha": "s1", "project_id": "10", "author_email": "Budi@x.com", "committed_date": "2024-05-02T01:00:00Z", "additions": 5, "deletions": 1},
    {"sha": "s1", "project_id": "10", "author_email": "budi@x.com", "committed_date": "2024-05-02T01:00:00Z", "additions": 5, "deletions": 1},  # dup sha
    {"sha": "s2", "project_id": "20", "author_email": "budi@x.com", "committed_date": "2024-05-03T01:00:00Z", "additions": 2, "deletions": 0},
], {"budi@x.com": ID_BUDI})
ab = agg[ID_BUDI]
assert (ab.commits, ab.additions, ab.active_days, ab.repos) == (2, 7, 2, 2), (ab.commits, ab.additions, ab.active_days, ab.repos)


class _FakeStore:
    def __init__(self, tis=None):
        self.tis = dict(tis or {})
        self.puts = 0
        self.commits = {}
        self.coverage = {}
        # Cache task (Fase 2)
        self.tasks = {}            # task_id -> row dict
        self.task_watermark = None
        self.backfilled = set()

    def get_time_in_status(self, ids):
        return {i: self.tis[i] for i in ids if i in self.tis}

    def put_time_in_status(self, tid, payload):
        self.tis[tid] = payload
        self.puts += 1

    def get_commit_coverage(self, pid):
        return self.coverage.get(pid)

    def set_commit_coverage(self, pid, e, l):
        cur = self.coverage.get(pid)
        if cur:
            e, l = min(cur[0], e), max(cur[1], l)
        self.coverage[pid] = (e, l)

    def upsert_commits(self, rows):
        for r in rows:
            self.commits.setdefault(r["sha"], r)

    def get_commits(self, pids, since, until):
        ps = set(pids)
        return [r for r in self.commits.values()
                if r["project_id"] in ps and since <= (r["committed_date"] or "")[:10] <= until]

    # --- task cache (Fase 2) ---
    def get_task_watermark(self):
        return self.task_watermark

    def set_task_watermark(self, ms):
        self.task_watermark = ms

    def get_backfilled_engineers(self):
        return set(self.backfilled)

    def mark_backfilled(self, ids):
        self.backfilled.update(ids)

    def upsert_tasks(self, rows):
        for r in rows:
            self.tasks[r["task_id"]] = dict(r)

    def get_tasks(self, developer_ids):
        want = set(developer_ids)
        return [r["payload"] for r in self.tasks.values()
                if want & set(r.get("developer_ids") or [])]

    def commit(self):
        pass


class _CountingClient:
    def __init__(self):
        self.calls = 0

    def get_time_in_status(self, tid):
        self.calls += 1
        return {"current_status": {"status": "done", "type": "closed", "total_time": {"by_minute": 0}}, "status_history": []}


# Task t1 sudah ada di cache → tidak dipanggil; t2 miss → ditarik & disimpan.
_store = _FakeStore(tis={"t1": {"status_history": []}})
_client = _CountingClient()
tis_out = _fetch_time_in_status(_client, [{"id": "t1"}, {"id": "t2"}], _store, lambda m: None)
assert set(tis_out) == {"t1", "t2"}, tis_out
assert _client.calls == 1, _client.calls          # hanya t2 ditarik
assert _store.puts == 1 and "t2" in _store.tis     # t2 disimpan ke cache


class _GLForStore:
    def __init__(self):
        self.fetches = 0

    def iter_commits(self, pid, since_iso, until_iso, with_stats=True):
        self.fetches += 1
        return [{"id": "c1", "author_email": "budi@x.com",
                 "committed_date": "2024-05-05T00:00:00Z", "stats": {"additions": 3, "deletions": 1}}]


_cstore, _gl = _FakeStore(), _GLForStore()
_r1 = _commits_via_store(_gl, _cstore, ["10"], {"budi@x.com": ID_BUDI}, "2024-05-01", "2024-05-31", lambda m: None)
assert _gl.fetches == 1 and _r1[ID_BUDI].commits == 1, (_gl.fetches, _r1)
# Window sama lagi → hari terakhir SELALU di-refetch (fresh hari ini), tapi commit dedup by sha.
_r2 = _commits_via_store(_gl, _cstore, ["10"], {"budi@x.com": ID_BUDI}, "2024-05-01", "2024-05-31", lambda m: None)
assert _gl.fetches == 2, _gl.fetches            # +1 fetch: refetch hari terakhir
assert _r2[ID_BUDI].commits == 1                # dedup by sha → tetap 1

# --- Cache TASK (Fase 2): _sync_tasks backfill + incremental ---
class _TaskClient:
    """Fake ClickUp client untuk iter_team_tasks; rekam pemanggilan + params."""

    def __init__(self, by_call):
        # by_call: list of (matcher_fn, tasks) dipakai berurutan menurut params
        self.by_call = by_call
        self.calls = []  # rekam kwargs tiap pemanggilan

    def iter_team_tasks(self, team_id, *, developer_field_id, developer_ids,
                        date_updated_gt=None, include_closed=True, **kw):
        self.calls.append({"developer_ids": list(developer_ids),
                           "date_updated_gt": date_updated_gt})
        for matcher, tasks in self.by_call:
            if matcher(developer_ids, date_updated_gt):
                return iter(tasks)
        return iter([])


def _task(tid, dev, du, dd=None, stype="open"):
    return {
        "id": tid,
        "date_updated": str(du),
        "date_done": str(dd) if dd is not None else None,
        "status": {"type": stype, "status": stype},
        "custom_fields": _dev(dev),
        "points": "1",
    }


BACKFILL_MS = 1_716_000_000_000

# Run-1 (cold): belum ada backfill → backfill dipanggil untuk new=[101,202],
# lalu incremental (watermark=BACKFILL_MS). Kedua-duanya dipanggil.
_ts = _FakeStore()
_backfill_tasks = [_task("bt1", ID_BUDI, BACKFILL_MS + 10), _task("bt2", ID_SARI, BACKFILL_MS + 20)]
_incr_tasks_1 = [_task("bt1", ID_BUDI, BACKFILL_MS + 30, dd=BACKFILL_MS + 30, stype="done")]
_tc = _TaskClient([
    (lambda d, du: du == BACKFILL_MS and set(d) == {ID_BUDI, ID_SARI} and len(_ts.backfilled) == 0, _backfill_tasks),
    (lambda d, du: True, _incr_tasks_1),
])
_sync_tasks(_tc, _ts, "team", DEV_FIELD, {ID_BUDI, ID_SARI}, BACKFILL_MS, lambda m: None)
assert _ts.backfilled == {ID_BUDI, ID_SARI}, _ts.backfilled            # keduanya di-backfill
assert len(_tc.calls) == 2, _tc.calls                                  # 1 backfill + 1 incremental
assert _tc.calls[0]["date_updated_gt"] == BACKFILL_MS                  # backfill sejak watermark awal
assert _tc.calls[1]["date_updated_gt"] == BACKFILL_MS                  # incremental: watermark None→backfill
assert set(_ts.tasks) == {"bt1", "bt2"}, set(_ts.tasks)               # bt1 di-upsert ulang oleh incremental
assert _ts.task_watermark == BACKFILL_MS + 30, _ts.task_watermark      # watermark = max date_updated

# Run-2 (warm): backfill sudah lengkap → TANPA backfill, hanya incremental dgn developer_ids penuh
# dan date_updated_gt = watermark sekarang.
_tc2 = _TaskClient([(lambda d, du: True, [])])  # tak ada delta baru
_sync_tasks(_tc2, _ts, "team", DEV_FIELD, {ID_BUDI, ID_SARI}, BACKFILL_MS, lambda m: None)
assert len(_tc2.calls) == 1, _tc2.calls                                # incremental saja, tanpa backfill
assert sorted(_tc2.calls[0]["developer_ids"]) == [ID_BUDI, ID_SARI]    # developer_ids penuh
assert _tc2.calls[0]["date_updated_gt"] == BACKFILL_MS + 30, _tc2.calls  # pakai watermark dari run-1

# get_tasks: filter overlap developer benar.
assert {t["id"] for t in _ts.get_tasks([ID_BUDI])} == {"bt1"}, _ts.get_tasks([ID_BUDI])
assert {t["id"] for t in _ts.get_tasks([ID_SARI])} == {"bt2"}, _ts.get_tasks([ID_SARI])
assert {t["id"] for t in _ts.get_tasks([ID_BUDI, ID_SARI])} == {"bt1", "bt2"}


md = render_markdown(data, generated_at="2024-05-31 09:00 WIB")
assert "Selesai terakhir" not in md  # kolom hanya muncul bila fitur aktif
assert "# Laporan Produktivitas Engineering" in md
assert "Budi" in md and "Sari" in md
assert "Bottleneck" in md
assert "Aktivitas Commit" in md
assert "Matriks Task vs Commit" in md

out = Path(__file__).resolve().parent / "sample_report.md"
out.write_text(md, encoding="utf-8")

print("OK - semua asersi lolos.")
print(f"Contoh laporan ditulis ke {out}")
print("\n----- cuplikan laporan -----\n")
print("\n".join(md.splitlines()[:18]))
