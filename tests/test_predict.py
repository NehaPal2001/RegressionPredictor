"""Phase 2a — deterministic predict pipeline tests. No network, no Groq."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


# ── helpers ────────────────────────────────────────────────────────────────

def _scope(commits=None, symbols=None, screens=None):
    return {
        "version": 1,
        "base": "main",
        "target": "feature",
        "repo": "/repo",
        "generated_at": "2026-07-24T10:00:00Z",
        "commits": commits or [
            {"hash": "abc1234", "author_email": "dev@co.com", "message": "add feature"},
        ],
        "changed_symbols": symbols or [
            {"id": "method:REPO:Foo.java:bar", "name": "bar",
             "path": "src/Foo.java", "risk": "HIGH", "fan_in": 3},
        ],
        "affected_screens": screens or ["Dashboard"],
    }


def _raw_case(jira_key="AS-1", status="ACTIVE"):
    """Real TCM API response shape (matches _parse_case in tcm_client.py)."""
    return {
        "id": "tc-001",
        "uniqueTestcaseId": "TC-001",
        "jiraStoryKey": jira_key,
        "jiraStoryId": "10001",
        "testcaseStatus": status,
        "approvalStatus": "APPROVED",
        "customFieldValues": [
            {"fieldDefinition": {"name": "TEST_CASE_TITLE"}, "value": "Login test"},
            {"fieldDefinition": {"name": "TEST_CASE_DESCRIPTION"}, "value": "Verify login flow"},
            {"fieldDefinition": {"name": "TEST_CATEGORY"}, "value": "Functional"},
            {"fieldDefinition": {"name": "TEST_CASE_PRIORITY"}, "value": "High"},
            {"fieldDefinition": {"name": "AUTOMATION_STATUS"}, "value": "Manual"},
            {"fieldDefinition": {"name": "TEST_CASE_TYPE"}, "value": "Black Box"},
        ],
        "testSteps": [
            {"order": 1, "action": "Open app", "expected": "App opens"},
        ],
    }


# ── Task 1: diff.py Commit author_email ────────────────────────────────────

def test_commit_has_author_email():
    from tracer.diff import Commit
    c = Commit(sha="abc", subject="fix", date="2026-01-01", author_email="dev@co.com")
    assert c.author_email == "dev@co.com"


def test_commit_author_email_default_empty():
    from tracer.diff import Commit
    c = Commit(sha="abc", subject="fix", date="2026-01-01")
    assert c.author_email == ""


# ── Task 2: tcm_client.py ──────────────────────────────────────────────────

def test_tcm_parse_case():
    from tracer.tcm_client import _parse_case
    tc = _parse_case(_raw_case("AS-1"))
    assert tc.id == "tc-001"
    assert tc.unique_id == "TC-001"
    assert tc.jira_story_key == "AS-1"
    assert tc.jira_story_id == "10001"
    assert tc.status == "ACTIVE"
    assert tc.title == "Login test"
    assert len(tc.steps) == 1
    assert tc.steps[0].action == "Open app"


def test_tcm_parse_case_no_jira():
    from tracer.tcm_client import _parse_case
    raw = _raw_case()
    raw["jiraStoryKey"] = None
    raw["jiraStoryId"] = None
    tc = _parse_case(raw)
    assert tc.jira_story_key == "NA"   # real default — not "N/A"
    assert tc.jira_story_id is None


def test_tcm_fetch_all_paginates():
    from tracer.tcm_client import fetch_all

    # Real API: top-level "content" array + "totalPages" (no wrapping "data" key)
    page0 = {"content": [_raw_case("AS-1")], "totalPages": 2, "currentPage": 0}
    page1 = {"content": [_raw_case("AS-2")], "totalPages": 2, "currentPage": 1}

    responses = [json.dumps(page0).encode(), json.dumps(page1).encode()]
    call_count = 0

    class FakeResp:
        def __init__(self, data):
            self._data = data
        def read(self):
            return self._data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    def fake_urlopen(req):
        nonlocal call_count
        r = FakeResp(responses[call_count])
        call_count += 1
        return r

    with patch("tracer.tcm_client.urllib.request.urlopen", side_effect=fake_urlopen):
        _raw, cases = fetch_all("v1", "AS360", "sess", "proj_sess", page_size=1)

    assert len(cases) == 2
    assert cases[0].jira_story_key == "AS-1"
    assert cases[1].jira_story_key == "AS-2"


def test_tcm_by_jira_key():
    from tracer.tcm_client import _parse_case, by_jira_key
    cases = [_parse_case(_raw_case("AS-1")), _parse_case(_raw_case("AS-2"))]
    result = by_jira_key(cases, "AS-1")
    assert len(result) == 1
    assert result[0].jira_story_key == "AS-1"


def test_tcm_linked_jira_keys():
    from tracer.tcm_client import _parse_case, linked_jira_keys
    raw_na = _raw_case()
    raw_na["jiraStoryKey"] = None   # will become "NA" after parse
    cases = [_parse_case(_raw_case("AS-1")), _parse_case(_raw_case("AS-2")), _parse_case(raw_na)]
    keys = linked_jira_keys(cases)
    assert keys == {"AS-1", "AS-2"}


# ── Task 3: jira_client.py ─────────────────────────────────────────────────

_ADF = {
    "type": "doc",
    "content": [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Fix bug "},
                {"type": "text", "text": "abc1234"},
            ],
        }
    ],
}


def test_adf_to_text_extracts_plain_text():
    from tracer.jira_client import _adf_to_text
    text = _adf_to_text(_ADF)
    assert "Fix bug" in text
    assert "abc1234" in text


def test_adf_to_text_none_returns_empty():
    from tracer.jira_client import _adf_to_text
    assert _adf_to_text(None) == ""


def _jira_raw(key="AS-1", description=None):
    return {
        "key": key,
        "fields": {
            "issuetype": {"name": "Story"},
            "status": {"name": "In Progress"},
            "priority": {"name": "High"},
            "summary": "Add login page",
            "description": description or _ADF,
            "customfield_10014": None,
        },
    }


def _fake_jira_urlopen(issues):
    payload = json.dumps({"issues": issues}).encode()

    class FakeResp:
        def read(self):
            return payload
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass

    return lambda req: FakeResp()


def test_jira_fetch_by_keys():
    from tracer.jira_client import JiraClient
    client = JiraClient("https://jira.example.com", "user@co.com", "token")
    with patch("tracer.jira_client.urllib.request.urlopen",
               _fake_jira_urlopen([_jira_raw("AS-1"), _jira_raw("AS-2")])):
        issues = client.fetch_by_keys(["AS-1", "AS-2"])
    assert len(issues) == 2
    assert issues[0].key == "AS-1"
    assert issues[1].key == "AS-2"


def test_jira_search_commit():
    from tracer.jira_client import JiraClient
    client = JiraClient("https://jira.example.com", "user@co.com", "token")
    with patch("tracer.jira_client.urllib.request.urlopen",
               _fake_jira_urlopen([_jira_raw("AS-1")])):
        issues = client.search_commit("abc1234", "AS360")
    assert len(issues) == 1
    assert "Fix bug" in issues[0].description


def test_jira_fetch_by_keys_empty():
    from tracer.jira_client import JiraClient
    client = JiraClient("https://jira.example.com", "user@co.com", "token")
    assert client.fetch_by_keys([]) == []


def test_jira_no_description():
    from tracer.jira_client import _parse_issue
    raw = _jira_raw("AS-1")
    raw["fields"]["description"] = None
    issue = _parse_issue(raw)
    assert issue.description == ""


# ── Task 4: gap_detector.py ────────────────────────────────────────────────

def _make_jira_client(results_by_hash):
    client = MagicMock()
    client.search_commit.side_effect = lambda h, _pk: results_by_hash.get(h, [])
    return client


def test_gap_detector_covered():
    from tracer.gap_detector import detect_gaps
    from tracer.jira_client import JiraIssue
    story = JiraIssue("AS-1", "Story", "Done", "High", "s", "d", None)
    commits = [{"hash": "abc1234", "author_email": "dev@co.com", "message": "fix"}]
    result = detect_gaps(commits, _make_jira_client({"abc1234": [story]}), "AS360")
    assert len(result) == 1
    assert result[0].covered is True
    assert result[0].jira_keys == ("AS-1",)


def test_gap_detector_uncovered():
    from tracer.gap_detector import detect_gaps
    commits = [{"hash": "dead000", "author_email": "dev@co.com", "message": "mystery"}]
    result = detect_gaps(commits, _make_jira_client({}), "AS360")
    assert result[0].covered is False
    assert result[0].jira_keys == ()


def test_gap_detector_mixed():
    from tracer.gap_detector import detect_gaps
    from tracer.jira_client import JiraIssue
    story = JiraIssue("AS-1", "Story", "Done", "High", "s", "d", None)
    commits = [
        {"hash": "abc1234", "author_email": "a@co.com", "message": "covered"},
        {"hash": "dead000", "author_email": "b@co.com", "message": "uncovered"},
    ]
    result = detect_gaps(commits, _make_jira_client({"abc1234": [story]}), "AS360")
    assert result[0].covered is True
    assert result[1].covered is False


# ── Task 5: mailer.py ──────────────────────────────────────────────────────

def _smtp_cfg():
    from tracer.mailer import SmtpConfig
    return SmtpConfig(host="smtp.co.com", port=587, user="u", password="p", from_addr="tracer@co.com")


def _uncovered_commit(email="dev@co.com"):
    from tracer.gap_detector import CommitCoverage
    return CommitCoverage(hash="dead000", author_email=email, message="mystery", covered=False, jira_keys=())


def test_mailer_sends_to_author_and_team():
    from tracer.mailer import send_gap_alert
    uncovered = [_uncovered_commit("dev@co.com")]
    with patch("smtplib.SMTP") as mock_smtp:
        instance = mock_smtp.return_value.__enter__.return_value
        send_gap_alert(uncovered, ["dev@co.com"], ["qa@co.com"], _smtp_cfg(), "AS360")
    instance.starttls.assert_called_once()
    instance.login.assert_called_once_with("u", "p")
    args = instance.sendmail.call_args[0]
    assert "dev@co.com" in args[1]
    assert "qa@co.com" in args[1]


def test_mailer_deduplicates_recipients():
    from tracer.mailer import send_gap_alert
    uncovered = [_uncovered_commit("dev@co.com")]
    with patch("smtplib.SMTP") as mock_smtp:
        instance = mock_smtp.return_value.__enter__.return_value
        send_gap_alert(uncovered, ["dev@co.com"], ["dev@co.com", "qa@co.com"], _smtp_cfg(), "AS360")
    recipients = instance.sendmail.call_args[0][1]
    assert recipients.count("dev@co.com") == 1


def test_mailer_no_send_when_no_uncovered():
    from tracer.mailer import send_gap_alert
    with patch("smtplib.SMTP") as mock_smtp:
        send_gap_alert([], ["dev@co.com"], ["qa@co.com"], _smtp_cfg(), "AS360")
    mock_smtp.assert_not_called()


# ── Task 6: predict_cfg.py ─────────────────────────────────────────────────

def test_predict_cfg_loads_from_env_dict(tmp_path):
    from tracer.predict_cfg import load_predict_config
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JIRA_EMAIL=user@co.com\n"
        "JIRA_API_TOKEN=secret\n"
        "TCM_SESSION=sess123\n"
        "ALERT_EMAILS=qa@co.com,lead@co.com\n",
        encoding="utf-8",
    )
    cfg = load_predict_config(env_file)
    assert cfg.jira_email == "user@co.com"
    assert cfg.jira_api_token == "secret"
    assert cfg.tcm_session == "sess123"
    assert cfg.alert_emails == ["qa@co.com", "lead@co.com"]


def test_predict_cfg_defaults_for_missing_keys(tmp_path):
    from tracer.predict_cfg import load_predict_config
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    cfg = load_predict_config(env_file)
    assert cfg.jira_base_url == "https://sdettech-tea.atlassian.net"
    assert cfg.smtp_port == 587
    assert cfg.alert_emails == []


# ── Task 7: tracer predict subcommand (CLI integration) ───────────────────

def test_predict_writes_test_cases_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scope_file = tmp_path / "scope.json"
    scope_file.write_text(json.dumps(_scope()), encoding="utf-8")

    from tracer.gap_detector import CommitCoverage
    from tracer.tcm_client import TestCase, TestStep
    from tracer.predict_cfg import PredictConfig

    coverage = [CommitCoverage("abc1234", "dev@co.com", "add feature", True, ("AS-1",))]
    tc = TestCase(
        id="tc-1", unique_id="TC-1", jira_story_key="AS-1", jira_story_id="10001",
        status="ACTIVE", approval_status="APPROVED", title="Login test",
        description="Verify login", category="Functional", priority="High",
        automation_status="Manual", test_type="Black Box",
        steps=(TestStep(1, "Open", "App opens"),),
    )
    jira_raw = [{"key": "AS-1", "fields": {"summary": "Add login", "status": {"name": "Done"}, "priority": {"name": "High"}}}]
    groq_payload = {
        "release_summary": "Summary.",
        "qa_notes": [{"screen": "Dashboard", "notes": "Focus.", "risks": "None."}],
        "blind_spots": [],
    }

    with patch("tracer.predict_cfg.load_predict_config",
               return_value=PredictConfig(jira_email="u@co.com", jira_api_token="tok",
                                          tcm_vertical_id="v1")), \
         patch("tracer.jira_client.JiraClient") as mock_jira_cls, \
         patch("tracer.gap_detector.detect_gaps", return_value=coverage), \
         patch("tracer.mailer.send_gap_alert"), \
         patch("tracer.tcm_client.fetch_all", return_value=([], [tc])), \
         patch("tracer.reporter._call_groq", return_value=groq_payload):

        jira_inst = MagicMock()
        jira_inst.fetch_raw_by_keys.return_value = jira_raw
        jira_inst.fetch_defects_for_stories.return_value = []
        mock_jira_cls.return_value = jira_inst

        from tracer.cli import main
        result = main([
            "predict",
            "--scope", str(scope_file),
            "--tcm-vertical", "v1",
            "--no-alert",
        ])

    assert result == 0
    run_dir = next((tmp_path / "runs").iterdir())
    out_file = run_dir / "test_cases.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert isinstance(data["test_cases"], list)
    assert len(data["test_cases"]) == 1
    assert data["test_cases"][0]["unique_id"] == "TC-1"
    assert data["test_cases"][0]["selection_reason"] == "linked"


def test_predict_missing_scope_file_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from tracer.cli import main
    result = main([
        "predict",
        "--scope", str(tmp_path / "nonexistent.json"),
        "--no-alert",
    ])
    assert result != 0


def test_predict_no_linked_cases_returns_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    scope_file = tmp_path / "scope.json"
    scope_file.write_text(json.dumps(_scope()), encoding="utf-8")

    from tracer.gap_detector import CommitCoverage
    from tracer.tcm_client import TestCase
    from tracer.predict_cfg import PredictConfig

    # commit IS covered (has jira key) but no TCM cases linked to that key → fallback
    coverage = [CommitCoverage("abc1234", "dev@co.com", "add feature", True, ("AS-1",))]
    tc_unlinked = TestCase(
        id="tc-2", unique_id="TC-2", jira_story_key="NA", jira_story_id=None,
        status="ACTIVE", approval_status="APPROVED", title="Smoke test",
        description="", category="Smoke", priority="Medium",
        automation_status="Manual", test_type="Black Box", steps=(),
    )
    groq_payload = {
        "release_summary": "Summary.",
        "qa_notes": [{"screen": "Dashboard", "notes": "Focus.", "risks": "None."}],
        "blind_spots": [],
    }

    with patch("tracer.predict_cfg.load_predict_config",
               return_value=PredictConfig(jira_email="u@co.com", jira_api_token="tok",
                                          tcm_vertical_id="v1")), \
         patch("tracer.jira_client.JiraClient") as mock_jira_cls, \
         patch("tracer.gap_detector.detect_gaps", return_value=coverage), \
         patch("tracer.mailer.send_gap_alert"), \
         patch("tracer.tcm_client.fetch_all", return_value=([], [tc_unlinked])), \
         patch("tracer.reporter._call_groq", return_value=groq_payload):

        jira_inst = MagicMock()
        jira_inst.fetch_raw_by_keys.return_value = []
        jira_inst.fetch_defects_for_stories.return_value = []
        mock_jira_cls.return_value = jira_inst

        from tracer.cli import main
        result = main([
            "predict",
            "--scope", str(scope_file),
            "--tcm-vertical", "v1",
            "--no-alert",
        ])

    assert result == 0
    run_dir = next((tmp_path / "runs").iterdir())
    data = json.loads((run_dir / "test_cases.json").read_text(encoding="utf-8"))
    assert len(data["test_cases"]) == 1
    assert data["test_cases"][0]["selection_reason"] == "all"


# ── Task 1: jira_client raw fetch + defect discovery ──────────────────────

def _raw_jira_issue(key="REG-20", issue_type="Story", links=None):
    return {
        "id": "10100",
        "key": key,
        "self": f"https://example.atlassian.net/rest/api/3/issue/10100",
        "fields": {
            "summary": f"Summary for {key}",
            "issuetype": {"name": issue_type},
            "status": {"name": "To Do"},
            "priority": {"name": "Medium"},
            "description": None,
            "customfield_10014": None,
            "issuelinks": links or [],
        },
    }


def _defect_link(defect_key="REG-35"):
    return {
        "id": "10492",
        "type": {"name": "Problem/Incident", "inward": "is caused by", "outward": "causes"},
        "outwardIssue": {
            "id": "19198",
            "key": defect_key,
            "fields": {
                "summary": "Bug: something broken",
                "status": {"name": "To Do"},
                "priority": {"name": "Medium"},
                "issuetype": {"name": "Defects"},
            },
        },
    }


def test_fetch_raw_by_keys_returns_raw_dicts():
    from tracer.jira_client import JiraClient
    client = JiraClient("https://example.atlassian.net", "user@x.com", "token")
    raw = [_raw_jira_issue("REG-20"), _raw_jira_issue("REG-21")]
    with patch.object(client, "_post_search", return_value=raw) as mock_search:
        result = client.fetch_raw_by_keys(["REG-20", "REG-21"])
    assert result == raw
    mock_search.assert_called_once_with('issue in ("REG-20", "REG-21")', 2)


def test_fetch_raw_by_keys_empty_returns_empty():
    from tracer.jira_client import JiraClient
    client = JiraClient("https://example.atlassian.net", "user@x.com", "token")
    result = client.fetch_raw_by_keys([])
    assert result == []


def test_fetch_defects_for_stories_finds_linked_defects():
    from tracer.jira_client import JiraClient
    client = JiraClient("https://example.atlassian.net", "user@x.com", "token")
    story_with_link = _raw_jira_issue("REG-15", links=[_defect_link("REG-35")])
    defect_raw = _raw_jira_issue("REG-35", issue_type="Defects")
    with patch.object(client, "_post_search", side_effect=[
        [story_with_link],   # first call: fetch stories
        [defect_raw],        # second call: fetch defect REG-35
    ]):
        result = client.fetch_defects_for_stories(["REG-15"])
    assert len(result) == 1
    assert result[0]["key"] == "REG-35"


def test_fetch_defects_for_stories_returns_empty_when_no_defect_links():
    from tracer.jira_client import JiraClient
    client = JiraClient("https://example.atlassian.net", "user@x.com", "token")
    story_no_links = _raw_jira_issue("REG-20")
    with patch.object(client, "_post_search", return_value=[story_no_links]):
        result = client.fetch_defects_for_stories(["REG-20"])
    assert result == []


# ── Task 2: tcm fetch_all returns (raw, parsed) tuple ─────────────────────

def _tcm_page(cases, total_pages=1):
    return {"content": cases, "totalPages": total_pages}


def test_fetch_all_returns_raw_and_parsed_tuple():
    from tracer import tcm_client as tcm
    raw_case = _raw_case("REG-20")
    page = _tcm_page([raw_case])
    with patch("tracer.tcm_client._do_request", return_value=page):
        raw, parsed = tcm.fetch_all("vert-1", "REG", "sess", "proj_sess")
    assert isinstance(raw, list)
    assert isinstance(parsed, list)
    assert len(raw) == 1
    assert len(parsed) == 1
    assert raw[0]["uniqueTestcaseId"] == "TC-001"
    assert parsed[0].unique_id == "TC-001"


def test_predict_cfg_loads_groq_api_key(tmp_path):
    from tracer.predict_cfg import load_predict_config
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=gsk_test123\n", encoding="utf-8")
    cfg = load_predict_config(env_file)
    assert cfg.groq_api_key == "gsk_test123"


# ── Task 3: reporter.py ────────────────────────────────────────────────────

def _make_groq_response(release_summary="All good", qa_notes=None, blind_spots=None):
    payload = {
        "release_summary": release_summary,
        "qa_notes": qa_notes or [
            {"screen": "Api Test Suite", "notes": "Focus on suite execution.", "risks": "Runtime chaining."}
        ],
        "blind_spots": blind_spots or ["No concurrent tests"],
    }
    return payload


def test_reporter_generate_writes_report_html(tmp_path):
    from tracer import reporter
    scope = _scope(screens=["Api Test Suite"])
    jira_raw = [_raw_jira_issue("REG-20")]
    defects_raw = []
    tcm_raw = [_raw_case("REG-20")]

    with patch("tracer.reporter._call_groq", return_value=_make_groq_response()):
        reporter.generate(str(tmp_path), scope, [], jira_raw, defects_raw, tcm_raw, "gsk_test")

    report_path = tmp_path / "report.html"
    assert report_path.exists()
    html = report_path.read_text(encoding="utf-8")
    assert "REG-20" in html
    assert "QA Regression Report" in html


def test_reporter_generate_raises_on_groq_failure(tmp_path):
    from tracer import reporter
    scope = _scope()
    with patch("tracer.reporter._call_groq", side_effect=RuntimeError("Groq failed")):
        with pytest.raises(RuntimeError, match="Groq failed"):
            reporter.generate(str(tmp_path), scope, [], [], [], [], "gsk_test")


def test_reporter_call_groq_parses_json_response():
    from tracer import reporter
    resp_payload = {"choices": [{"message": {"content": '{"release_summary": "ok", "qa_notes": [], "blind_spots": []}'}}]}
    resp_bytes = json.dumps(resp_payload).encode()

    class FakeResp:
        def read(self): return resp_bytes
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("urllib.request.urlopen", return_value=FakeResp()):
        result = reporter._call_groq("test prompt", "gsk_key")
    assert result["release_summary"] == "ok"


# ── Task 4: cli.py run dir + raw JSON files ────────────────────────────────

def _make_cli_mocks(jira_raw=None, defects_raw=None, tcm_raw=None):
    if jira_raw is None:
        jira_raw = [_raw_jira_issue("REG-20")]
    if defects_raw is None:
        defects_raw = []
    if tcm_raw is None:
        tcm_raw = [_raw_case("REG-20")]

    mock_coverage = MagicMock()
    mock_coverage.covered = True
    mock_coverage.hash = "abc1234"
    mock_coverage.author_email = "dev@co.com"
    mock_coverage.message = "add feature"
    mock_coverage.jira_keys = ("REG-20",)

    groq_payload = {
        "release_summary": "Test release summary.",
        "qa_notes": [{"screen": "Api Test Suite", "notes": "Focus here.", "risks": "Watch out."}],
        "blind_spots": ["No coverage for concurrent users"],
    }
    return mock_coverage, jira_raw, defects_raw, tcm_raw, groq_payload


def test_predict_creates_runs_directory(tmp_path, monkeypatch):
    from tracer.cli import main
    monkeypatch.chdir(tmp_path)
    scope_file = tmp_path / "scope.json"
    scope_file.write_text(json.dumps(_scope()), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JIRA_EMAIL=x@x.com\nJIRA_API_TOKEN=tok\nGROQ_API_KEY=gsk_test\n"
        "TCM_VERTICAL_ID=vert-1\nTCM_PROJECT_KEY=REG\n",
        encoding="utf-8",
    )
    mock_cov, jira_raw, defects_raw, tcm_raw, groq_payload = _make_cli_mocks()

    with patch("tracer.gap_detector.detect_gaps", return_value=[mock_cov]), \
         patch("tracer.jira_client.JiraClient.fetch_raw_by_keys", return_value=jira_raw), \
         patch("tracer.jira_client.JiraClient.fetch_defects_for_stories", return_value=defects_raw), \
         patch("tracer.tcm_client.fetch_all", return_value=(tcm_raw, [])), \
         patch("tracer.reporter._call_groq", return_value=groq_payload):
        result = main(["predict", "--scope", str(scope_file), "--env", str(env_file),
                       "--jira-project", "REG", "--no-alert"])

    assert result == 0
    runs_dir = tmp_path / "runs"
    assert runs_dir.exists()
    run_subdirs = list(runs_dir.iterdir())
    assert len(run_subdirs) == 1


def test_predict_writes_all_output_files(tmp_path, monkeypatch):
    from tracer.cli import main
    monkeypatch.chdir(tmp_path)
    scope_file = tmp_path / "scope.json"
    scope_file.write_text(json.dumps(_scope()), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JIRA_EMAIL=x@x.com\nJIRA_API_TOKEN=tok\nGROQ_API_KEY=gsk_test\n"
        "TCM_VERTICAL_ID=vert-1\nTCM_PROJECT_KEY=REG\n",
        encoding="utf-8",
    )
    mock_cov, jira_raw, defects_raw, tcm_raw, groq_payload = _make_cli_mocks()

    with patch("tracer.gap_detector.detect_gaps", return_value=[mock_cov]), \
         patch("tracer.jira_client.JiraClient.fetch_raw_by_keys", return_value=jira_raw), \
         patch("tracer.jira_client.JiraClient.fetch_defects_for_stories", return_value=defects_raw), \
         patch("tracer.tcm_client.fetch_all", return_value=(tcm_raw, [])), \
         patch("tracer.reporter._call_groq", return_value=groq_payload):
        main(["predict", "--scope", str(scope_file), "--env", str(env_file),
              "--jira-project", "REG", "--no-alert"])

    run_dir = next((tmp_path / "runs").iterdir())
    assert (run_dir / "scope.json").exists()
    assert (run_dir / "test_cases.json").exists()
    assert (run_dir / "jira_stories.json").exists()
    assert (run_dir / "defects.json").exists()
    assert (run_dir / "sdet360testcases.json").exists()
    assert (run_dir / "report.html").exists()

    jira_stories = json.loads((run_dir / "jira_stories.json").read_text())
    assert jira_stories[0]["key"] == "REG-20"

    defects = json.loads((run_dir / "defects.json").read_text())
    assert defects == []

    tcm = json.loads((run_dir / "sdet360testcases.json").read_text())
    assert tcm[0]["uniqueTestcaseId"] == "TC-001"
