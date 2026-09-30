"""Photo capture with status polling that stops on 286 (AC-7/AC-15), and voice capture with a
stubbed SpeechRecognition that posts only text (AC-19)."""

import json
import time
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from playwright.sync_api import Page, Request, Response, expect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
STEPS = json.loads((FIXTURES / "fake_script.json").read_text())["steps"]
SHELF_ITEMS = [item["name"] for item in STEPS[0]["result"]["items"]]
# The shelf step, slow enough that the status partial polls at least once before it is done.
SLOW_SHELF = STEPS[0] | {"delay_s": 3}


@pytest.mark.fake_steps(SLOW_SHELF)
def test_photo_capture_polls_until_done_then_stops(server, page: Page) -> None:
    garage_id, code = server.create_location("Garage")
    server.login(page, f"/l/{code}")
    polls: list[tuple[float, str]] = []
    answers: list[tuple[float, int]] = []

    def on_request(request: Request) -> None:
        if request.url.endswith("/status"):
            polls.append((time.monotonic(), request.url))

    def on_response(response: Response) -> None:
        if response.url.endswith("/status"):
            answers.append((time.monotonic(), response.status))

    page.on("request", on_request)
    page.on("response", on_response)

    # Choosing the file submits the form (as after taking the photo on a phone).
    page.set_input_files("#capture-photo-input", str(FIXTURES / "shelf.jpg"))
    status = page.locator("#capture-results .capture-status")
    expect(status).to_have_attribute("data-capture-status", "done", timeout=20_000)
    expect(status).to_contain_text("Added 12 items to Garage")

    assert [code for _, code in answers].count(286) == 1
    assert answers[-1][1] == 286
    assert 200 in [code for _, code in answers]  # it did poll while processing
    stopped_at = answers[-1][0]
    page.wait_for_timeout(5_000)
    assert [url for at, url in polls if at > stopped_at] == []

    page.reload()
    rows = page.locator("#location-items .item-name")
    expect(rows).to_have_count(12)
    for name in SHELF_ITEMS:
        expect(page.locator("#location-items")).to_contain_text(name)
    (row,) = server.query("SELECT count(*) AS n FROM items WHERE location_id = ?", garage_id)
    assert row["n"] == 12


TRANSCRIPT = "two cartons of milk and the drill is in the garage"

# A SpeechRecognition that "hears" TRANSCRIPT; getUserMedia is recorded to prove no audio
# is captured by the page itself.
SPEECH_STUB = """
(() => {
  window.__mediaRequests = 0;
  if (navigator.mediaDevices) {
    navigator.mediaDevices.getUserMedia = () => {
      window.__mediaRequests += 1;
      return Promise.reject(new Error("no audio in tests"));
    };
  }
  class FakeRecognition {
    start() {
      setTimeout(() => {
        const result = [{ transcript: "TRANSCRIPT", confidence: 0.9 }];
        result.isFinal = true;
        if (this.onresult) this.onresult({ results: [result], resultIndex: 0 });
        if (this.onend) this.onend();
      }, 50);
    }
    stop() { if (this.onend) this.onend(); }
    abort() {}
  }
  for (const name of ["SpeechRecognition", "webkitSpeechRecognition"]) {
    Object.defineProperty(window, name, { value: FakeRecognition, configurable: true });
  }
})();
""".replace("TRANSCRIPT", TRANSCRIPT)


@pytest.mark.fake_steps(2)
def test_voice_fills_textarea_and_posts_only_text(server, page: Page) -> None:
    server.create_location("Garage")
    pantry_id, code = server.create_location("Pantry")
    page.add_init_script(SPEECH_STUB)
    server.login(page, f"/l/{code}")
    sent: list[Request] = []
    page.on("request", lambda request: sent.append(request))

    mic = page.locator("#voice-mic")
    expect(mic).to_be_visible()
    mic.tap()
    expect(page.locator("#capture-text-input")).to_have_value(TRANSCRIPT)
    page.locator("#capture-text button[type=submit]").tap()
    status = page.locator("#capture-results .capture-status")
    expect(status).to_have_attribute("data-capture-status", "done", timeout=20_000)

    posts = [r for r in sent if r.method != "GET"]
    assert [r.url for r in posts] == [server.url("/captures/text")]
    (post,) = posts
    assert post.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert parse_qs(post.post_data) == {"location_id": [str(pantry_id)], "text": [TRANSCRIPT]}
    for request in sent:
        content_type = request.headers.get("content-type", "")
        assert not content_type.startswith(("multipart/", "audio/")), request.url
    assert page.evaluate("window.__mediaRequests") == 0

    items = {r["name"]: r for r in server.query("SELECT name, quantity, location_id FROM items")}
    assert set(items) == {"Milk", "Cordless drill"}
    assert (items["Milk"]["quantity"], items["Milk"]["location_id"]) == (2, pantry_id)
