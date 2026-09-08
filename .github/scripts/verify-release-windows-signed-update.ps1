[CmdletBinding()]
param(
  [string]$InstallRoot,
  [string]$UserDataDir,
  [string]$EvidenceRoot,
  [string]$BaselineVersion,
  [string]$BaselineExecutableSha256,
  [string]$BaselineSourceSha,
  [string]$CandidateInstaller,
  [string]$CandidateInstallerSha256,
  [string]$CandidateSourceSha,
  [string]$ChannelManifest,
  [ValidateRange(30, 1800)][int]$InstallTimeoutSeconds = 600
)

$ErrorActionPreference = 'Stop'

function Test-SignedAuditVersion([string]$Actual, [string]$Expected) {
  return $Actual -ceq $Expected -or $Actual -ceq "$Expected.0"
}

function Get-SignedAuditPlan {
  param(
    [string]$InstallRoot, [string]$UserDataDir, [string]$NativeUserDataDir,
    [string]$EvidenceRoot, [string]$TemporaryRoot, [string]$BaselineVersion,
    [string]$BaselineExecutableSha256, [string]$BaselineSourceSha,
    [string]$CandidateInstaller, [string]$CandidateInstallerSha256,
    [string]$CandidateSourceSha, [string]$ChannelManifest
  )
  foreach ($value in @($InstallRoot, $UserDataDir, $NativeUserDataDir, $EvidenceRoot,
      $TemporaryRoot, $CandidateInstaller, $ChannelManifest)) {
    if (-not $value -or -not [IO.Path]::IsPathRooted($value)) {
      throw 'Every audit path must be explicit and absolute.'
    }
  }
  foreach ($sha in @($BaselineSourceSha, $CandidateSourceSha)) {
    if ($sha -cnotmatch '^[0-9a-f]{40}$') { throw 'Both build source SHAs must be full lowercase commit hashes.' }
  }
  foreach ($sha in @($BaselineExecutableSha256, $CandidateInstallerSha256)) {
    if ($sha -cnotmatch '^[0-9a-f]{64}$') { throw 'Both artifact SHA256 hashes must be pinned before the audit.' }
  }
  if ($BaselineVersion -cnotmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' -or
      $BaselineVersion -in @('0.5.3', '0.5.4')) {
    throw 'Use a stable signed baseline with the new handoff capability; 0.5.3/0.5.4 retain the manual audit.'
  }
  $name = [IO.Path]::GetFileName($CandidateInstaller)
  if ($name -cnotmatch '^OpenSquilla-(?<version>(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))-win-x64\.exe$') {
    throw 'Candidate must have the canonical stable Windows installer filename.'
  }
  $candidateVersion = $Matches['version']
  if ([version]$candidateVersion -le [version]$BaselineVersion) { throw 'Candidate B must be newer than baseline A.' }
  $userData = [IO.Path]::GetFullPath($UserDataDir).TrimEnd([IO.Path]::DirectorySeparatorChar)
  $nativeUserData = [IO.Path]::GetFullPath($NativeUserDataDir).TrimEnd([IO.Path]::DirectorySeparatorChar)
  if (-not $userData.Equals($nativeUserData, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Use the disposable account native AppData/OpenSquilla directory; NSIS does not inherit --user-data-dir.'
  }
  if (Test-Path -LiteralPath $userData) { throw 'The native OpenSquilla profile already exists; use a fresh disposable account.' }
  $evidence = [IO.Path]::GetFullPath($EvidenceRoot)
  $temporary = [IO.Path]::GetFullPath($TemporaryRoot).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
  if (-not $evidence.StartsWith($temporary, [StringComparison]::OrdinalIgnoreCase) -or
      (Test-Path -LiteralPath $evidence)) {
    throw 'EvidenceRoot must be a new directory strictly inside the temporary directory.'
  }
  $install = [IO.Path]::GetFullPath($InstallRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
  foreach ($other in @($userData, $evidence)) {
    if ($other.Equals($install, [StringComparison]::OrdinalIgnoreCase) -or
        $other.StartsWith($install + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $install.StartsWith($other + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
      throw 'Installation, evidence, and synthetic profile directories must be separate.'
    }
  }
  return [pscustomobject]@{
    InstallRoot = $install; Executable = (Join-Path $install 'OpenSquilla.exe')
    UserDataDir = $userData; Profile = (Join-Path $userData 'opensquilla')
    EvidenceRoot = $evidence; CandidateVersion = $candidateVersion
    CandidateInstaller = [IO.Path]::GetFullPath($CandidateInstaller)
    ChannelManifest = [IO.Path]::GetFullPath($ChannelManifest)
  }
}

function Find-SignedRestartCandidate {
  param([object[]]$Starts, [string]$ExecutablePath, [datetime]$NotBefore, [int]$OldPid)
  # NSIS uses ExecShellAsUser, which can launch through the user's shell broker.
  # A new process is an observation; its cause needs separate Finish attestation.
  return @($Starts | Where-Object {
    $_.Path -ieq $ExecutablePath -and $_.StartedAt -ge $NotBefore -and $_.Pid -ne $OldPid -and
    $_.CommandLine -match '(?:^|\s)"?--updated"?(?=\s|$)' -and
    $_.CommandLine -notmatch '(?:^|\s)"?--type(?:=|\s|"|$)'
  } | Sort-Object StartedAt | Select-Object -Last 1) | Select-Object -First 1
}

function New-SignedAuditResult {
  param([string]$BaselineVersion, [string]$CandidateVersion, [string]$BaselineSourceSha,
    [string]$CandidateSourceSha, [string]$BaselineExecutableSha256, [string]$CandidateInstallerSha256)
  return [ordered]@{
    schemaVersion = 1; ok = $false; stage = 'preflight'; releaseGatePassed = $false
    fromVersion = $BaselineVersion; toVersion = $CandidateVersion
    provenance = [ordered]@{
      baselineSourceSha = $BaselineSourceSha; candidateSourceSha = $CandidateSourceSha
      baselineExecutableSha256 = $BaselineExecutableSha256; candidateInstallerSha256 = $CandidateInstallerSha256
      sourceBinding = 'operator-supplied build provenance; artifact hashes verified locally'
    }
    handoffObserved = $false; automaticRestartVerified = $false
    installedVersionVerified = $false; installedSignaturesVerified = $false
    firstSendVerified = $false; firstSendScope = 'new synthetic profile only'; sessionRecoveryVerified = $false
    normalQuitObserved = $false; stopAndRestartVerified = $false; profilePreserved = $false; toolCallVerified = $false
    gaps = @('Shell-brokered restart causality is operator-attested, not machine-proven.',
      'First send and a necessary tool call on the retained upgraded profile are not exercised.',
      'Chat Stop and a necessary tool call on the retained profile still require a separate native audit.',
      'NSIS interruption after the old uninstaller starts has no verified rollback guarantee.',
      'Uninstall preservation is covered separately by the existing installer audit.')
  }
}

function Invoke-SignedWindowsUpdateAudit {
  param(
    [string]$InstallRoot, [string]$UserDataDir, [string]$EvidenceRoot,
    [string]$BaselineVersion, [string]$BaselineExecutableSha256, [string]$BaselineSourceSha,
    [string]$CandidateInstaller, [string]$CandidateInstallerSha256, [string]$CandidateSourceSha,
    [string]$ChannelManifest, [int]$InstallTimeoutSeconds = 600
  )
  if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'This audit requires Windows.' }
  $repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
  $nativeUserData = Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'OpenSquilla'
  $temporary = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
  $planArguments = @{} + $PSBoundParameters
  $null = $planArguments.Remove('InstallTimeoutSeconds')
  $planArguments.NativeUserDataDir = $nativeUserData
  $planArguments.TemporaryRoot = $temporary
  $plan = Get-SignedAuditPlan @planArguments
  foreach ($path in @($plan.Executable, $plan.CandidateInstaller, $plan.ChannelManifest)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required input is missing: $path" }
  }
  $existing = @(Get-CimInstance Win32_Process -Filter "Name='OpenSquilla.exe' OR Name='opensquilla-gateway.exe'" | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($plan.InstallRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
  })
  if ($existing.Count) { throw 'The selected baseline is already running; use a stopped disposable installation.' }
  $actualA = ([Diagnostics.FileVersionInfo]::GetVersionInfo($plan.Executable)).ProductVersion
  if (-not (Test-SignedAuditVersion $actualA $BaselineVersion)) { throw 'Installed A has a different PE version.' }
  if ((Get-FileHash -LiteralPath $plan.Executable -Algorithm SHA256).Hash.ToLowerInvariant() -cne $BaselineExecutableSha256 -or
      (Get-FileHash -LiteralPath $plan.CandidateInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -cne $CandidateInstallerSha256) {
    throw 'A or B does not match its pinned artifact hash.'
  }
  $manifest = Get-Content -LiteralPath $plan.ChannelManifest -Raw | ConvertFrom-Json
  if ($manifest.schemaVersion -ne 1 -or $manifest.version -cne $plan.CandidateVersion -or
      $manifest.tag -cne "v$($plan.CandidateVersion)" -or $manifest.prerelease -ne $false) {
    throw 'The controlled manifest does not identify candidate B.'
  }
  New-Item -ItemType Directory -Path $plan.EvidenceRoot | Out-Null
  $signatureCheck = Join-Path $repo '.github/scripts/verify-windows-signatures.ps1'
  $probe = Join-Path $repo '.github/scripts/verify-release-profile-preservation.py'
  $resultPath = Join-Path $plan.EvidenceRoot 'result.json'
  $result = New-SignedAuditResult $BaselineVersion $plan.CandidateVersion $BaselineSourceSha `
    $CandidateSourceSha $BaselineExecutableSha256 $CandidateInstallerSha256
  $result.provenance.channelManifestSha256 = (Get-FileHash -LiteralPath $plan.ChannelManifest -Algorithm SHA256).Hash.ToLowerInvariant()
  $sourceId = 'OpenSquilla.SignedUpdate.' + [guid]::NewGuid().ToString('N')
  $subscription = $null
  $automaticPid = $null
  $originalOffline = $env:OPENSQUILLA_RECOVERY_OFFLINE
  $originalUpdate = $env:OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE
  try {
    & $signatureCheck -InstallerPath $plan.CandidateInstaller -InstalledRoot $plan.InstallRoot |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'preflight-signatures.txt')
    if ($LASTEXITCODE -ne 0) { throw 'A/B signature verification failed.' }
    # A must inherit the same native profile B will use from the NSIS Finish page.
    # No APPDATA/LOCALAPPDATA redirection or profile reuse is permitted.
    $env:OPENSQUILLA_RECOVERY_OFFLINE = '1'
    $env:OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE = '1'
    # Trace registration can require elevation. Establish it before creating the
    # fresh native profile, so an access denial cannot consume that precondition.
    $queue = [Collections.Concurrent.ConcurrentQueue[object]]::new()
    $observer = @{ Queue = $queue; Executable = $plan.Executable }
    $subscription = Register-CimIndicationEvent -Query 'SELECT * FROM Win32_ProcessStartTrace' `
      -SourceIdentifier $sourceId -MessageData $observer -Action {
        $trace = $Event.SourceEventArgs.NewEvent
        if ($trace.ProcessName -ine 'OpenSquilla.exe') { return }
        $started = [datetime]::FromFileTimeUtc([long]$trace.TIME_CREATED)
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($trace.ProcessID)" -ErrorAction SilentlyContinue
        if ($process -and $process.ExecutablePath -ieq $Event.MessageData.Executable -and
            $process.CreationDate.ToUniversalTime() -le $started -and $Event.MessageData.Queue.Count -lt 1024) {
          $Event.MessageData.Queue.Enqueue([pscustomobject]@{
            Pid = [int]$trace.ProcessID; ParentPid = [int]$trace.ParentProcessID
            StartedAt = $started; CreatedAt = $process.CreationDate
            Path = [string]$process.ExecutablePath; CommandLine = [string]$process.CommandLine
          })
        }
      }
    & python $probe seed --home $plan.Profile --label signed-update-audit --external-root (Join-Path $plan.EvidenceRoot 'external-sentinels') |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'profile-seed.log')
    if ($LASTEXITCODE -ne 0) { throw 'Could not seed the isolated synthetic profile.' }
    $result.stage = 'waiting-for-installer-handoff'
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding utf8
    $handoffPath = Join-Path $plan.EvidenceRoot 'handoff.json'
    & node (Join-Path $repo 'desktop/electron/scripts/test-packaged-real-update-flow.mjs') `
      --mode signed-handoff --executable $plan.Executable --user-data-dir $plan.UserDataDir `
      --baseline-version $BaselineVersion --expected-version $plan.CandidateVersion `
      --channel-manifest $plan.ChannelManifest --expected-sha256 $CandidateInstallerSha256 `
      --source-sha $CandidateSourceSha --ready-output $handoffPath 2>&1 |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'handoff-driver.log')
    if ($LASTEXITCODE -ne 0) { throw 'The packaged client did not complete a verified installer handoff.' }
    $handoff = Get-Content -LiteralPath $handoffPath -Raw | ConvertFrom-Json
    if ($handoff.stage -ne 'installer-handoff' -or $handoff.handoffObserved -ne $true -or
        $handoff.requiresPostInstallVerification -ne $true -or $handoff.ok -ne $false -or
        $handoff.fromVersion -cne $BaselineVersion -or $handoff.toVersion -cne $plan.CandidateVersion -or
        $handoff.sha256 -cne $CandidateInstallerSha256 -or $handoff.sourceSha -cne $CandidateSourceSha) {
      throw 'The handoff result does not match the pinned A-to-B audit.'
    }
    $result.handoffObserved = $true
    $result.stage = 'waiting-for-operator-installer-and-automatic-restart'
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding utf8
    Write-Host 'Complete the NSIS wizard and leave Run OpenSquilla selected. Do not launch B manually.'
    $notBefore = [datetime]::Parse($handoff.handoffStartedAt).ToUniversalTime()
    $starts = [Collections.Generic.List[object]]::new()
    $deadline = [datetime]::UtcNow.AddSeconds($InstallTimeoutSeconds)
    $restart = $null
    while ([datetime]::UtcNow -lt $deadline) {
      $observed = $null
      while ($queue.TryDequeue([ref]$observed)) {
        if ($starts.Count -ge 1024) { throw 'Too many process starts; restart evidence is ambiguous.' }
        $starts.Add($observed)
      }
      if (Test-Path -LiteralPath $plan.Executable -PathType Leaf) {
        $actualB = ([Diagnostics.FileVersionInfo]::GetVersionInfo($plan.Executable)).ProductVersion
        if (Test-SignedAuditVersion $actualB $plan.CandidateVersion) {
          $restart = Find-SignedRestartCandidate -Starts $starts.ToArray() `
            -ExecutablePath $plan.Executable -NotBefore $notBefore -OldPid $handoff.oldPid
          if ($restart) { break }
        }
      }
      Start-Sleep -Milliseconds 250
    }
    if (-not $restart) { throw 'B installation and a new process after handoff were not both observed.' }
    $automaticPid = $restart.Pid
    $result.restartObservation = $restart
    $attestation = Read-Host 'If B started from NSIS Finish with Run OpenSquilla selected and you did not launch it manually, type FINISH-AUTOLAUNCH'
    if ($attestation -cne 'FINISH-AUTOLAUNCH') { throw 'Finish-page restart was not confirmed; a manual launch does not count.' }
    $result.restartAttestation = 'operator confirmed NSIS Finish/Run; shell-broker causality not machine-proven'
    $result.installedVersionVerified = $true
    & $signatureCheck -InstallerPath $plan.CandidateInstaller -InstalledRoot $plan.InstallRoot |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'postinstall-signatures.txt')
    if ($LASTEXITCODE -ne 0) { throw 'B installed executable signature verification failed.' }
    $result.installedSignaturesVerified = $true
    $result.stage = 'postinstall-probes'
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding utf8
    # Capture B and its known descendants before the operator uses ordinary Quit.
    # Never force-kill a process that may still be persisting the retained profile.
    $live = Get-CimInstance Win32_Process -Filter "ProcessId=$automaticPid" -ErrorAction SilentlyContinue
    if (-not $live -or $live.ExecutablePath -ine $plan.Executable -or $live.CreationDate -ne $restart.CreatedAt) {
      throw 'Observed B main process exited or changed identity before postinstall verification.'
    }
    $snapshot = @(Get-CimInstance Win32_Process)
    $owned = [Collections.Generic.List[object]]::new()
    $owned.Add([pscustomobject]@{ Pid = $automaticPid; CreatedAt = $live.CreationDate })
    do {
      $added = $false
      foreach ($process in $snapshot) {
        if ($process.ParentProcessId -in @($owned.Pid) -and $process.ProcessId -notin @($owned.Pid)) {
          $owned.Add([pscustomobject]@{ Pid = $process.ProcessId; CreatedAt = $process.CreationDate })
          $added = $true
        }
      }
    } while ($added)
    $result.quitProcessSnapshot = $owned.ToArray()
    $quit = Read-Host 'Use the running B tray Quit command, then type QUIT (do not end it in Task Manager)'
    if ($quit -cne 'QUIT') { throw 'Normal Quit was not confirmed; processes and profile are retained for diagnosis.' }
    $deadline = [datetime]::UtcNow.AddSeconds([Math]::Min(90, $InstallTimeoutSeconds))
    do {
      $remaining = @($owned | Where-Object {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Pid)" -ErrorAction SilentlyContinue
        $process -and $process.CreationDate -eq $_.CreatedAt
      })
      if (-not $remaining.Count) { break }
      Start-Sleep -Milliseconds 250
    } while ([datetime]::UtcNow -lt $deadline)
    if ($remaining.Count) { throw 'B or its captured child processes did not exit after Quit; no force cleanup was performed.' }
    $automaticPid = $null
    $result.normalQuitObserved = $true
    $result.quitScope = 'operator tray Quit attestation plus exit of B and captured descendants; later children are not proven'
    & node (Join-Path $repo 'desktop/electron/scripts/test-packaged-first-send-renderer.mjs') `
      --executable $plan.Executable --user-data-dir (Join-Path $plan.EvidenceRoot 'first-send-new-profile') --iterations 1 2>&1 |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'first-send.log')
    if ($LASTEXITCODE -ne 0) { throw 'Installed B first-send/owned Gateway probe failed.' }
    $result.firstSendVerified = $true
    & node (Join-Path $repo 'desktop/electron/scripts/test-packaged-session-recovery.mjs') `
      --executable $plan.Executable --user-data-dir $plan.UserDataDir `
      --label signed-update-audit `
      --session-key 'agent:main:webchat:release-recovery-long-session' `
      --switch-session-key 'agent:main:webchat:release-recovery-switch-session' 2>&1 |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'session-recovery.log')
    if ($LASTEXITCODE -ne 0) { throw 'Installed B session recovery/relaunch probe failed.' }
    $result.sessionRecoveryVerified = $true
    & python $probe verify-runtime --home $plan.Profile --label signed-update-audit `
      --external-root (Join-Path $plan.EvidenceRoot 'external-sentinels') |
      Out-File -LiteralPath (Join-Path $plan.EvidenceRoot 'profile-preservation.log')
    if ($LASTEXITCODE -ne 0) { throw 'Postinstall probes changed retained profile data.' }
    $result.profilePreserved = $true
    $result.stage = 'postinstall-verified-with-gaps'
    # Existing first-send uses a synthetic provider and does not call a tool.
    # Keep releaseGatePassed/ok false and return 2 rather than greenwash that gap.
    return 2
  } catch {
    $result.error = $_.Exception.Message
    $result.stage = 'failed'
    Write-Error -ErrorAction Continue $_
    return 1
  } finally {
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $resultPath -Encoding utf8
    if ($subscription) {
      Unregister-Event -SourceIdentifier $sourceId -ErrorAction SilentlyContinue
      Get-Event -SourceIdentifier $sourceId -ErrorAction SilentlyContinue | Remove-Event
      Remove-Job -Job $subscription -Force -ErrorAction SilentlyContinue
    }
    $env:OPENSQUILLA_RECOVERY_OFFLINE = $originalOffline
    $env:OPENSQUILLA_DESKTOP_DISABLE_AUTO_UPDATE = $originalUpdate
    Write-Host "Audit evidence retained at $($plan.EvidenceRoot). Release gate remains closed."
  }
}

if ($MyInvocation.InvocationName -ne '.') {
  exit (Invoke-SignedWindowsUpdateAudit @PSBoundParameters)
}
