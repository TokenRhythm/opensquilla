# OpenSquilla

OpenSquilla is a Python agent runtime with MCP-native tools, durable sessions,
local memory, multi-channel messaging, and a local web control UI.

Public releases provide Desktop installers and a versioned Python wheel.
Use the Desktop installer for the bundled app, or install the wheel for CLI and
Gateway use. Both include the built Web UI and do not require Node.js or npm;
those tools are needed only when building from a Git checkout.

## Requirements

- Python 3.12 or newer.
- A configured model provider for live model calls.
- Optional channel credentials only for the channel integrations you enable.

## After Install

Run the onboarding command before starting the gateway:

```sh
opensquilla onboard --if-needed
```

Then start the local gateway:

```sh
opensquilla gateway run
```

## Project Links

Repository, license, release, and third-party notice information are available
in the public OpenSquilla repository and the release artifacts.
