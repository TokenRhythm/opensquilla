# Windows Profile Supervisor

These scripts are a Windows-only helper for running every explicit OpenSquilla
profile under one profiles root.

They are intended to be used after profile support is available:

```powershell
opensquilla --profile coder init
opensquilla --profile planner init
.\scripts\supervisor\start-all.ps1
```

By default the profiles root is:

```text
%USERPROFILE%\.opensquilla\profiles
```

You can override it with `-ProfilesRoot` or `OPENSQUILLA_HOME`.

Common commands:

```powershell
.\scripts\supervisor\start-all.ps1 -ProfilesRoot D:\OpenSquilla\profiles
.\scripts\supervisor\status.ps1 -ProfilesRoot D:\OpenSquilla\profiles
.\scripts\supervisor\stop-all.ps1 -ProfilesRoot D:\OpenSquilla\profiles
.\scripts\supervisor\install-autostart.ps1 -ProfilesRoot D:\OpenSquilla\profiles
.\scripts\supervisor\uninstall-autostart.ps1
```

`install-autostart.ps1` registers a per-user Task Scheduler entry that runs at
interactive logon. It does not require administrator privileges.
