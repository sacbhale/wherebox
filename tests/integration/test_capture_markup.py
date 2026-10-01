"""Capture form markup: voice input on the text form (AC-19), camera and receipt inputs
(plan Step 5 "Inputs", AC-32; the photo tray of spec-multiphoto R13)."""

import re

import pytest

from app.inventory import locations
from tests.inventory_helpers import code_of, open_db


@pytest.fixture
def shelf(auth_client, auth_settings) -> tuple[int, str]:
    with open_db(auth_settings) as conn:
        location_id, _ = locations.create(conn, "Shelf")
        return location_id, code_of(conn, location_id)


def file_input(html: str, input_id: str) -> str:
    match = re.search(rf'<input[^>]*\bid="{input_id}"[^>]*>', html)
    assert match, f"no file input #{input_id}"
    assert 'type="file"' in match.group(0)
    return match.group(0)


def pages(client, shelf) -> dict[str, str]:
    location_id, code = shelf
    return {
        "home panel": client.get("/captures/panel").text,
        "location panel": client.get("/captures/panel", params={"loc": location_id}).text,
        "capture page": client.get(f"/l/{code}/capture").text,
        "reconcile page": client.get(f"/l/{code}/reconcile").text,
    }


def test_text_forms_include_voice_input(auth_client, shelf):
    for name, html in pages(auth_client, shelf).items():
        form = re.search(r'<form[^>]*id="capture-text".*?</form>', html, re.DOTALL)
        assert form, name
        assert 'id="capture-text-input"' in form.group(0), name
        mic = re.search(r'<button[^>]*id="voice-mic"[^>]*>', form.group(0))
        assert mic, name
        # Hidden until voice.js finds SpeechRecognition; a plain button, never a submit.
        assert " hidden" in mic.group(0) and 'type="button"' in mic.group(0), name
        assert 'data-voice-target="#capture-text-input"' in mic.group(0), name
        assert '<script src="/static/voice.js" defer></script>' in html, name


def test_voice_script_posts_text_only(auth_client):
    response = auth_client.get("/static/voice.js")
    assert response.status_code == 200
    script = response.text
    assert "webkitSpeechRecognition" in script and "SpeechRecognition" in script
    assert "interimResults = true" in script and "isFinal" in script
    assert "button.hidden = true" in script  # unsupported browsers: no mic
    # Only text reaches the server: no audio capture or upload APIs.
    for forbidden in ("getUserMedia", "MediaRecorder", "fetch(", "XMLHttpRequest", "FormData"):
        assert forbidden not in script


def test_camera_inputs_use_the_rear_camera(auth_client, shelf):
    html = pages(auth_client, shelf)
    for name, input_id in [
        ("home panel", "capture-photo-input"),
        ("location panel", "capture-photo-input"),
        ("location panel", "capture-reconcile-input"),
        ("reconcile page", "capture-reconcile-input"),
    ]:
        tag = file_input(html[name], input_id)
        assert 'accept="image/*"' in tag and 'capture="environment"' in tag, (name, tag)
        # "Take photo" of the tray: one photo at a time, kept by app.js (no name, R13).
        assert "multiple" not in tag and "name=" not in tag and "data-tray-input" in tag, tag


def test_receipt_input_accepts_pdf_and_has_no_capture(auth_client, shelf):
    html = pages(auth_client, shelf)["home panel"]
    form = re.search(r'<form[^>]*id="capture-receipt".*?</form>', html, re.DOTALL)
    assert form
    tags = re.findall(r'<input type="file"[^>]*>', form.group(0))
    assert len(tags) == 1
    tag = tags[0]
    assert 'accept="image/*,application/pdf"' in tag
    assert "capture=" not in tag
