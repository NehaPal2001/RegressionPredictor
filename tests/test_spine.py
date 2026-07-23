"""Smallest checks that fail if the spine logic breaks."""

import sqlite3

from tracer.diff import parse_unified
from tracer.history import Recurrence
from tracer.loom_client import LoomClient
from tracer.risk import assess_symbol
from tracer.loom_client import Symbol
from tracer.screens import humanize, screen_for, _mapping_of


DIFF = """\
diff --git a/src/A.java b/src/A.java
index 111..222 100644
--- a/src/A.java
+++ b/src/A.java
@@ -10,3 +12,4 @@ class A
+x
@@ -30,2 +40,0 @@ class A
-y
diff --git a/src/B.py b/src/B.py
@@ -1,0 +2,2 @@
+z
"""


def test_parse_unified():
    files = parse_unified(DIFF)
    assert [(f.path, f.ranges, f.deletions) for f in files] == [
        ("src/A.java", [(12, 15)], 1),
        ("src/B.py", [(2, 3)], 0),
    ]


def test_risk_rules():
    from datetime import date, timedelta
    sym = Symbol("m:x", "method", "a.b.foo", "src/A.java", 10, 20, "java")
    recent_date = (date.today() - timedelta(days=30)).isoformat()
    rec = Recurrence("src/A.java", 15, 16, "abcd1234", "fix NPE on empty list", recent_date)
    assert assess_symbol(sym, 0, [rec], False).level == "HIGH"
    assert assess_symbol(sym, 9, [], False).level == "MEDIUM"
    low = assess_symbol(sym, 1, [], False)
    assert low.level == "LOW" and low.reasons == ["directly changed in this diff"]
    # recurrence in a different file must not fire
    assert assess_symbol(sym, 0, [Recurrence("src/C.java", 15, 16, "ff", "fix", "d")], False).level == "LOW"


def test_screens():
    assert humanize("TestCaseApprovalController") == "Test Case Approval"
    smap = {"/api/v1": "Generic", "/api/v1/tax": "Tax Settings"}
    assert screen_for("/api/v1/tax/rates", "fb", smap) == "Tax Settings"
    assert screen_for("/other", "fb", smap) == "fb"
    assert _mapping_of(['  @PostMapping("/create")']) == ("POST", "/create")
    assert _mapping_of(['@RequestMapping(value = "/x", method = RequestMethod.PUT)']) == ("PUT", "/x")
    assert _mapping_of(["public void x() {"]) is None


def test_bad_ref_raises_not_all_clear(tmp_path):
    import subprocess

    import pytest

    from tracer.diff import GitError
    from tracer.history import recurrence

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    with pytest.raises(GitError):  # bad base ref must fail loud, never empty-result
        recurrence(str(tmp_path), "HEAD", "no-such-ref", [("a.py", [(1, 2)])])


def test_blast_radius_inferred_taint(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT,
             start_line INT, end_line INT, language TEXT, deleted_at INT);
           CREATE TABLE edges (from_id TEXT, to_id TEXT, kind TEXT, confidence_tier TEXT, metadata TEXT);
           INSERT INTO nodes VALUES
             ('impl', 'method', 'Impl.save', 'Impl.java', 1, 5, 'java', NULL),
             ('iface', 'method', 'Iface.save', 'Iface.java', 1, 2, 'java', NULL),
             ('ctrl', 'method', 'Ctrl.post', 'Ctrl.java', 1, 9, 'java', NULL);
           INSERT INTO edges VALUES
             ('iface', 'impl', 'CALLS', 'inferred', '{}'),   -- DI bridge
             ('ctrl', 'iface', 'CALLS', 'extracted', '{}');
        """
    )
    con.commit()
    con.close()
    reaches = {r.symbol.id: r for r in LoomClient(db).blast_radius("impl")}
    assert reaches["iface"].depth == 1 and reaches["iface"].inferred
    assert reaches["ctrl"].depth == 2 and reaches["ctrl"].inferred  # taint propagates


def test_fan_in_counts_distinct_files(tmp_path):
    """Two edges from the same file must count as 1, not 2. Soft-deleted callers excluded."""
    db = tmp_path / "fi.db"
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT,
             start_line INT, end_line INT, language TEXT, deleted_at INT);
           CREATE TABLE edges (from_id TEXT, to_id TEXT, kind TEXT, confidence_tier TEXT, metadata TEXT);
           INSERT INTO nodes VALUES
             ('callee', 'method', 'Svc.save', 'Svc.java',  1, 10, 'java', NULL),
             ('a1',    'method', 'A.m1',     'A.java',    1,  5, 'java', NULL),
             ('a2',    'method', 'A.m2',     'A.java',    6, 10, 'java', NULL),
             ('b1',    'method', 'B.m1',     'B.java',    1,  5, 'java', NULL),
             ('del1',  'method', 'Del.m1',   'Del.java',  1,  5, 'java', 1);
           INSERT INTO edges VALUES
             ('a1',   'callee', 'CALLS', 'extracted', '{}'),
             ('a2',   'callee', 'CALLS', 'extracted', '{}'),
             ('b1',   'callee', 'CALLS', 'extracted', '{}'),
             ('del1', 'callee', 'CALLS', 'extracted', '{}');
        """
    )
    con.commit(); con.close()
    # 4 edges from 3 files, but Del.java is soft-deleted — expect 2 live distinct files
    assert LoomClient(db).fan_in('callee') == 2


