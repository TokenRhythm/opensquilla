from __future__ import annotations

import pytest

from opensquilla.gateway.visualize_html import validate_visualize_html
from opensquilla.tools.types import RetryableToolInputError


@pytest.mark.parametrize("html", [
    '<style>.chart{color:red}</style><svg viewBox="0 0 10 10"><circle r="2"/></svg>',
    '<!doctype html><html><head><style>body{margin:0}</style></head>'
    '<body><canvas></canvas><script>const count = 3;</script></body></html>',
    '<p>Optional end tag<ul><li>one<li>two</ul>',
    '<script type="application/json">{"arbitrary":true}</script>',
    '<pre>```html</pre><button>Click</button>',
    '<!-- <script> is an example --><div>Visible</div>',
    '<svg><script/><circle r="2"/></svg>',
])
def test_html_preflight_accepts_fragments_documents_and_free_content(html):
    validate_visualize_html(html.encode("utf-8"))


@pytest.mark.parametrize("html,diagnostic", [
    ('<div>Hi</div>\n<script>const x = 1', 'line 2, column 1'),
    ('<style>div{color:red}', 'missing </style>'),
    ('\ufeff \n\t', 'nonempty HTML'),
    ('```html\n<div>Hi</div>\n```', 'Markdown fences'),
])
def test_html_preflight_returns_actionable_correctable_errors(html, diagnostic):
    with pytest.raises(RetryableToolInputError, match=diagnostic):
        validate_visualize_html(html.encode("utf-8"))
