# frozen_string_literal: true

require "language/node"

# OpenSquilla — microkernel Python agent runtime. See caveats for the loopback-safe
# gateway default and the documented `--listen 0.0.0.0` opt-in.
class Opensquilla < Formula
  include Language::Python::Virtualenv

  desc "Microkernel Python agent runtime with MCP tools and multi-channel messaging"
  homepage "https://github.com/OpenSquilla/opensquilla"
  url "https://github.com/OpenSquilla/opensquilla/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "Apache-2.0"
  head "https://github.com/OpenSquilla/opensquilla.git", branch: "main"

  depends_on "node" => :build
  depends_on "python@3.13"

  # First-draft formula: pip_install_and_link resolves runtime deps from
  # PyPI at brew-install time. Once OpenSquilla ships a 0.1.0 tag, each runtime
  # dep will be pinned here as a `resource` block for audit-grade install.
  def install
    cd "opensquilla-webui" do
      system "npm", "ci", "--#{Language::Node.npm_cache_config}"
      system "npm", "run", "build"
    end
    venv = virtualenv_create(libexec, "python3.13")
    venv.pip_install_and_link buildpath
  end

  def caveats
    <<~EOS
      OpenSquilla installed.

      Default gateway bind: 127.0.0.1:18790 (loopback only).
      Network exposure is opt-in only. To expose the gateway on the network:

        - CLI flag:  opensquilla gateway run --listen 0.0.0.0
        - Env var:   OPENSQUILLA_LISTEN=0.0.0.0 opensquilla gateway run

      Reminder: only expose 0.0.0.0 behind a trusted reverse proxy or VPN.
      The gateway's first-class auth assumes loopback-scope by default.

      The Homebrew formula installs the core runtime. The bundled local ML
      router also requires hydrated Git LFS model assets, which GitHub source
      tarballs do not carry. If you want squilla-router ML routing, use a
      source checkout with Git LFS plus the `recommended` profile:

        git lfs pull --include="src/opensquilla/squilla_router/models/**"
        uv sync --extra recommended

      Service units (launchd / systemd / Task Scheduler) ship in
      service-units/. For macOS, install the LaunchAgent:

        envsubst < service-units/launchd/ai.opensquilla.gateway.plist \\
          > ~/Library/LaunchAgents/ai.opensquilla.gateway.plist
        launchctl load ~/Library/LaunchAgents/ai.opensquilla.gateway.plist

      See service-units/README.md for the per-platform install + opt-in
      walkthrough.
    EOS
  end

  test do
    assert_match "opensquilla", shell_output("#{bin}/opensquilla --help 2>&1")
  end
end