def test_recurrence_scrubs_merges_and_reverts(tmp_path):
    """Real git merge commits and revert-prefixed commits must not appear in recurrence."""
    import subprocess as sp
    import os
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t.com",
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
    }

    def git(*args):
        result = sp.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True)
        assert result.returncode == 0, f"git {args} failed: {result.stderr}"
        return result.stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@t.com")
    git("config", "user.name", "T")

    # Commit 1: base file content
    (repo / "svc.py").write_text("def foo():\n    pass\n")
    git("add", "svc.py")
    git("commit", "-m", "initial")

    # detect default branch name after first commit
    branch = git("rev-parse", "--abbrev-ref", "HEAD") or "master"

    # Commit 2: revert-prefixed commit (must be in prior set → comes before base ref)
    (repo / "svc.py").write_text("def foo():\n    pass\n    x = 1\n")
    git("add", "svc.py")
    git("commit", "-m", "Revert 'accidentally reverted fix'")

    # Commit 3: real merge commit via branch (must also be in prior set)
    git("checkout", "-b", "side")
    (repo / "svc.py").write_text("def foo():\n    pass\n    x = 1\n    y = 1\n")
    git("add", "svc.py")
    git("commit", "-m", "side: add y")
    git("checkout", branch)
    git("merge", "--no-ff", "side", "-m", "Merge branch 'side' into main")

    # Commit 4: real fix commit (must be in prior set)
    (repo / "svc.py").write_text("def foo():\n    pass\n    x = 1\n    y = 1\n    return 1\n")
    git("add", "svc.py")
    git("commit", "-m", "fix: null pointer in foo")

    # Commit 5: the "new change" that is the diff target
    (repo / "svc.py").write_text("def foo():\n    return 42\n")
    git("add", "svc.py")
    git("commit", "-m", "refactor: simplify foo")

    # HEAD~1 is the "fix" commit. Everything reachable from HEAD~1 is in prior.
    # So revert, merge, and fix commits are ALL in prior — only the scrubbing rules filter them.
    from tracer.history import recurrence
    results = recurrence(str(repo), "HEAD", "HEAD~1", [("svc.py", [(1, 10)])])
    subjects = [r.subject for r in results]
    assert not any("Merge" in s for s in subjects), f"merge commits must be filtered, got: {subjects}"
    assert not any(s.lower().startswith("revert") for s in subjects), f"revert commits must be filtered, got: {subjects}"
    assert any("fix" in s for s in subjects), f"real fix commits must still appear, got: {subjects}"
    assert len(results) == 1, f"expected exactly 1 fix commit, got {len(results)}: {subjects}"
    assert len(results[0].sha) == 8, f"sha should be 8 chars, got: {results[0].sha!r}"


