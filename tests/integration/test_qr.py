"""AC-11: QR codes encode exactly PUBLIC_BASE_URL/l/CODE; PNG download and print label work."""

import io

import pytest
import zxingcpp
from PIL import Image

from app import qr
from tests.conftest import TEST_PASSWORD, logged_in
from tests.inventory_helpers import code_of, make_chain, open_db

PUBLIC = "https://inv.example.com:8443"


@pytest.fixture
def lan_client(make_settings):
    """Public URL differs from the Host the request is made to (LAN access)."""
    settings = make_settings(household_password=TEST_PASSWORD, public_base_url=PUBLIC)
    with logged_in(
        settings, base_url="http://nas.local:8000", headers={"Origin": "http://nas.local:8000"}
    ) as client:
        with open_db(settings) as conn:
            (loc,) = make_chain(conn, ["Garage"])
            client.code = code_of(conn, loc)
        yield client


def decode(png_bytes: bytes) -> str:
    results = zxingcpp.read_barcodes(Image.open(io.BytesIO(png_bytes)))
    assert len(results) == 1
    assert results[0].format == zxingcpp.BarcodeFormat.QRCode
    return results[0].text


def test_png_decodes_to_public_url_and_is_attachment(lan_client):
    code = lan_client.code
    response = lan_client.get(f"/l/{code}/qr.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="location-{code}.png"'
    )
    assert decode(response.content) == f"{PUBLIC}/l/{code}"


def test_lowercase_code_still_encodes_canonical_url(lan_client):
    code = lan_client.code
    assert decode(lan_client.get(f"/l/{code.lower()}/qr.png").content) == f"{PUBLIC}/l/{code}"


@pytest.mark.parametrize("scale", [1, 4, 40])
def test_png_scale(lan_client, scale):
    response = lan_client.get(f"/l/{lan_client.code}/qr.png", params={"scale": scale})
    assert response.status_code == 200
    width, _ = Image.open(io.BytesIO(response.content)).size
    assert width % scale == 0
    if scale >= 4:
        assert decode(response.content) == f"{PUBLIC}/l/{lan_client.code}"


@pytest.mark.parametrize("scale", [0, 41, 1000, "x"])
def test_png_scale_bounds(lan_client, scale):
    assert (
        lan_client.get(f"/l/{lan_client.code}/qr.png", params={"scale": scale}).status_code == 422
    )


def test_svg(lan_client):
    response = lan_client.get(f"/l/{lan_client.code}/qr.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in response.content


def test_label_page_shows_url_text_and_qr(lan_client):
    code = lan_client.code
    label = lan_client.get(f"/l/{code}/label")
    assert label.status_code == 200
    assert f'<p class="label-url">{PUBLIC}/l/{code}</p>' in label.text
    assert "<svg" in label.text and "Garage" in label.text
    assert "nas.local" not in label.text


def test_qr_error_level_m():
    assert qr._make("https://x.example/l/ABC123").error == "M"
    with pytest.raises(ValueError):
        qr.png("https://x.example/l/ABC123", scale=0)
