"""Phase 2a — deterministic predict pipeline tests. No network, no Groq."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

# Pre-import tracer.cli so its module-level bindings are established before any
# patch() context managers run. Without this, the first test that does
#   `from tracer.cli import main` inside a patch context could bind module-level
#   names (e.g. JiraClient) to mock objects, polluting later tests.
import tracer.cli  # noqa: F401


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
         patch("tracer.llm_config.call_llm_api", return_value=groq_payload):

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
         patch("tracer.llm_config.call_llm_api", return_value=groq_payload):

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
    # step= is log metadata only (names the operation in the Step column); the
    # jql and max_results are what actually drive the query.
    mock_search.assert_called_once_with('issue in ("REG-20", "REG-21")', 2,
                                        step="FetchRawByKeys")


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
    from tracer.llm_config import LLMConfig
    scope = _scope(screens=["Api Test Suite"])
    jira_raw = [_raw_jira_issue("REG-20")]
    defects_raw = []
    tcm_raw = [_raw_case("REG-20")]
    cfg = LLMConfig(provider="groq", model="llama-3.3-70b-versatile", api_key="gsk_test")

    with patch("tracer.llm_config.call_llm_api", return_value=_make_groq_response()):
        reporter.generate(str(tmp_path), scope, [], jira_raw, defects_raw, tcm_raw, cfg)

    report_path = tmp_path / "report.html"
    assert report_path.exists()
    html = report_path.read_text(encoding="utf-8")
    assert "REG-20" in html
    assert "QA Regression Report" in html


def test_reporter_generate_raises_on_groq_failure(tmp_path):
    from tracer import reporter
    from tracer.llm_config import LLMConfig
    scope = _scope()
    cfg = LLMConfig(provider="groq", model="llama-3.3-70b-versatile", api_key="gsk_test")
    with patch("tracer.llm_config.call_llm_api", side_effect=RuntimeError("Groq failed")):
        with pytest.raises(RuntimeError, match="Groq failed"):
            reporter.generate(str(tmp_path), scope, [], [], [], [], cfg)


def test_reporter_call_llm_api_parses_json_response():
    from tracer.llm_config import call_llm_api, LLMConfig
    resp_payload = {"choices": [{"message": {"content": '{"release_summary": "ok", "qa_notes": [], "blind_spots": []}'}}]}
    resp_bytes = json.dumps(resp_payload).encode()

    class FakeResp:
        def read(self): return resp_bytes
        def __enter__(self): return self
        def __exit__(self, *a): pass

    cfg = LLMConfig(provider="groq", model="llama-3.3-70b-versatile", api_key="gsk_key")
    with patch("urllib.request.urlopen", return_value=FakeResp()):
        result = call_llm_api("test prompt", cfg)
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
         patch("tracer.llm_config.call_llm_api", return_value=groq_payload):
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
         patch("tracer.llm_config.call_llm_api", return_value=groq_payload):
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


# ── Task 1: jira_client.fetch_open_stories ─────────────────────────────────

def test_fetch_open_stories_posts_correct_jql():
    from tracer.jira_client import JiraClient
    client = JiraClient("https://example.atlassian.net", "user@x.com", "token")
    captured = {}

    class FakeResp:
        def read(self): return json.dumps({"issues": []}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def fake_urlopen(req):
        captured["body"] = json.loads(req.data.decode())
        return FakeResp()

    with patch("tracer.jira_client.urllib.request.urlopen", side_effect=fake_urlopen):
        result = client.fetch_open_stories("REG")

    jql = captured["body"]["jql"]
    assert 'status in ("To Do","In Progress")' in jql
    assert 'issuetype=Story' in jql
    assert 'project="REG"' in jql
    assert result == []


def test_fetch_open_stories_returns_raw_dicts():
    from tracer.jira_client import JiraClient
    client = JiraClient("https://example.atlassian.net", "user@x.com", "token")
    raw_story = {"id": "1", "key": "REG-10", "fields": {"summary": "Story A"}}

    class FakeResp:
        def read(self): return json.dumps({"issues": [raw_story]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): pass

    with patch("tracer.jira_client.urllib.request.urlopen", return_value=FakeResp()):
        result = client.fetch_open_stories("REG")

    assert result == [raw_story]


# ── Task 2: semantic_matcher.enrich_layer3 ─────────────────────────────────

def _make_coverage(hash_="abc1234", covered=False, jira_keys=()):
    from tracer.gap_detector import CommitCoverage
    return CommitCoverage(
        hash=hash_,
        author_email="dev@co.com",
        message="add feature X",
        covered=covered,
        jira_keys=tuple(jira_keys),
    )


def _open_story(key="REG-10", summary="Feature X"):
    return {"id": "1", "key": key, "fields": {"summary": summary, "status": {"name": "To Do"}}}


def test_enrich_layer3_auto_includes_high_score():
    from tracer.semantic_matcher import enrich_layer3
    from tracer.llm_config import LLMConfig

    uncovered = [_make_coverage("abc1234", covered=False)]
    llm_response = {"scores": [{"key": "REG-10", "score": 8, "reason": "direct match"}]}

    mock_jira = MagicMock()
    mock_jira.fetch_open_stories.return_value = [_open_story("REG-10")]

    with patch("tracer.semantic_matcher.call_llm_api", return_value=llm_response):
        included, alerts = enrich_layer3(
            uncovered, [{"name": "featureX", "risk": "HIGH"}], ["Dashboard"],
            mock_jira, "REG", LLMConfig("openai", "gpt-4o", "key"),
        )

    assert len(included) == 1
    assert included[0].key == "REG-10"
    assert included[0].score == 8
    assert included[0].confidence == "semantic"
    assert alerts == []


def test_enrich_layer3_alerts_on_mid_score():
    from tracer.semantic_matcher import enrich_layer3
    from tracer.llm_config import LLMConfig

    uncovered = [_make_coverage("abc1234")]
    llm_response = {"scores": [{"key": "REG-10", "score": 5, "reason": "partial match"}]}

    mock_jira = MagicMock()
    mock_jira.fetch_open_stories.return_value = [_open_story("REG-10")]

    with patch("tracer.semantic_matcher.call_llm_api", return_value=llm_response):
        included, alerts = enrich_layer3(
            uncovered, [], [], mock_jira, "REG",
            LLMConfig("openai", "gpt-4o", "key"),
        )

    assert included == []
    assert len(alerts) == 1
    assert alerts[0].key == "REG-10"
    assert alerts[0].confidence == "suggested"


def test_enrich_layer3_discards_low_score():
    from tracer.semantic_matcher import enrich_layer3
    from tracer.llm_config import LLMConfig

    uncovered = [_make_coverage("abc1234")]
    llm_response = {"scores": [{"key": "REG-10", "score": 2, "reason": "unrelated"}]}

    mock_jira = MagicMock()
    mock_jira.fetch_open_stories.return_value = [_open_story("REG-10")]

    with patch("tracer.semantic_matcher.call_llm_api", return_value=llm_response):
        included, alerts = enrich_layer3(
            uncovered, [], [], mock_jira, "REG",
            LLMConfig("openai", "gpt-4o", "key"),
        )

    assert included == []
    assert alerts == []


def test_enrich_layer3_skips_when_no_uncovered_commits():
    from tracer.semantic_matcher import enrich_layer3
    from tracer.llm_config import LLMConfig

    mock_jira = MagicMock()

    with patch("tracer.semantic_matcher.call_llm_api") as mock_llm:
        included, alerts = enrich_layer3(
            [], [], [], mock_jira, "REG",
            LLMConfig("openai", "gpt-4o", "key"),
        )

    mock_jira.fetch_open_stories.assert_not_called()
    mock_llm.assert_not_called()
    assert included == []
    assert alerts == []


def test_enrich_layer3_skips_when_no_open_stories():
    from tracer.semantic_matcher import enrich_layer3
    from tracer.llm_config import LLMConfig

    uncovered = [_make_coverage("abc1234")]
    mock_jira = MagicMock()
    mock_jira.fetch_open_stories.return_value = []

    with patch("tracer.semantic_matcher.call_llm_api") as mock_llm:
        included, alerts = enrich_layer3(
            uncovered, [], [], mock_jira, "REG",
            LLMConfig("openai", "gpt-4o", "key"),
        )

    mock_llm.assert_not_called()
    assert included == []
    assert alerts == []


# ── Task 3: semantic_matcher.enrich_layer4 ─────────────────────────────────

def _unlinked_tc(uid="REGTC-55", title="Verify filter behaviour"):
    return {
        "id": "tc-055",
        "uniqueTestcaseId": uid,
        "jiraStoryKey": "NA",
        "testcaseStatus": "ACTIVE",
        "customFieldValues": [
            {"fieldDefinition": {"name": "TEST_CASE_TITLE"}, "value": title},
        ],
        "testSteps": [],
    }


def test_enrich_layer4_auto_includes_high_score():
    from tracer.semantic_matcher import enrich_layer4
    from tracer.llm_config import LLMConfig

    tcs = [_unlinked_tc("REGTC-55", "Verify filter behaviour")]
    llm_response = {"scores": [{"key": "REGTC-55", "score": 7, "reason": "direct match"}]}

    with patch("tracer.semantic_matcher.call_llm_api", return_value=llm_response):
        included, alerts = enrich_layer4(
            tcs, ["Dashboard"], [{"name": "getFilters", "risk": "HIGH"}],
            ["REG-20: API Test Suite"], LLMConfig("openai", "gpt-4o", "key"),
        )

    assert len(included) == 1
    assert included[0].key == "REGTC-55"
    assert included[0].confidence == "semantic"
    assert alerts == []


def test_enrich_layer4_alerts_on_mid_score():
    from tracer.semantic_matcher import enrich_layer4
    from tracer.llm_config import LLMConfig

    tcs = [_unlinked_tc("REGTC-61", "Verify notification")]
    llm_response = {"scores": [{"key": "REGTC-61", "score": 6, "reason": "indirect"}]}

    with patch("tracer.semantic_matcher.call_llm_api", return_value=llm_response):
        included, alerts = enrich_layer4(
            tcs, [], [], [], LLMConfig("openai", "gpt-4o", "key"),
        )

    assert included == []
    assert len(alerts) == 1
    assert alerts[0].confidence == "suggested"


def test_enrich_layer4_discards_low_score():
    from tracer.semantic_matcher import enrich_layer4
    from tracer.llm_config import LLMConfig

    tcs = [_unlinked_tc("REGTC-70", "Unrelated TC")]
    llm_response = {"scores": [{"key": "REGTC-70", "score": 2, "reason": "unrelated"}]}

    with patch("tracer.semantic_matcher.call_llm_api", return_value=llm_response):
        included, alerts = enrich_layer4(
            tcs, [], [], [], LLMConfig("openai", "gpt-4o", "key"),
        )

    assert included == []
    assert alerts == []


def test_enrich_layer4_skips_when_no_unlinked_tcs():
    from tracer.semantic_matcher import enrich_layer4
    from tracer.llm_config import LLMConfig

    with patch("tracer.semantic_matcher.call_llm_api") as mock_llm:
        included, alerts = enrich_layer4(
            [], [], [], [], LLMConfig("openai", "gpt-4o", "key"),
        )

    mock_llm.assert_not_called()
    assert included == []
    assert alerts == []


# ── Task 4: CLI integration — semantic enrichment wired ────────────────────

def test_cli_predict_writes_semantic_fields(tmp_path):
    scope = _scope(
        commits=[{"hash": "abc1234ef", "author_email": "dev@co.com", "message": "add filter"}]
    )
    scope_file = tmp_path / "scope.json"
    scope_file.write_text(json.dumps(scope))

    from tracer.gap_detector import CommitCoverage
    from tracer.semantic_matcher import SemanticMatch
    from tracer.predict_cfg import PredictConfig

    uncovered_commit = CommitCoverage(
        hash="abc1234ef", author_email="dev@co.com",
        message="add filter", covered=False, jira_keys=(),
    )
    semantic_story_raw = {"id": "2", "key": "REG-10", "fields": {
        "summary": "Filter feature", "status": {"name": "To Do"},
        "priority": {"name": "Medium"}, "issuetype": {"name": "Story"},
        "issuelinks": [],
    }}
    unlinked_raw = {
        "id": "tc-055", "uniqueTestcaseId": "REGTC-55",
        "jiraStoryKey": "NA", "testcaseStatus": "ACTIVE",
        "customFieldValues": [
            {"fieldDefinition": {"name": "TEST_CASE_TITLE"}, "value": "Verify filter"},
        ],
        "testSteps": [],
    }

    l3_result = ([SemanticMatch(key="REG-10", score=8, reason="match", confidence="semantic")], [])
    l4_result = ([SemanticMatch(key="REGTC-55", score=7, reason="filter", confidence="semantic")], [])

    with patch("tracer.cli.gd.detect_gaps", return_value=[uncovered_commit]), \
         patch("tracer.cli.JiraClient") as MockJira, \
         patch("tracer.cli.tcm.fetch_all", return_value=([unlinked_raw], [])), \
         patch("tracer.cli.reportermod.generate"), \
         patch("tracer.cli.mailermod.send_gap_alert"), \
         patch("tracer.cli.enrich_layer3", return_value=l3_result), \
         patch("tracer.cli.enrich_layer4", return_value=l4_result), \
         patch("tracer.cli.load_predict_config", return_value=PredictConfig(
             jira_base_url="https://x.atlassian.net", jira_email="u@x.com",
             jira_api_token="t", tcm_session="s", tcm_project_session="ps",
             tcm_refresh_token="r", tcm_vertical_id="v1", tcm_project_key="REG",
             smtp_host="", smtp_port=587, smtp_user="", smtp_password="",
             smtp_from="", alert_emails=[], groq_api_key="",
         )), \
         patch("tracer.llm_config.call_llm_api", return_value={"release_summary": "r", "qa_notes": [], "blind_spots": []}):

        mock_jira_inst = MockJira.return_value
        mock_jira_inst.fetch_raw_by_keys.return_value = [semantic_story_raw]
        mock_jira_inst.fetch_defects_for_stories.return_value = []

        from tracer.cli import main
        ret = main([
            "predict", "--scope", str(scope_file),
            "--env", str(tmp_path / ".env"),
            "--no-alert", "--jira-project", "REG",
        ])

    assert ret == 0
    import os
    runs_dir = Path("runs")
    run_dirs = sorted(runs_dir.iterdir()) if runs_dir.exists() else []
    assert run_dirs, "no run dir created"
    output = json.loads((run_dirs[-1] / "test_cases.json").read_text())

    assert "semantic_test_cases" in output
    assert "semantic_alerts" in output
    assert any(tc["unique_id"] == "REGTC-55" for tc in output["semantic_test_cases"])


# ── Task 5: reporter.py semantic badge rendering ───────────────────────────

def test_reporter_renders_semantic_badge(tmp_path):
    from tracer import reporter as reportermod
    from tracer.semantic_matcher import SemanticMatch
    from tracer.llm_config import LLMConfig
    from unittest.mock import patch

    llm_response = {
        "release_summary": "Test release",
        "qa_notes": [],
        "blind_spots": [],
    }
    semantic_tc = SemanticMatch(key="REGTC-55", score=8, reason="Directly tests changed area", confidence="semantic")
    llm_cfg = LLMConfig(provider="openai", model="gpt-4o", api_key="test")

    with patch("tracer.llm_config.call_llm_api", return_value=llm_response):
        reportermod.generate(
            str(tmp_path), {"base": "main", "target": "feat", "commits": [], "changed_symbols": [], "affected_screens": []},
            [], [], [], [],
            llm_cfg,
            semantic_tcs=[semantic_tc],
            semantic_alerts=[],
        )

    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "SEMANTIC" in html
    assert "REGTC-55" in html


def test_reporter_no_semantic_section_when_empty(tmp_path):
    from tracer import reporter as reportermod
    from tracer.llm_config import LLMConfig
    from unittest.mock import patch

    llm_response = {"release_summary": "Test", "qa_notes": [], "blind_spots": []}
    llm_cfg = LLMConfig(provider="openai", model="gpt-4o", api_key="test")

    with patch("tracer.llm_config.call_llm_api", return_value=llm_response):
        reportermod.generate(
            str(tmp_path), {"base": "main", "target": "feat", "commits": [], "changed_symbols": [], "affected_screens": []},
            [], [], [], [],
            llm_cfg,
            semantic_tcs=[],
            semantic_alerts=[],
        )

    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "semantic-alert" not in html


# ── Gap alert email redesign ───────────────────────────────────────────────

def test_build_gap_narrative_returns_summary_and_areas():
    from tracer.mailer import build_gap_narrative
    from tracer.gap_detector import CommitCoverage
    from tracer.llm_config import LLMConfig
    from unittest.mock import patch

    llm_response = {
        "summary": "Two changes were made outside planned work.",
        "unplanned_areas": [
            {"name": "Notification Module", "detail": "Delivery logic changed.", "business_impact": "Users may see different alerts."}
        ],
    }
    uncovered = [CommitCoverage("abc123", "fix: update delivery", "dev@test.com", set(), False)]
    open_stories = [{"key": "REG-1", "fields": {"summary": "API suite", "status": {"name": "In Progress"}}}]
    llm_cfg = LLMConfig(provider="openai", model="gpt-4o", api_key="test")

    with patch("tracer.mailer.call_llm_api", return_value=llm_response):
        result = build_gap_narrative(uncovered, open_stories, {}, "REG", llm_cfg)

    assert result["summary"] == "Two changes were made outside planned work."
    assert len(result["unplanned_areas"]) == 1
    assert result["unplanned_areas"][0]["name"] == "Notification Module"


def test_build_gap_narrative_falls_back_on_llm_error():
    from tracer.mailer import build_gap_narrative
    from tracer.gap_detector import CommitCoverage
    from tracer.llm_config import LLMConfig
    from unittest.mock import patch

    uncovered = [CommitCoverage("abc123", "fix: update delivery", "dev@test.com", set(), False)]
    llm_cfg = LLMConfig(provider="openai", model="gpt-4o", api_key="test")

    with patch("tracer.mailer.call_llm_api", side_effect=RuntimeError("LLM down")):
        result = build_gap_narrative(uncovered, [], {}, "REG", llm_cfg)

    assert result["summary"] is None
    assert result["unplanned_areas"] == []


def test_send_gap_alert_sends_html_when_llm_succeeds():
    from tracer.mailer import send_gap_alert, SmtpConfig
    from tracer.gap_detector import CommitCoverage
    from tracer.llm_config import LLMConfig
    from unittest.mock import patch, MagicMock

    llm_response = {
        "summary": "One unplanned change detected.",
        "unplanned_areas": [
            {"name": "Report Module", "detail": "Output format changed.", "business_impact": "Dashboards may look different."}
        ],
    }
    uncovered = [CommitCoverage("abc123", "fix: report format", "dev@test.com", set(), False)]
    smtp_cfg = SmtpConfig(host="smtp.test.com", port=587, user="u", password="p", from_addr="from@test.com")
    llm_cfg = LLMConfig(provider="openai", model="gpt-4o", api_key="test")
    jira_client = MagicMock()
    jira_client.fetch_open_stories.return_value = []

    sent_messages = []

    class FakeSMTP:
        def __init__(self, host, port): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): pass
        def login(self, u, p): pass
        def sendmail(self, from_addr, recipients, msg_str):
            sent_messages.append(msg_str)

    with patch("tracer.mailer.call_llm_api", return_value=llm_response), \
         patch("tracer.mailer.smtplib.SMTP", FakeSMTP):
        send_gap_alert(
            uncovered, ["dev@test.com"], ["qa@test.com"], smtp_cfg, "REG",
            jira_client=jira_client, scope_data={}, llm_cfg=llm_cfg,
            jira_base_url="https://example.atlassian.net",
        )

    import base64, email as _email
    assert len(sent_messages) == 1
    assert "text/html" in sent_messages[0]
    parsed = _email.message_from_string(sent_messages[0])
    payload = parsed.get_payload(decode=True)
    body = payload.decode("utf-8") if payload else sent_messages[0]
    assert "Not Planned" in body


def test_send_gap_alert_falls_back_to_plain_text_when_llm_fails():
    from tracer.mailer import send_gap_alert, SmtpConfig
    from tracer.gap_detector import CommitCoverage
    from tracer.llm_config import LLMConfig
    from unittest.mock import patch, MagicMock

    uncovered = [CommitCoverage("abc123", "fix: something", "dev@test.com", set(), False)]
    smtp_cfg = SmtpConfig(host="smtp.test.com", port=587, user="u", password="p", from_addr="from@test.com")
    llm_cfg = LLMConfig(provider="openai", model="gpt-4o", api_key="test")
    jira_client = MagicMock()
    jira_client.fetch_open_stories.return_value = []

    sent_messages = []

    class FakeSMTP:
        def __init__(self, host, port): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): pass
        def login(self, u, p): pass
        def sendmail(self, from_addr, recipients, msg_str):
            sent_messages.append(msg_str)

    with patch("tracer.mailer.call_llm_api", side_effect=RuntimeError("LLM down")), \
         patch("tracer.mailer.smtplib.SMTP", FakeSMTP):
        send_gap_alert(
            uncovered, ["dev@test.com"], ["qa@test.com"], smtp_cfg, "REG",
            jira_client=jira_client, scope_data={}, llm_cfg=llm_cfg,
        )

    assert len(sent_messages) == 1
    assert "text/plain" in sent_messages[0]


def test_send_gap_alert_noop_when_uncovered_empty():
    from tracer.mailer import send_gap_alert, SmtpConfig
    from unittest.mock import patch

    smtp_cfg = SmtpConfig(host="smtp.test.com", port=587, user="u", password="p", from_addr="from@test.com")

    with patch("tracer.mailer.smtplib.SMTP") as mock_smtp:
        send_gap_alert([], [], ["qa@test.com"], smtp_cfg, "REG")

    mock_smtp.assert_not_called()