def test_recurrence_recency_decay():
    from datetime import date, timedelta
    from tracer.history import _is_recent, RECENCY_DAYS

    # --- unit test _is_recent boundary ---
    today = date.today()
    assert _is_recent((today - timedelta(days=0)).isoformat()) is True   # today
    assert _is_recent((today - timedelta(days=89)).isoformat()) is True  # 89 days ago
    assert _is_recent((today - timedelta(days=90)).isoformat()) is False # exactly 90 days → not recent
    assert _is_recent((today - timedelta(days=100)).isoformat()) is False
    assert _is_recent("not-a-date") is True  # ValueError fallback → conservative

    # --- integration: assess_symbol uses dates correctly ---
    sym = Symbol("m:x", "method", "a.b.foo", "src/A.java", 10, 20, "java")
    recent_date = (today - timedelta(days=30)).isoformat()
    old_date    = (today - timedelta(days=100)).isoformat()
    recent_rec = Recurrence("src/A.java", 15, 16, "abc1", "fix NPE", recent_date)
    old_rec    = Recurrence("src/A.java", 15, 16, "abc2", "fix timeout", old_date)

    assert assess_symbol(sym, 0, [recent_rec], False).level == "HIGH"
    assert assess_symbol(sym, 0, [old_rec],    False).level == "MEDIUM"

    # Mixed: recent wins
    result = assess_symbol(sym, 0, [old_rec, recent_rec], False)
    assert result.level == "HIGH"
    assert len(result.reasons) == 2  # both recurrences contribute a reason


def test_hub_cap_applies_at_deep_hops():
    from tracer.risk import cap_reach_risk
    level, reason = cap_reach_risk("HIGH", depth=6, seed_fan_in=30, seed_is_proto=False)
    assert level == "LOW"
    assert reason == "distant, hub-mediated path"

def test_hub_cap_exempt_for_proto():
    from tracer.risk import cap_reach_risk
    level, reason = cap_reach_risk("HIGH", depth=6, seed_fan_in=30, seed_is_proto=True)
    assert level == "HIGH"
    assert reason is None

def test_hub_cap_not_applied_at_shallow_depth():
    from tracer.risk import cap_reach_risk
    level, reason = cap_reach_risk("HIGH", depth=3, seed_fan_in=30, seed_is_proto=False)
    assert level == "HIGH"
    assert reason is None

def test_hub_cap_not_applied_below_hub_threshold():
    from tracer.risk import cap_reach_risk
    level, reason = cap_reach_risk("HIGH", depth=6, seed_fan_in=10, seed_is_proto=False)
    assert level == "HIGH"
    assert reason is None

def test_hub_cap_at_exact_boundary():
    from tracer.risk import cap_reach_risk, DEEP_HOP, HUB_FAN_IN
    # Exactly at boundary — should cap
    level, reason = cap_reach_risk("HIGH", depth=DEEP_HOP, seed_fan_in=HUB_FAN_IN, seed_is_proto=False)
    assert level == "LOW"
    assert reason == "distant, hub-mediated path"

def test_hub_cap_low_input_never_capped():
    from tracer.risk import cap_reach_risk
    # Already LOW — never cap further
    level, reason = cap_reach_risk("LOW", depth=6, seed_fan_in=30, seed_is_proto=False)
    assert level == "LOW"
    assert reason is None


