# Third-party notices for `html-to-pdf` skill

Source: ClawHub `generate-pdf`
(<https://clawhub.ai/generate-pdf>, MIT-0).

## Runtime dependency

This skill requires `weasyprint` (BSD-3-Clause license,
<https://weasyprint.org/>). Because WeasyPrint pulls in native libraries
(Pango, Cairo, GDK-PixBuf, fontconfig), it ships in OpenSquilla's
`[document-extras]` optional-dependencies group rather than the default
install.

## License

The ClawHub source is MIT-0. The OpenSquilla project license is Apache-2.0.
The WeasyPrint runtime carries its own BSD-3-Clause license.
