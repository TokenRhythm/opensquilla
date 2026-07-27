<!-- このファイルは README.md @ 8794ffbe から翻訳されています。正典は英語版 README です。 -->
<!-- 古くなっていないか確認するには: git log 8794ffbe..HEAD -- README.md -->

# OpenSquilla — Token を効率的に使う AI Agent

<p align="center">
  <img src="assets/opensquilla-long-logo.png" alt="OpenSquilla logo" width="500">
</p>

<p align="center">
  <b>同じ予算で、Agent にもっと多くを、もっと上手にこなさせる。</b><br>
  マイクロカーネル AI Agent — スマートルーティング、永続メモリ、安全なサンドボックス、組み込み検索とローカル埋め込み。
</p>

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/opensquilla/opensquilla/ci.yml?style=for-the-badge" alt="CI"></a>
  <a href="https://opensquilla.ai/"><img src="https://img.shields.io/badge/website-opensquilla.ai-blue?style=for-the-badge" alt="Website"></a>
  <a href="https://github.com/opensquilla/opensquilla/releases"><img src="https://img.shields.io/github/v/release/opensquilla/opensquilla?include_prereleases&style=for-the-badge" alt="GitHub release"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12%2B-blue?style=for-the-badge" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue?style=for-the-badge" alt="Apache 2.0 License"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="README.zh-Hans.md">中文</a> · <b>日本語</b> · <a href="README.fr.md">Français</a> · <a href="README.de.md">Deutsch</a> · <a href="README.es.md">Español</a>
</p>

> このドキュメントは英語版 [`README.md`](README.md) から翻訳されています。内容に食い違いがある場合は英語版が正典です。

---

## お知らせ

- 📢 **2026-07-03** — 技術レポート **[Agentic Routing: The Harness-Native Data Flywheel](docs/releases/agentic_routing_v0.pdf)**（プレビュー版）を、OpenSquilla **0.5.0 Preview 1** と同時に公開しました。harness ネイティブなルーターが日々の Agent トラフィックを自己改善型のデータフライホイールへと変える仕組みを詳しく解説しています。

---

## 概要

OpenSquilla は、Token を効率的に使うマイクロカーネル AI Agent です。ローカルのモデルルーターが各ターンを、それを処理できる最も安価なモデルに振り分けます。さらに永続メモリ、階層化されたサンドボックス、組み込みのウェブ検索、デバイス上で動く埋め込みが、ひとつの共有されたターンループを支えています。

すべての入口——Web UI、CLI、チャットチャネル——が同じループ上で動くため、ツールのディスパッチ、リトライ、判断ログの挙動はどこでも同一です。プラグイン可能なプロバイダ層は TokenRhythm、OpenRouter、OpenAI、Anthropic、Ollama、DeepSeek、Gemini、Qwen/DashScope をはじめとする 20 以上の LLM プロバイダと、あなたのコードや設定スキーマを変えることなくやり取りします。

OpenSquilla 0.5.0 が現在の安定版リリースです。

タスク指向の製品ドキュメントについては、[OpenSquilla 製品ガイド](README.product.md)または[ドキュメント索引](docs/README.md)から始めてください。

---

## インストール

OpenSquilla は Windows、macOS、Linux で動作します。ご自身のユースケースに合った方法を選んでください。

デスクトップインストーラーとターミナルからのクイックインストールは、ビルド済みの**リリース**版をそのまま入手できます——Git は不要です。残りの 2 つ——ソースからのインストールとソースからの開発——は、Vue コントロールコンソールを含めて **Git のチェックアウトから**ビルドします（`git clone` + Git LFS）。リリース wheel とデスクトップインストーラーにはコンソールが含まれるため、利用者に Node.js や npm は不要です。

リリース版のインストールコマンドは、公開された GitHub リリースのアセットを使います。Python wheel のインストールでは、バージョン付きの wheel ファイル名を使います。インストーラーが wheel ファイル名に埋め込まれたバージョンを検証するためです。

0.5.0 をデスクトップで使う場合は、GitHub リリースからパッケージ版デスクトップインストーラーを使うことをおすすめします。macOS では `OpenSquilla-0.5.0-mac-arm64.dmg`、Windows では `OpenSquilla-0.5.0-win-x64.exe` です。