def test_blind_spot_excludes_nodes_reaching_an_endpoint(tmp_path):
    """A service method between a changed symbol and its controller must NOT be a blind
    spot just because it isn't itself an endpoint — its own upward walk reaches one."""
    from tracer.cli import compute_blind_spots
    from tracer.loom_client import LoomClient

    db = tmp_path / "bs.db"
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT,
             start_line INT, end_line INT, language TEXT, deleted_at INT);
           CREATE TABLE edges (from_id TEXT, to_id TEXT, kind TEXT, confidence_tier TEXT, metadata TEXT);
           INSERT INTO nodes VALUES
             ('seed', 'method', 'Svc.changed',   'Svc.java',  1, 5, 'java', NULL),
             ('mid',  'method', 'Svc.helper',    'Svc.java',  6, 10, 'java', NULL),
             ('ep',   'method', 'Ctrl.endpoint', 'Ctrl.java', 1, 5, 'java', NULL),
             ('dead', 'method', 'Util.orphan',   'Util.java', 1, 5, 'java', NULL);
           INSERT INTO edges VALUES
             ('mid', 'seed', 'CALLS', 'extracted', '{}'),  -- mid calls seed
             ('ep',  'mid',  'CALLS', 'extracted', '{}');  -- ep (controller) calls mid
        """
    )
    con.commit()
    con.close()
    lc = LoomClient(db)
    candidates = {"mid": "Svc.helper", "dead": "Util.orphan"}
    blind = compute_blind_spots(lc, candidates, known_endpoint_ids={"ep"}, max_depth=6)
    names = [s for s in blind]
    assert "Svc.helper" not in names   # reaches 'ep' via its own blast radius — not blind
    assert "Util.orphan" in names      # no path to any endpoint — genuinely blind


def test_jira_mock_maps_commits_to_screens(tmp_path, monkeypatch):
    """A fix commit touching a changed symbol's file maps to that symbol's screens and risk;
    merge commits are not flagged FIX."""
    from tracer import jira_mock
    from tracer.diff import Commit
    from tracer.loom_client import Symbol
    from tracer.report import Feature
    from tracer.risk import SymbolRisk

    sym = Symbol("id1", "method", "Svc.createManually", "svc/TestCaseService.java", 10, 40, "java")
    changed = [SymbolRisk(sym, "HIGH", ["recurrence"])]
    feat = Feature("Test Case", [], "HIGH", ["recurrence"], 0, False, via=["createManually"])

    monkeypatch.setattr(jira_mock, "branch_commits", lambda *a, **k: [
        Commit("aaaa1111", "unique id fix", "2026-05-05"),
        Commit("bbbb2222", "Merge branch 'fix/x' into y", "2026-05-05"),
    ])
    monkeypatch.setattr(jira_mock, "commit_files",
                        lambda repo, sha: ["svc/TestCaseService.java"] if sha == "aaaa1111" else [])

    parent, subs = jira_mock.build_tickets("repo", "base", "target", changed, [feat])
    assert parent.kind == "Story" and parent.risk == "HIGH"
    assert "Test Case" in parent.screens
    fix = subs[0]
    assert fix.is_fix and fix.risk == "HIGH" and "Test Case" in fix.screens
    merge = subs[1]
    assert not merge.is_fix  # merge subject with 'fix/x' must NOT be flagged a fix


def test_blind_spot_section_in_html():
    from tracer.report import render_html, Feature
    from tracer.diff import DiffScope
    from tracer.loom_client import Symbol

    sym = Symbol("id1", "method", "Util.compute", "src/Util.java", 1, 10, "java")
    scope = DiffScope("base", "HEAD", [], [], 0)
    html = render_html(
        scope=scope,
        changed=[],
        features=[],
        recs=[],
        coupled={},
        tests=[],
        blind_spots=[sym],
        ai_notes=None,
    )
    assert "compute" in html          # short name (rsplit strips "Util." prefix)
    assert "src/Util.java" in html    # full path still shown
    assert "blind spot" in html.lower() or "unreachable" in html.lower()

    # Empty list must not render the section
    html_empty = render_html(
        scope=scope, changed=[], features=[], recs=[],
        coupled={}, tests=[], blind_spots=[], ai_notes=None,
    )
    assert "blind spot" not in html_empty.lower()
    assert "unreachable" not in html_empty.lower()


# -- bridge.py (cross-repo: backend endpoint → Angular client screen) --------


def test_normalize_path_and_suffix_match():
    from tracer.bridge import normalize_path, suffix_match

    be = normalize_path("/api/dashboard/{verticalId}/testcase-vs-assignee")
    cl = normalize_path("dashboard/${this.activeVerticalId}/testcase-vs-assignee?projectKey=${x}")
    assert be == ["api", "dashboard", "{*}", "testcase-vs-assignee"]  # {var} → {*}
    assert cl == ["dashboard", "{*}", "testcase-vs-assignee"]         # query stripped, ${..} → {*}
    assert suffix_match(be, cl)                                       # real pair matches (absorbs /api)
    # literal mismatch in aligned trailing position rejected
    assert not suffix_match(be, normalize_path("dashboard/${id}/other"))
    # client longer than backend rejected
    assert not suffix_match(normalize_path("a/b"), normalize_path("x/a/b"))
    # 1-literal coincidence rejected (needs >=2 literal trailing segments)
    assert not suffix_match(normalize_path("/api/admin/users"), normalize_path("${id}/users"))


def test_extract_endpoint_and_verb():
    from tracer.bridge import extract_endpoint, extract_verb

    ctx = "return this.apiService.get(`dashboard/${this.activeVerticalId}/testcase-vs-assignee?x=1`);"
    assert extract_endpoint(ctx) == "dashboard/${this.activeVerticalId}/testcase-vs-assignee?x=1"
    assert extract_endpoint("this.apiService.post('users/create', body)") == "users/create"
    # fully indirected (no literal segments) → None, becomes a coverage gap
    assert extract_endpoint("return this.apiService.get(`${this.base()}?page=0`);") is None
    assert extract_endpoint("this.apiService.get(this.dynamicUrl)") is None  # no string arg
    assert extract_verb("get") == "GET" and extract_verb("ApiService.delete") == "DELETE"


def test_parse_routes_and_component_screen():
    from tracer.bridge import parse_routes, component_screen

    src = """
    export const routes: Routes = [
      { path: '', component: HomeComponent },
      {
        path: 'dashboard',
        component: DashboardComponent,
        children: [
          { path: 'testcase-vs-assignee', component: TestcaseVsAssigneeComponent },
        ],
      },
    ];
    """
    routes = parse_routes(src)
    assert routes["HomeComponent"] == ""
    assert routes["DashboardComponent"] == "dashboard"
    assert routes["TestcaseVsAssigneeComponent"] == "dashboard/testcase-vs-assignee"  # nested composed
    assert component_screen("TestcaseVsAssigneeComponent", routes) == "Dashboard Testcase Vs Assignee"
    # fallback: not in routes → humanized class name
    assert component_screen("NewDashboardComponent", {}) == "New Dashboard"


def test_resolve_client_screens(tmp_path):
    import json

    from tracer.bridge import resolve_client_screens
    from tracer.loom_client import LoomClient
    from tracer.screens import Endpoint

    db = tmp_path / "client.db"
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, name TEXT, path TEXT,
             start_line INT, end_line INT, language TEXT, deleted_at INT);
           CREATE TABLE edges (from_id TEXT, to_id TEXT, kind TEXT, confidence_tier TEXT, metadata TEXT);"""
    )
    con.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)",
        [
            ("svc_get", "method", "get", "src/app/core/api.service.ts", 10, 12, "typescript", None),
            ("resolved_caller", "method", "DashboardService.loadAssignees",
             "src/app/dashboard/dashboard.service.ts", 5, 9, "typescript", None),
            ("component_m", "method", "DashboardComponent.ngOnInit",
             "src/app/dashboard/dashboard.component.ts", 1, 4, "typescript", None),
            ("indirect_caller", "method", "ReportService.load",
             "src/app/report/report.service.ts", 5, 9, "typescript", None),
        ],
    )
    resolved_ctx = "return this.apiService.get(`dashboard/${this.activeVerticalId}/testcase-vs-assignee?x=1`);"
    indirect_ctx = "return this.apiService.get(`${this.base()}?page=0`);"
    con.executemany(
        "INSERT INTO edges VALUES (?,?,?,?,?)",
        [
            ("resolved_caller", "svc_get", "CALLS", "extracted", json.dumps({"call_context": resolved_ctx})),
            ("component_m", "resolved_caller", "CALLS", "extracted", "{}"),   # component calls the service
            ("indirect_caller", "svc_get", "CALLS", "extracted", json.dumps({"call_context": indirect_ctx})),
        ],
    )
    con.commit()
    con.close()

    repo = tmp_path / "client"
    (repo / "src/app").mkdir(parents=True)
    (repo / "src/app/app.routes.ts").write_text(
        "export const routes = [ { path: 'dashboard', component: DashboardComponent } ];"
    )

    dash = Endpoint("GET", "/api/dashboard/{verticalId}/testcase-vs-assignee", "Dashboard",
                    "DashboardController", "getAssignees", "ep_dash")
    report = Endpoint("GET", "/api/report/{id}/summary", "Report",
                      "ReportController", "getSummary", "ep_report")

    result = resolve_client_screens([dash, report], LoomClient(db), repo)
    screens = {m.endpoint.node_id: (m.screen_name, m.client_component, m.confidence) for m in result.mappings}
    assert screens["ep_dash"] == ("Dashboard", "DashboardComponent", "inferred")  # walked up to routed component
    assert [e.node_id for e in result.unresolved] == ["ep_report"]  # indirected client call → gap
    assert result.match_rate == 0.5
