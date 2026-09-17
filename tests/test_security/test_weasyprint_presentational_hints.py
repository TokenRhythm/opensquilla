"""Exercise the document extra against GHSA-jhhc-3hcp-qhm5 without networking."""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image


@pytest.mark.parametrize(
    "template",
    [
        '<body background="{background}"><p>Document</p></body>',
        '<table background="{background}"><tr><td>Document</td></tr></table>',
        '<table><tr><td background="{background}">Document</td></tr></table>',
    ],
    ids=["body", "table", "cell"],
)
def test_presentational_background_cannot_inject_a_second_fetch(template: str) -> None:
    # Ordinary base installs do not include the optional document renderer.
    # A document-extras validation job must install it and its native libraries.
    weasyprint = pytest.importorskip("weasyprint")
    from weasyprint.urls import URLFetcher, URLFetcherResponse

    image_buffer = BytesIO()
    Image.new("RGB", (1, 1), "white").save(image_buffer, format="PNG")
    image = image_buffer.getvalue()
    requests: list[str] = []

    class LocalFetcher(URLFetcher):
        def __call__(self, url: str) -> URLFetcherResponse:
            requests.append(url)
            return URLFetcherResponse(url, image, {"Content-Type": "image/png"})

    def render(background: str) -> bytes:
        return weasyprint.HTML(
            string=template.format(background=background),
            base_url="https://assets.invalid/",
            url_fetcher=LocalFetcher(),
        ).write_pdf(presentational_hints=True)

    # First demonstrate that the hint is active and reaches our local fetcher.
    assert render("ordinary.png").startswith(b"%PDF-")
    assert requests == ["https://assets.invalid/ordinary.png"]

    requests.clear()
    payload = "x);background-image:url(https://injected.invalid/secret)"
    assert render(payload).startswith(b"%PDF-")
    # Treat the attribute as one URL (with normalized path separators),
    # retaining CSS punctuation as path data.
    # Merely observing no request to the second host would also pass if the
    # CSS parser discarded the malformed declaration instead of escaping it.
    assert requests == [
        "https://assets.invalid/x);background-image:url(https:/injected.invalid/secret)"
    ]
