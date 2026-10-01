"""The comparison report (spec C4): ranked summary, best value (indicative), per-case
drill-down with matched/missed/extra marks and the original capture's model, "Use this
model", past runs on /compare, and the picker/selection partials."""

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.ai import openrouter_catalog
from app.ai.fake import FakeProvider
from app.ai.model_choice import effective_model
from app.ai.provider import ProviderPermanentError
from app.compare import execution, report, runs
from app.config import Settings
from tests.compare_helpers import CATALOG, T0, extract, make_case
from tests.conftest import TEST_PASSWORD, SettingsFactory, logged_in
from tests.inventory_helpers import HX, open_db

GOOD = "test/model-b"
CHEAP = "openai/gpt-5-nano"


@pytest.fixture(autouse=True)
def _fresh_catalog() -> Iterator[None]:
    openrouter_catalog.clear_cache()
    yield
    openrouter_catalog.clear_cache()


@pytest.fixture
def auth_settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(
        household_password=TEST_PASSWORD, openrouter_catalog_file=CATALOG, ai_model="test/model-a"
    )


def _finished_run(settings: Settings) -> int:
    """GOOD finds everything; CHEAP misses, adds extras, miscounts, then fails a case."""
    with open_db(settings) as conn:
        text_case = make_case(conn, settings, [("Milk", 2), "Cordless drill"], model="test/model-a")
        receipt_case = make_case(
            conn, settings, ["Whole milk", "Bread"], capture_type="receipt", model="vendor/old"
        )
        fake = FakeProvider(
            models={
                GOOD: [extract("Whole milk", "Bread"), extract(("Milk", 2), "Cordless drill")],
                CHEAP: [
                    ProviderPermanentError("OpenRouter: insufficient credits"),
                    extract(("Milk", 1), "Hammer"),
                ],
            }
        )
        run_id = runs.start(conn, [CHEAP, GOOD], [receipt_case, text_case], 1234, T0)
        while execution.run_once(conn, settings, fake.with_model):
            pass
        assert runs.get(conn, run_id)["status"] == "done"
    return run_id


def _summary_models(html: str) -> list[str]:
    table = html.split('id="compare-summary"', 1)[1].split("</table>", 1)[0]
    return re.findall(r'<tr data-model-id="([^"]+)" data-rank', table)


def test_report_ranks_and_drills_down(auth_client: TestClient, auth_settings: Settings) -> None:
    run_id = _finished_run(auth_settings)
    response = auth_client.get(f"/compare/runs/{run_id}")
    assert response.status_code == 200
    html = response.text
    assert _summary_models(html) == [GOOD, CHEAP]  # the better model first
    good_row = html.split(f'<tr data-model-id="{GOOD}"', 1)[1].split("</tr>", 1)[0]
    assert "<td>1.00</td>" in good_row and "best value (indicative)" in good_row
    cheap_row = html.split(f'<tr data-model-id="{CHEAP}"', 1)[1].split("</tr>", 1)[0]
    cells = re.findall(r"<td>([^<]*)</td>", cheap_row)
    # F1: failed receipt = 0, text case 1 of 2 found with 1 extra = 0.5 -> mean 0.25.
    assert cells[:5] == ["2", "0.25", "1", "3", "1"]  # rank, F1, found, missed, extra
    assert cells[-2:] == ["1", "2"]  # failures, n
    assert 'id="compare-indicative"' in html
    assert "n = 2 cases" in html
    assert 'class="table-scroll"' in html
    # Drill-down: expected list, marks, the original capture's model.
    assert '<code class="original-model">vendor/old</code>' in html
    assert '<code class="original-model">test/model-a</code>' in html
    assert 'class="line-matched"' in html and 'class="line-extra"' in html
    assert 'class="line-missed"' in html and "wrong quantity" in html
    assert "Failed: OpenRouter: insufficient credits" in html
    # The run has a receipt case: price accuracy is shown (no expected prices here: a dash).
    assert "<th>Price acc.</th>" in html
    assert '<td class="price-accuracy">\u2013</td>' in good_row
    # Neither compared model made an original capture.
    assert "original captures</span>" not in html.split('id="compare-summary"', 1)[1]


