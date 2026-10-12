"""Package-manager domain bundles for sandbox managed network."""

from __future__ import annotations

PACKAGE_BUNDLES: dict[str, tuple[str, ...]] = {
    "python-package-install": (
        "pypi.org",
        "files.pythonhosted.org",
        "pypi.python.org",
        "bootstrap.pypa.io",
        "python-poetry.org",
        "install.python-poetry.org",
    ),
    "node-package-install": (
        "registry.npmjs.org",
        "registry.yarnpkg.com",
        "yarnpkg.com",
        "nodejs.org",
        "unpkg.com",
        "cdn.jsdelivr.net",
    ),
    "rust-package-install": (
        "crates.io",
        "static.crates.io",
        "index.crates.io",
        "github.com",
        "objects.githubusercontent.com",
    ),
    "go-package-install": (
        "proxy.golang.org",
        "sum.golang.org",
        "go.dev",
        "golang.org",
        "storage.googleapis.com",
    ),
    "java-package-install": (
        "repo.maven.apache.org",
        "repo1.maven.org",
        "plugins.gradle.org",
        "services.gradle.org",
    ),
    "php-package-install": (
        "packagist.org",
        "repo.packagist.org",
        "getcomposer.org",
    ),
    "github-default": (
        "github.com",
        "api.github.com",
        "raw.githubusercontent.com",
        "objects.githubusercontent.com",
        "codeload.github.com",
        "github.githubassets.com",
        "avatars.githubusercontent.com",
        "actions.githubusercontent.com",
        "pipelines.actions.githubusercontent.com",
        "results-receiver.actions.githubusercontent.com",
        "uploads.github.com",
        "release-assets.githubusercontent.com",
        "ghcr.io",
        "pkg-containers.githubusercontent.com",
    ),
}

DEFAULT_PACKAGE_BUNDLE_IDS: tuple[str, ...] = tuple(PACKAGE_BUNDLES)


def expand_package_bundle(bundle_id: str) -> tuple[str, ...]:
    return PACKAGE_BUNDLES.get(str(bundle_id or ""), ())


__all__ = ["DEFAULT_PACKAGE_BUNDLE_IDS", "PACKAGE_BUNDLES", "expand_package_bundle"]