| 方法 | 対象 | 使うべき場面 |
| --- | --- | --- |
| [デスクトップインストーラー](#desktop-installers)**（デスクトップ推奨）** | macOS および Windows ユーザー | パッケージ版デスクトップアプリ |
| [ターミナルからのクイックインストール](#quick-terminal-install)**（推奨）** | あらゆる OS のエンドユーザー | ターミナルからリリース版 wheel をインストール |
| [ソースからのインストール](#install-from-source) | `main` を追跡するユーザー | チェックアウトを編集せずに実行する |
| [ソースからの開発](#develop-from-source) | コントリビューター | ソースを編集、テスト、デバッグする |

### 前提条件

| 要件 | クイックインストール | ソースからのインストール | ソースからの開発 |
| --- | :---: | :---: | :---: |
| Python 3.12+ | `uv` 経由 | `uv` またはシステム経由 | `uv` 経由 |
| Git + Git LFS | — | 必須 | 必須 |
| Node.js 22.12+ + npm | — | Web UI のビルドに必須 | Web UI と wheel のビルドに必須 |
| `uv` | なければ自動インストール | 推奨 | 必須 |

デフォルトの `recommended` プロファイルは **SquillaRouter**——OpenSquilla のデバイス上モデルルーター——とそのモデルアセットをインストールします。`OPENSQUILLA_INSTALL_PROFILE=core` ではこれらの依存関係を省きます。これとは別の `--router disabled` というオンボーディングフラグは、依存関係はインストールしたまま、実行時にルーターをオフにします。

Windows では、SquillaRouter に同梱された ONNX ランタイムが Visual C++ ランタイムも必要とします。ソースからの PowerShell インストーラーは、`winget` 経由でこれを自動的にインストールします。一方、**ターミナルからのクイックインストール**（`uv tool install`）の経路ではインストールしません——起動時に `DLL load failed` エラーが記録された場合は、手動でインストールしてください（[トラブルシューティング](#troubleshooting)を参照）。インストールされるまで、OpenSquilla は単一モデルへの直接ルーティングで動作を続けます。

macOS のターミナルインストールでは、SquillaRouter の LightGBM ランタイムがシステムの OpenMP ライブラリも必要とすることがあります。デスクトップアプリは必要なランタイムを同梱していますが、**ターミナルからのクイックインストール**は Homebrew やシステムライブラリをインストールしません。起動時に `Library not loaded: @rpath/libomp.dylib` が記録された場合は、`brew install libomp` を実行してからゲートウェイを再起動してください。インストールされるまで、OpenSquilla は単一モデルへの直接ルーティングで動作を続けます。

インストールリンク: [Git](https://git-scm.com/downloads) ·
[Git LFS](https://git-lfs.com/) ·
[Node.js](https://nodejs.org/en/download) ·
[uv](https://docs.astral.sh/uv/getting-started/installation/)。

<a id="desktop-installers"></a>

### デスクトップインストーラー

0.5.0 のデスクトップインストーラーは、Vue 製コントロールコンソールとゲートウェイランタイムを Electron シェルにまとめています。

- macOS Apple Silicon: <https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-mac-arm64.dmg>
- Windows x64: <https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/OpenSquilla-0.5.0-win-x64.exe>

中国本土からより高速にダウンロードするには、OSS の直接ダウンロード用エイリアスを使用してください。
- macOS Apple Silicon: <https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-mac-arm64.dmg>
- Windows x64: <https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-win-x64.exe>

これらの固定リンクは、より新しい対象リリースがミラー検証に合格した場合にのみ更新されます。特定のリリースが必要な場合は、上記のバージョン付き GitHub Release リンクを使用してください。

アップグレードの前に、実行中の OpenSquilla デスクトップアプリをすべて終了してください。プラットフォームのアプリデータディレクトリにある既存の Desktop プロファイルが引き続き使用されます。ターミナル版の `~/.opensquilla` は別のプロファイルです。必要な場合は設定から明示的に転送してください。

Windows Desktop を RC3 から RC4 以降へ更新するときは、新しいインストーラーを既存のインストールへ直接上書き実行してください。RC3 を先にアンインストールしないでください。RC3 のアンインストーラーはデスクトップのユーザーデータを削除する可能性があります。更新前に `%APPDATA%\OpenSquilla` をバックアップしてください。RC4 以降のインストーラーは通常のアンインストールでプロファイルデータを保持します。

<a id="quick-terminal-install"></a>

### ターミナルからのクイックインストール

Windows、macOS、Linux での推奨経路です。`uv` は OpenSquilla を独立した隔離環境にインストールし、専用の Python を自前で管理します——システムの Python は不要です。この経路は公開済みのリリースのみをインストールします。`main`、開発ブランチ、ローカルのチェックアウトが必要な場合は[ソースからのインストール](#install-from-source)を使ってください。

**1. `uv` をインストールする**——`uv --version` がすでに動くならスキップしてください。

Linux / macOS:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
. "$HOME/.local/bin/env"
```

Windows PowerShell:

```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;" + $env:Path
```

**2. OpenSquilla をインストールする**——どのプラットフォームでも同じコマンドです。

```sh
uv tool install --python 3.12 "opensquilla[recommended] @ https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl"
```

これはリリース URL から OpenSquilla wheel をインストールし、続いて `uv` が、選択した extra が宣言する依存関係をダウンロードします。デフォルトの `recommended` extra には、ONNX Runtime、LightGBM、NumPy、tokenizers といった SquillaRouter のランタイム依存関係が含まれるため、これらの wheel がすでにキャッシュされていない限り、初回インストールにはネットワークアクセスが必要です。`uv` は macOS の `libomp` や Windows の Visual C++ Redistributable のようなシステムネイティブのランタイムはインストールしません。ルーターランタイムがネイティブライブラリの読み込みエラーを報告した場合は、[トラブルシューティング](#troubleshooting)を参照してください。

**3. 設定して実行する。**

```sh
opensquilla onboard
opensquilla gateway run
```

> [!NOTE]
> 新規の `uv` インストール直後に `opensquilla` が見つからない場合は、新しいターミナルを開くか、ステップ 1 の PATH 設定の行を再実行してください。

完全にバージョンを固定したインストールには、バージョン付きの wheel URL を使ってください:
`https://github.com/opensquilla/opensquilla/releases/download/v0.5.0/opensquilla-0.5.0-py3-none-any.whl`。

<a id="install-from-source"></a>

### ソースからのインストール

この経路は、OpenSquilla をチェックアウトから編集せずに実行する場合に使います。このクローンはインストーラーにとってのパッケージソースにすぎません。インストール後は `opensquilla` コマンドを使ってください——`uv run` は実行しないでください。コードを変更するつもりなら、代わりに[ソースからの開発](#develop-from-source)を選んでください。

1. **LFS アセット込みでクローンする**

   ```sh
   git lfs install
   git clone https://github.com/opensquilla/opensquilla.git
   cd opensquilla
   git lfs pull --include="src/opensquilla/squilla_router/models/**"
   ```

2. **インストーラーを実行する**

   **macOS / Linux**

   ```sh
   bash scripts/install_source.sh
   ```

   **Windows PowerShell**

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1
   ```

   このスクリプトは最初に `opensquilla-webui` で `npm ci` と
   `npm run build` を実行し、その後 `uv tool install` で `.[recommended]`（SquillaRouter + メモリ +
   ローカルモデル）を専用のユーザー環境にインストールし、`uv` が使えない場合は
   `python -m pip install --user` にフォールバックします。ソースから再インストールするたびに
   `npm ci` が `node_modules` を作り直し、コンソールを再ビルドします。初回は通常ダウンロード量が
   最大で、以後は npm キャッシュで通信量を減らせますが、ビルド時間とディスク書き込みは残ります。インストール後に
   `opensquilla` が `PATH` に乗っていない場合は、新しいターミナルを開いてください。

   `pip install .`、`uv tool install .`、VCS URL からの直接インストールは低レベルの
   ソースビルドであり、このスクリプトの代わりにはなりません。ローカルのチェックアウトでは
   先に Web UI をビルドする必要があります。VCS URL のチェックアウトには生成物がないため、
   意図的に拒否されます。ソースインストーラーまたは公式リリース wheel を使用してください。

3. **（任意）高度な extra をインストールする。** ほとんどのチャネル——Feishu、
   Telegram、DingTalk、QQ、WeCom、Slack、Discord——は基本インストールで動作します。オプトインの extra は次のとおりです:

   - `matrix` — Matrix チャネル（`matrix-nio` を導入）
   - `matrix-e2e` — エンドツーエンド暗号化付きの Matrix チャネル（libolm が必要）
   - `document-extras` — WeasyPrint による PDF 生成

   ```sh
   OPENSQUILLA_INSTALL_EXTRAS=matrix bash scripts/install_source.sh        # macOS / Linux
   ```

   ```powershell
   powershell -ExecutionPolicy Bypass -File ./scripts/install_source.ps1 -Extras matrix   # Windows
   ```

4. **設定して実行する**——[設定](#configuration)を参照してください。

<details>
<summary>ソースからのインストール——ターミナルでの前提条件とインストーラーのオプション</summary>

**ターミナルから前提条件（Git、Git LFS、Node.js 22.12+ と npm、uv）をインストールする**

Windows PowerShell:

```powershell
winget install --id Git.Git -e
winget install --id GitHub.GitLFS -e
winget install --id OpenJS.NodeJS.LTS -e
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
git lfs install
```

macOS（Homebrew）:

```sh
brew install git git-lfs node uv
git lfs install
```

Debian / Ubuntu:

```sh
sudo apt update && sudo apt install -y git git-lfs curl
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
curl -LsSf https://astral.sh/uv/install.sh | sh
git lfs install
```

Fedora では `sudo dnf install -y git git-lfs`、Arch では
`sudo pacman -S --needed git git-lfs` を使い、ディストリビューションまたは
nodejs.org から Node.js 22.12+ と npm をインストールしてから、上記の `curl` コマンドで `uv` を
インストールしてください。これらのインストーラーによる PATH の変更は、新しいターミナルセッションで反映されます。

**インストーラーの環境変数と PATH の確認**

```sh
OPENSQUILLA_INSTALL_PROFILE=core   bash scripts/install_source.sh   # 最小ランタイム、SquillaRouter なし
OPENSQUILLA_INSTALL_DRY_RUN=1      bash scripts/install_source.sh   # 計画を表示するだけ
```

シェルが実際にどの `opensquilla` を実行するかは、`command -v opensquilla`（macOS/Linux）または `where.exe opensquilla`（Windows）で確認してください。`PATH` に乗っていない場合は `uv tool update-shell` を実行します。ローカルのチェックアウトから再インストールした後は、更新されたパッケージを読み込むためにゲートウェイを再起動してください。

</details>

<a id="develop-from-source"></a>

### ソースからの開発

この経路は、OpenSquilla のソースコードに手を入れているとき——変更を加える、テストを走らせる、このチェックアウトに対して挙動をデバッグする——に使います。通常のインストール経路ではありません。[ソースからのインストール](#install-from-source)とは異なり、この経路には `uv` が必要です。`uv sync` はリポジトリローカルの `.venv` を作成し、`uv run` はこのチェックアウト内のファイルに対してコマンドを実行します。

```sh
cd opensquilla-webui
npm ci
npm run build
cd ..
uv sync --extra recommended --extra dev
uv run opensquilla --help
```

Web UI のソースを変更した後は `npm run build` を再実行してください。通常の wheel
ビルドはコンソール成果物がない、または古い場合に失敗します。バックエンドのみの
editable `uv sync` は引き続き利用できます。

`recommended` extra は開発時にも SquillaRouter を含みます。`dev` extra はテスト、lint、型チェックのツールをインストールします。追加の extra は、実行する環境と同じ環境にインストールしてください:

```sh
uv sync --extra recommended --extra dev --extra matrix
uv run opensquilla channels status matrix --json
```

このモードでは、[設定](#configuration)に出てくるすべての `opensquilla` コマンドに `uv run` を前置してください。ユーザーローカルの `opensquilla` コマンドで開発用チェックアウトをデバッグしてはいけません——そのコマンドは別の Python 環境で動いています。

### アンインストール

`opensquilla uninstall` で OpenSquilla をアンインストールします。デフォルトではデータを残し、プログラム本体だけを削除します:

```sh
opensquilla uninstall --dry-run   # 削除されるものと残されるものをプレビュー
opensquilla uninstall             # プログラムを削除し、データは残す
```

データも削除したい場合は、明示的にオプトインしてください:

```sh
opensquilla uninstall --purge-state    # セッション、ログ、キャッシュ、スケジューラ、メモリ
opensquilla uninstall --purge-config   # config.toml とシークレット（.env）
opensquilla uninstall --purge-all      # すべて（確認の入力を求められます）
```

まず実行中のゲートウェイがドレインされて停止し、削除は OpenSquilla のホーム内にとどまります。Docker やデスクトップでのインストールの場合は、代わりにガイド付きの削除手順が提示されます。完全なリファレンスは [`docs/cli.md`](docs/cli.md#uninstall) を参照してください。

---

## インストールのプライバシー

OpenSquilla は、インストール数、バージョンの採用状況、ランタイムの互換性を推定するために、匿名のインストールテレメトリを使用します。データはゲートウェイの初回起動時に送信され、OpenSquilla の各バージョンにつき 1 回だけ送信されます。また、完了したトップレベルの会話数とトークン使用量を、会話内容を含めず UTC 日付ごとにローカルで集計し、起動時およびその後 1 時間ごとに、未送信の UTC 日別累積スナップショットを同じテレメトリサービスへ送信しようとします。OpenSquilla は、デスクトップ起動時やアプリの継続実行中に 1 日最大 1 回行われる自動更新確認など、受動的な更新確認を行う場合もあります。アップロードには短いタイムアウトが設定されており、起動をブロックすることはありません。

送信される内容:

- スキーマバージョン
- ローカルで生成された安定した `install_id` ダイジェスト
- OpenSquilla のバージョン
- イベントタイプ（`install`、`version_seen`、または `daily_usage`）
- インストール方法（`pip`、`source`、`docker`、`desktop`、または `unknown`）
- オペレーティングシステム、OS のバージョン、CPU アーキテクチャ、Python のメジャー/マイナー
  バージョン
- 初回確認時と送信時のタイムスタンプ
- CI/テスト環境のマーカー（`ci_environment`）
- 日次利用状況イベントの UTC 日付、完了した会話数、および input/output/cache/cache-write トークン使用量の集計

`install_id` は、利用可能な MAC アドレスから——MAC がない場合はローカル IP アドレスから——導出されるローカルの一方向 SHA-256 ダイジェストで、どちらもない場合はランダムに生成して永続化した値でフォールバックします。生の MAC/IP の値はアップロードされません。

送信されない内容: ユーザー名、ホスト名、パス、API キー、プロバイダ設定、チャット/セッション/メモリ/Agent の内容、ファイル名、ファイルの内容。送信元 IP はトランスポート層で HTTP サーバーから見える場合がありますが、ペイロードには含まれません。

ユーザー操作によらないネットワーク可観測性を起動前に無効にするには:

```sh
OPENSQUILLA_PRIVACY_DISABLE_NETWORK_OBSERVABILITY=true
```

または設定ファイルで次のように指定します:

```toml
[privacy]
disable_network_observability = true
```

この統一スイッチは、自動インストールテレメトリ、日次集計利用状況テレメトリ、受動的な更新確認、およびデスクトップ起動時やアプリの継続実行中に行われる自動更新確認に適用されます。統一スイッチまたは互換性維持のためのオプトアウトが有効な間は、ユーザーが明示的に実行する更新確認もこれを回避しません。リリースページを開く、リリース資産をダウンロードする、設定済みのプロバイダ、検索、チャネルを利用するなど、ユーザーが明示的に開始したその他の操作では、ネットワークサービスへ接続する場合があります。

従来のオプトアウト用環境変数も引き続き使用できます:

```sh
OPENSQUILLA_TELEMETRY_DISABLED=true
OPENSQUILLA_UPDATE_CHECK_DISABLED=true
```

高度なデプロイでは、独自のインストールテレメトリエンドポイントを使用できます:

```sh
OPENSQUILLA_TELEMETRY_ENDPOINT=https://example.com/v1/install
```

---

<a id="configuration"></a>

## 設定

### 初回セットアップ

`opensquilla onboard` は、対話式の初回セットアップウィザードです。これはアクティブな設定ファイルを書き込み、`--api-key-env` を渡すとプロバイダのシークレットを環境変数に残します。ルーターはデフォルトで `recommended`（サポートされているプロバイダでは SquillaRouter）です。単一モデルへの直接ルーティングが必要な場合は `--router disabled` を渡してください。

```sh
opensquilla onboard                # 完全な対話式ウィザード
opensquilla onboard --if-needed    # 冪等: スクリプトや再インストールでも安全
opensquilla onboard --minimal      # プロバイダのみ; チャネルと検索はスキップ
opensquilla onboard status         # 書き込まずに各セットアップ項目を確認
```

SSH、CI、または TTY のないあらゆる環境では、非対話形式を使ってください——シークレットは環境に残し、その値ではなく**名前**を渡します:

**Linux / macOS**

```sh
export OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

**Windows PowerShell**

```powershell
$env:OPENROUTER_API_KEY="sk-..."
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY
```

OpenRouter はあくまで一例です——サポートされている任意のプロバイダと、その API キー変数に置き換えてください。

後からウィザード全体をやり直さずに、特定の項目だけを設定し直せます（以下の例は、該当する API キーがすでに環境にあることを前提とします）:

```sh
opensquilla configure provider --provider openai --model gpt-4o --api-key-env OPENAI_API_KEY
opensquilla configure router --router recommended
opensquilla configure search   --search-provider duckduckgo
opensquilla configure search   --search-provider exa --api-key-env EXA_API_KEY
opensquilla configure channels
```

各項目: `provider`、`router`、`channels`、`search`、`image-generation`、`memory-embedding`。Web UI は `/control/setup` で同じカタログとステータスモデルを公開しています。Provider と Router が速い経路で、Channels、Search、Image generation、Memory embedding は Capability Center に置かれ、後から設定できます。チャネルが空のままでも、セットアップの失敗ではなくオプトアウトとして扱われます。

**設定の読み込み順:** `OPENSQUILLA_GATEWAY_CONFIG_PATH` →
`./opensquilla.toml` → `~/.opensquilla/config.toml` → 組み込みのデフォルト。個々のシークレットについては、環境変数の値が常にファイルの値より優先されます。

### OpenClaw や Hermes Agent からの移行

`~/.openclaw` や `~/.hermes` の下にすでに状態がある場合は、まずドライランを実行して移行レポートを確認し、その後で明示的に適用してください:

```sh
opensquilla migrate openclaw --json
opensquilla migrate openclaw --apply

opensquilla migrate hermes --json
opensquilla migrate hermes --apply
```

`opensquilla migrate --source openclaw,hermes --apply` を使うと、両方のデフォルトホームを取り込めます。`--migrate-secrets` は、ドライランのレポートを確認してから初めて追加してください。カスタムパスと競合の処理については [`MIGRATION.md`](MIGRATION.md) を参照してください。

### 実行

```sh
opensquilla gateway run                # フォアグラウンド、127.0.0.1:18791
opensquilla gateway start --json       # バックグラウンド + ヘルスチェック待ち
opensquilla chat                       # 対話式 REPL
opensquilla agent -m "あなたのプロンプト" # 単発実行、自動化向き
```

<http://127.0.0.1:18791/control/> で Web UI を開きます。**Health** ビューには、OpenSquilla が準備できているか、準備できていない項目は何か、そして次の復旧手順が表示されます。CLI からは次を実行します:

```sh
opensquilla doctor
opensquilla doctor --json
opensquilla doctor --config ./opensquilla.toml --json
```

`/health` と `/healthz` は、プロセス確認のための軽量な稼働確認エンドポイントです。`opensquilla doctor` と Web UI の Health ビューは、プロバイダ設定、メモリ、ログ、検索、チャネル、サンドボックスの状態、ルーター、画像生成、復旧ガイダンスについての準備状況を見る場所です。フォアグラウンドのゲートウェイは `Ctrl+C` で停止します。

その他のコマンドグループには `sessions`、`skills`、`memory`、`migrate`、`cron`、`channels`、`providers`、`models`、`cost` があります。詳細は `opensquilla --help` または `opensquilla <グループ> --help` を実行してください。

<details>
<summary>高度な設定——チャネルの検証、パブリックネットワークへのバインド、Docker</summary>

**メッセージングチャネルを接続して検証する**

チャネルの保存は設定の変更であって、実行時の接続性の証明ではありません。チャネルを編集した後はゲートウェイを再起動し、それから実際のチャネルを検証してください:

```sh
opensquilla gateway restart
opensquilla channels status <name> --json
```

ステータスのペイロードが `enabled=true`、`configured=true`、`connected=true` を報告したときに限り、チャネルが接続済みだと見なしてください。Feishu はデフォルトで websocket モード、Telegram はポーリング、Slack は Socket Mode を使えます——これらのモードはいずれもパブリック URL を必要としません。Feishu の webhook モード、Telegram の webhook モード、Slack の webhook モード、そして WeCom は、パブリックでプロバイダから到達可能な URL を必要とします。

**パブリックネットワークへのバインド**

別のマシンから Web UI に到達するには、ゲートウェイをすべてのインターフェースにバインドし、ホストのパブリック IP を使ってください:

```sh
opensquilla gateway run --listen 0.0.0.0 --port 18791
```

パブリックアクセスにはさらに、そのポートへの受信 TCP をホストのファイアウォールやクラウドのセキュリティグループが許可している必要があります。`[auth] mode = "none"` のままゲートウェイを公開してはいけません——`0.0.0.0` にバインドする前にトークン認証を設定してください。

**Docker**

ビルド済みのマルチアーキテクチャイメージ（`amd64`/`arm64`）は、リリースタグごとに `ghcr.io/opensquilla/opensquilla` に公開されます。コンテナ配備の完全なガイド（ホームサーバー/NAS、トークン認証つきの LAN 公開、アップグレード）は [`docs/docker.md`](docs/docker.md) を参照してください:

```sh
OPENSQUILLA_GATEWAY_IMAGE=ghcr.io/opensquilla/opensquilla:latest docker compose up -d
```

`OPENSQUILLA_GATEWAY_IMAGE` を設定しない場合、compose 経路は自分でビルドした `opensquilla:local` イメージを実行します。Git LFS のルーターアセットを取得済みのソースチェックアウトからビルドしてください（クローンと `git lfs pull` については[ソースからのインストール](#install-from-source)を参照）:

```sh
docker build -t opensquilla:local .
```

その後 `./start.sh`（Windows では `start.ps1`）が `docker compose up -d` を実行し、ゲートウェイのログを追尾します。Docker が省くのはホストの Python ツールチェーンであって、ローカルイメージのビルドではありません。

</details>

プロバイダの階層、サンドボックスのチューニング、画像生成、並行処理の設定は `opensquilla.toml.example` にあります。

---

## 0.4.1 の新着情報

OpenSquilla 0.4.1 は、デスクトップと Control UI のラインに向けたメンテナンスリリースです:

- **デスクトップの信頼性** - パッケージ版ゲートウェイのチェックが Coding モード、
  `code-task`、SquillaRouter の起動までカバーするようになり、デスクトップのウィンドウ/成果物の扱いがより安定しました。
- **6 言語のクライアント対応** - Control UI とデスクトップクライアントが、初回描画と設定の画面全体で
  英語、簡体字中国語、日本語、フランス語、ドイツ語、スペイン語に対応します。
- **Coding モードとルーターのパッケージング** - ルーターアセットが欠落しているか、まだ Git LFS の
  ポインタのままである場合、デスクトップビルドは早期に失敗し、機能が損なわれたリリースパッケージの生成を防ぎます。
- **テレメトリと Windows の磨き込み** - インストールテレメトリは CI とテスト環境をスキップし、Windows のデスクトップアセットは OpenSquilla のロゴを使います。
- **メインライン運営** - 通常のプルリクエストとリリース統合は `main` を中心にそろえられ、メンテナーブランチはリリース、ホットフィックス、ステージング、統合、サンドボックスの作業用に予約されています。

完全なノート: [`CHANGELOG.md`](CHANGELOG.md) ·
[`docs/releases/0.4.1.md`](docs/releases/0.4.1.md)。

## 0.2.1 の新着情報

OpenSquilla 0.2.1 は、リリースパッケージの起動と、長時間稼働する Agent の信頼性に焦点を当てたメンテナンスリリースです:

- **Windows ポータブル版の起動** —— ポータブル版のランチャーが、同梱された ONNX ルーターに必要な
  Visual C++ ランタイムをよりうまく検出してブートストラップします。
- **長時間稼働する Agent のターン** —— ツールを多用する WebUI セッションが、過大なツール結果、不正な形式のツール呼び出し、成果物の引き渡し、品質の落ちた最終応答から、よりきれいに回復します。
- **よりすっきりした WebUI 出力** —— 生成された成果物のマーカーは通常のチャット再生から除かれ、配信されたファイルは引き続き表示されます。
- **メモリ想起のスコアリング** —— ローカルおよび OpenAI 互換の埋め込みベクトルは、セマンティック検索の前に正規化されます。ベクトルのスコアが低いときでも、強いキーワードの一致は依然として有効です。

完全なノート: [`CHANGELOG.md`](CHANGELOG.md) ·
[リリースノート](https://opensquilla.ai/news/)。

## 0.2.0 の新着情報

このリリースは、移行、CLI チャット、チャネル、スケジューリング、長時間稼働するツール作業の各方面で OpenSquilla を拡張します:

- **既存の Agent ホームからの移行経路** —— `opensquilla migrate` は、既存の OpenClaw/Hermes ホームからのインポートをプレビューして適用します。メモリ、ペルソナファイル、スキル、MCP/チャネル設定、競合処理、移行レポートを含みます。
- **実用的なチャット CLI** —— `opensquilla chat` は、安定したターミナル UI、ストリーミング出力、入力のキューイング、スラッシュモードの発見、ツール/ステータスのストリップ、そしてより決定的なライブプロンプトの挙動を備えています。
- **画面横断的な cron 自動化** —— cron ジョブは、構造化されたスケジュール、タイムゾーンを考慮した exact/every/cron の実行、チャネルや webhook への配信、失敗時の送り先、手動実行、そして WebUI/CLI/RPC の同等性をカバーするようになりました。
- **より良い Feishu と Discord のチャネル** —— チャネルアダプターは、より明確な機能メタデータ、より安全な DM/グループの扱い、ネイティブのファイルと成果物の経路、改善された添付/スレッドの挙動を公開する一方、特権操作はスコープが限定されたままです。
- **より頑丈な長時間稼働ターン** —— 失敗したターンはプロバイダの再生から除かれ、不正な形式のツール呼び出しはより安全に扱われ、承認ゲート付きのリトライはオペレーターの判断を待ちます。
- **より賢いコンテキストとツールの予算管理** —— プロバイダ予算に基づくコンパクション、プロンプトキャッシュの保持、ツール結果の上限設定、副作用を考慮した並行処理によって、大規模でツールを多用するセッションの予測可能性が高まります。
- **Web UI とリリースの磨き込み** —— 0.2.0 では、新しさ順の並べ替え、テーブルレイアウト、モバイルのコントロール、重複通知、セットアップフォーム、リリース URL、インストール経路を引き締めました。

完全なノート: [`CHANGELOG.md`](CHANGELOG.md) ·
[リリースノート](https://opensquilla.ai/news/)。

---

## 主な機能

| 機能 | 内容 |
| --- | --- |
| **Token を効率化するルーティング** | `SquillaRouter`——`recommended` extra に含まれるローカルの LightGBM + ONNX 分類器——が、各ターンを長さ、言語、コード、キーワード、セマンティック埋め込みで採点し、4 つの階層（C0〜C3。旧来の T0〜T3 という名前はエイリアス）にわたって、対応できる最も安価なモデルへ振り分けます。分類はデバイス上で実行されます。その判断を下すために、あなたのプロンプトがマシンを離れることはありません。 |
| **適応的な推論とプロンプト** | OpenSquilla は、ルーターが複雑だと採点したターンに対してのみ拡張推論を要求し、システムプロンプトもタスクの複雑さに応じて伸縮します——ささいなターンには軽量に、複雑なターンには完全な指示を。 |
| **20 以上の LLM プロバイダ** | プロバイダレジストリは 20 以上の LLM バックエンドを対象とします——TokenRhythm、OpenRouter、OpenAI、Anthropic、Ollama、DeepSeek、Gemini、DashScope/Qwen、Moonshot、Mistral、Groq、Zhipu、SiliconFlow、vLLM、LM Studio など。プライマリ + フォールバックの選択にも対応し、初回オンボーディングでは検証済みのサブセットが提示されます。 |
| **オンデマンドなスキルと MCP** | 15 個の同梱スキル（coding、GitHub、cron、pptx/docx/xlsx/pdf、要約、tmux、天気など）は、タスクが必要とするときだけ読み込まれます。OpenSquilla は MCP クライアントであり、MCP サーバーとしても動作できます——`opensquilla mcp-server run` には `mcp` extra が必要です（`opensquilla[recommended,mcp]` をインストール）。スキルは CLI から作成、インストール、公開できます。 |
| **永続的なローカルメモリ** | 厳選された `MEMORY.md` に、日付付きの Markdown ノートを加えたもので、SQLite の全文キーワード検索と `sqlite-vec` のセマンティック想起で検索します。埋め込みは同梱の ONNX でデバイス上で実行されますが、OpenAI/Ollama に切り替えることもできます。任意の指数減衰と、オプトインの「dream」整合（コンソリデーション）も利用できます。 |
| **階層化されたセキュリティサンドボックス** | 権限マトリクス上の 3 つのポリシー階層（Standard / Strict / Locked）。Linux では Bubblewrap がコード実行を隔離します。macOS の Seatbelt バックエンドは現在プロファイルを生成するだけで（実行は未対応）、Windows にはまだサンドボックスバックエンドがありません。拒否台帳（denial ledger）は、拒否が繰り返されると自律実行を自動的に一時停止し、拒否された出力は破棄されます。スキルのメタデータとツール結果は、プロンプトインジェクション対策として XML エスケープされます。 |
| **組み込みツール** | ファイルの読み取り/書き込み/編集、シェルとバックグラウンドプロセス、git、SSRF ガードの背後にあるウェブ検索（DuckDuckGo、Bocha、Brave、Tavily、Exa）と取得、スプレッドシート/PPTX/PDF の作成、画像生成、テキスト読み上げ。 |
| **統一ゲートウェイ** | `127.0.0.1:18791` で動作する Starlette ASGI サーバーで、WebSocket RPC と組み込みのコントロールコンソール（`/control/`）を備えます。Web UI、CLI、そして Terminal、WebSocket、Slack、Telegram、Discord、Feishu、DingTalk、WeCom、Matrix、QQ のチャネルが、すべて 1 つの `TurnRunner` を共有します。 |
| **耐久性のあるセッション、サブエージェント、スケジューリング** | SQLite を基盤としたセッション、トランスクリプト、再生のストレージに、Agent ごとのワークスペースを備えます。Agent は深さの制限されたサブエージェントを生成でき、ツリー内蔵の cron パーサーを持つ `SchedulerEngine` が `opensquilla cron` 経由で定期ジョブを実行します。 |
| **オペレーター制御** | ヒューマンインザループの承認により、機微なツール呼び出しを判断のために一時停止できます。ターンごと・セッションごとの Token とコストの集計（`opensquilla cost`）と診断は、CLI と Web UI から利用できます。 |

MetaSkill ドキュメント: [`docs/features/meta-skills.md`](docs/features/meta-skills.md)、
[`docs/features/meta-skill-user-guide.md`](docs/features/meta-skill-user-guide.md)、
[`docs/authoring/meta-skills.md`](docs/authoring/meta-skills.md)。

---

## ベンチマーク結果

PinchBench 1.2.1 の 25 タスクにわたる平均結果:

| Agent | ベースモデル | 平均スコア | 総入力 token | 総出力 token | 総コスト |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenSquilla | モデルルーター（Opus4.7、GLM5.1、DS4 Flash） | 0.9251 | 1,721,328 | 61,475 | $0.688 |
| OpenClaw | Claude Opus 4.7 | 0.9255 | 3,066,243 | 50,890 | $6.233 |

スコアは 25 タスクの平均で、token 数とコストは実行全体の合計です。

---

<a id="troubleshooting"></a>

## トラブルシューティング

<details>
<summary>macOS: <code>Library not loaded: @rpath/libomp.dylib</code></summary>

起動時に `lightgbm/lib/lib_lightgbm.dylib` から `Library not loaded: @rpath/libomp.dylib` が記録された場合、OpenSquilla は単一モデルへの直接ルーティングで動作を続けますが、同梱の `SquillaRouter` ランタイムは、macOS の OpenMP ランタイムがインストールされるまで非アクティブのままです。

デスクトップアプリは、必要なネイティブランタイムを同梱しています。ターミナルからのクイックインストール、またはシェルからのソースインストールを使った場合は、Homebrew で `libomp` をインストールしてからゲートウェイを再起動してください:

```sh
brew install libomp
opensquilla gateway restart
```

</details>

<details>
<summary>Windows: <code>DLL load failed</code> / Visual C++ ランタイム</summary>

起動時に `DLL load failed while importing onnxruntime_pybind11_state` が記録された場合、OpenSquilla は単一モデルへの直接ルーティングで動作を続けますが、同梱の `SquillaRouter` ランタイムは、Visual Studio 2015〜2022（x64）向けの Visual C++ Redistributable がインストールされるまで非アクティブのままです。

ソースからの PowerShell インストーラーは、`winget` 経由でこの redistributable のインストールを試みます。ターミナルからのクイックインストールを使った場合、または `winget` が利用できない場合は、手動でインストールしてから PowerShell を再起動してください: <https://aka.ms/vs/17/release/vc_redist.x64.exe>。その後、推奨のルーターを復元します:

```powershell
opensquilla onboard --provider openrouter --api-key-env OPENROUTER_API_KEY --router recommended
opensquilla gateway restart
```

</details>

---

## クレジット

OpenSquilla は [OpenClaw](https://github.com/openclaw/openclaw) に着想を得ています。同梱の第三者コンテンツの出典は [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) に明記されています。

コミュニティのコントリビューターは [`CONTRIBUTORS.md`](CONTRIBUTORS.md) に記載されており、squash マージや再生された作業についての、リリースごとの帰属の注記も含まれています。

---

## コントリビューター

OpenSquilla に貢献してくださったすべての方に感謝します。

<p align="center">
  <a href="https://github.com/opensquilla/opensquilla/graphs/contributors">
    <img src="https://contrib.rocks/image?repo=opensquilla/opensquilla&max=100&columns=10" alt="OpenSquilla contributors" />
  </a>
</p>

---

## 貢献する

あらゆる種類の貢献を歓迎します——バグ報告、機能のアイデア、ドキュメント、新しいプロバイダやチャネルのアダプター、スキル、そしてコアランタイムの開発です。[`CONTRIBUTING.md`](CONTRIBUTING.md) を読んだうえで、[GitHub](https://github.com/opensquilla/opensquilla) で issue やプルリクエストを開いてください。

[行動規範](CODE_OF_CONDUCT.md) · [セキュリティ](SECURITY.md) ·
[サポート](SUPPORT.md) · [ライセンス](LICENSE)（Apache-2.0）