def test_report_marks_the_original_model_and_hides_price_accuracy(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    with open_db(auth_settings) as conn:
        case_id = make_case(conn, auth_settings, ["Milk"], model="test/model-a")
        fake = FakeProvider(repeat_last=True, models={GOOD: [extract("Milk")]})
        fake_a = FakeProvider(repeat_last=True, models={"test/model-a": [extract("Milk")]})
        run_id = runs.start(conn, ["test/model-a", GOOD], [case_id], 0, T0)
        providers = {"test/model-a": fake_a.with_model("test/model-a"), GOOD: fake.with_model(GOOD)}
        while execution.run_once(conn, auth_settings, providers.get):
            pass
    html = auth_client.get(f"/compare/runs/{run_id}").text
    summary = html.split('id="compare-summary"', 1)[1].split("</table>", 1)[0]
    original = summary.split('<tr data-model-id="test/model-a"', 1)[1].split("</tr>", 1)[0]
    other = summary.split(f'<tr data-model-id="{GOOD}"', 1)[1].split("</tr>", 1)[0]
    assert "original captures</span>" in original and "original captures" not in other
    assert "Price acc." not in summary


def test_use_this_model(
    auth_client: TestClient, auth_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = _finished_run(auth_settings)
    html = auth_client.get(f"/compare/runs/{run_id}").text
    assert html.count("Use this model") == 2
    builds: list[int] = []
    original_build = report.build

    def counting_build(*args, **kwargs):
        builds.append(1)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(report, "build", counting_build)
    response = auth_client.post(f"/compare/runs/{run_id}/use", data={"model": GOOD}, headers=HX)
    assert response.status_code == 200
    assert len(builds) == 1  # the report is built once
    assert response.text.lstrip().startswith('<div id="compare-summary">')
    assert '<span class="active-model">Active</span>' in response.text
    assert response.text.count("Use this model") == 1
    with open_db(auth_settings) as conn:
        assert effective_model(conn, auth_settings) == GOOD
    assert f"fake / {GOOD}" in auth_client.get("/settings").text

    bad = auth_client.post(
        f"/compare/runs/{run_id}/use", data={"model": "test/model-a"}, headers=HX
    )
    assert bad.status_code == 400
    missing = auth_client.post("/compare/runs/999/use", data={"model": GOOD}, headers=HX)
    assert missing.status_code == 404


def test_use_hidden_when_model_not_selectable(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        household_password=TEST_PASSWORD,
        ai_provider="anthropic",
        ai_api_key="sk-ant-x",
        openrouter_api_key="sk-or-v1-x",
        openrouter_catalog_file=CATALOG,
    )
    with logged_in(settings) as client:
        run_id = _finished_run(settings)
        html = client.get(f"/compare/runs/{run_id}").text
        assert _summary_models(html) == [GOOD, CHEAP]
        assert "Use this model" not in html
        use = client.post(f"/compare/runs/{run_id}/use", data={"model": GOOD}, headers=HX)
        assert use.status_code == 404


def test_past_runs_and_running_report(auth_client: TestClient, auth_settings: Settings) -> None:
    done = _finished_run(auth_settings)
    with open_db(auth_settings) as conn:
        case_id = make_case(conn, auth_settings, ["Eggs"])
        running = runs.start(conn, [GOOD, CHEAP], [case_id], 0, T0)
    page = auth_client.get("/compare").text
    assert f'href="/compare/runs/{done}"' in page and f'href="/compare/runs/{running}"' in page
    assert "4/4 calls" in page and "0/2 calls" in page
    assert f'id="run-{running}"' in page  # the running run's status polls on the page
    report_html = auth_client.get(f"/compare/runs/{running}").text
    assert "Not run yet." in report_html
    assert 'hx-trigger="every 2s"' in report_html
    assert auth_client.get("/compare/runs/999").status_code == 404


def test_model_search_partial(auth_client: TestClient) -> None:
    response = auth_client.get("/compare/models", params={"q": "model"})
    assert response.status_code == 200
    ids = re.findall(r'data-model-id="([^"]+)"', response.text)
    assert "test/model-a" in ids and "test/model-b" in ids
    assert "test/text-only" not in ids and "test/price-unknown" not in ids
    everything = auth_client.get("/compare/models", params={"q": "text only", "all": "1"})
    assert 'data-model-id="test/text-only"' in everything.text
    free = auth_client.get("/compare/models", params={"q": "free", "all": "1"}).text
    assert 'data-model-id="openrouter/free"' not in free  # routers are never offered
    assert 'data-model-id="qwen/qwen3.8-27b:free"' in free
    assert 'hx-vals=\'{"add": "test/model-a"}\'' in response.text


def test_selection_partial(auth_client: TestClient) -> None:
    add = auth_client.get(
        "/compare/selection",
        params={"models": ["test/model-a"], "add": GOOD, "cases": ["1", "2"]},
    )
    assert add.status_code == 200
    chosen = re.findall(r'name="models" value="([^"]+)"', add.text)
    assert chosen == ["test/model-a", GOOD]
    # 2 cases x (1550 + 3100) micros with the default 1,500 in / 400 out tokens.
    assert "Rough estimate: <strong>$0.0093</strong>" in add.text
    removed = auth_client.get(
        "/compare/selection", params={"models": ["test/model-a", GOOD], "remove": "test/model-a"}
    )
    assert re.findall(r'name="models" value="([^"]+)"', removed.text) == [GOOD]
    junk = auth_client.get(
        "/compare/selection",
        params={"models": ["openrouter/auto", "vendor/unknown", "<x>", GOOD, GOOD]},
    )
    assert re.findall(r'name="models" value="([^"]+)"', junk.text) == [GOOD]
    many = ["test/model-a", GOOD, CHEAP, "google/gemini-3.1-flash-lite", "amazon/nova-lite-v1"]
    capped = auth_client.get(
        "/compare/selection", params={"models": many, "add": "anthropic/claude-sonnet-5.5"}
    )
    assert len(re.findall(r'name="models"', capped.text)) == 5
