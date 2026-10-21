import zipfile

from opensquilla.artifacts import ArtifactStore, collect_artifact_bundle
from opensquilla.gateway.html_archive import HtmlZipExporter, capture_saved_html


async def test_export_preserves_nested_chinese_paths_and_relative_dependencies(tmp_path):
    site = tmp_path / "site"
    contents = {
        "首页.html": '<link rel="stylesheet" href="css/site.css"><a href="pages/关于.html">关于</a>'
        '<script type="module" src="js/app.js"></script>',
        "pages/关于.html": '<img src="../images/图标.svg">',
        "css/site.css": 'body{background:url("../images/图标.svg")} '
        '@font-face{font-family:example;src:url("../fonts/sample.woff2")}',
        "js/app.js": 'import "./utils.js";',
        "js/utils.js": 'export const value = 1;',
        "images/图标.svg": '<svg xmlns="http://www.w3.org/2000/svg"/>',
        "fonts/sample.woff2": "font-fixture",
    }
    expected = {name: value.encode("utf-8") for name, value in contents.items()}
    for name, data in expected.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (site / "unrelated.txt").write_text("must not export", encoding="utf-8")
    bundle = collect_artifact_bundle(site / "首页.html", workspace_root=tmp_path)
    assert bundle is not None
    store = ArtifactStore(tmp_path / "media")
    ref = store.publish_bundle(
        bundle, session_id="s", session_key="k", name="首页.html", mime="text/html",
        source="test",
    )
    # An immutable archive remains v1 even if its original workspace changes.
    (site / "首页.html").write_text("new live version", encoding="utf-8")

    async def valid():
        pass

    material = await HtmlZipExporter().export(
        lambda: capture_saved_html(store, ref.id, "s"), validate=valid,
    )
    assert material.path is not None and material.cleanup is not None
    try:
        with zipfile.ZipFile(material.path) as archive:
            assert {name: archive.read(name) for name in archive.namelist()} == expected
            assert archive.testzip() is None
        assert material.filename == "首页.zip"
    finally:
        await material.cleanup()
    assert not material.path.parent.exists()
