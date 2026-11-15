# iPhone Use

[中文](README.md) · **English**

![iPhone Use controlling a real iPhone in Codex with a live screen preview](assets/iphone-use-demo.png)

Let Codex operate your real iPhone over USB. Describe a task in natural language: open apps, read screens, tap, scroll, enter text, and watch the phone in a sidebar widget.

Nothing has to be installed or signed on the phone. iPhone Use uses exactly two local channels:

| Channel | Covers | How |
| --- | --- | --- |
| CoreDevice | Screenshots, coordinate taps, swipes, keyboard, Home / volume keys, unlock, app launch | `pymobiledevice3` in its own interpreter, driven over a JSON-lines subprocess protocol |
| AXe | Element tree, selector taps | The local `axe` binary, reading the accessibility tree through the CoreDevice tunnel |

Both run on the Mac. USB traffic goes straight through `pymobiledevice3`: no port forwarder, no HTTP server, no on-device app. When element targeting fails, the tool returns the current screenshot and point scale so the model can fall back to coordinates.

## Requirements

| Requirement | Purpose |
| --- | --- |
| macOS and Codex desktop / CLI | Local plugin installation, MCP, and the screen sidebar |
| A real iPhone connected over USB | Trust the Mac, enable Developer Mode, keep it unlocked while in use |
| Python 3.9+ | Run the MCP server using Python's standard library |
| A separate CoreDevice interpreter (`pymobiledevice3` + `Pillow`) | Screenshots, HID gestures, keyboard, buttons, unlock, app launch |
| The [AXe](https://github.com/cameroncooke/AXe) binary | Accessibility tree and selector taps |
| Node.js 20.19+, 22.12+, or 24+; npm 10+ | Only to build the screen widget locally |

One-time preparation (two commands, nothing to repeat later):

```bash
/opt/homebrew/bin/python3 -m venv ~/.local/share/iphone-use-coredevice/venv
~/.local/share/iphone-use-coredevice/venv/bin/pip install pymobiledevice3 Pillow

brew install cameroncooke/axe/axe   # or install AXe to ~/.local/share/iphone-use-axe/axe
```

`pua_doctor` reports whether both are in place. The defaults are `~/.local/share/iphone-use-coredevice/venv/bin/python` and `~/.local/share/iphone-use-axe/axe`; override them with `IPHONE_USE_COREDEVICE_PYTHON` and `IPHONE_USE_AXE`.

## Ask Codex to install it

Paste this prompt into Codex running on your Mac:

```text
Install and configure iPhone Use for me:
https://github.com/zhongerxin/iPhone-use

Read the repository README and installation script first. Check Codex CLI,
Python, Node.js, npm, and the iPhone connected over USB. Clone the project into
a suitable local directory and run sh scripts/install.sh.

Create the CoreDevice interpreter, install pymobiledevice3 and Pillow there, and
confirm the AXe binary is available. If this chat cannot load the new tools, tell
me to reconnect or start a new chat and continue there. Once tools are available,
read the iphone-use-setup skill, run pua_setup to discover the device and pin its
UDID, and continue until pua_ready returns ready=true. Then show the phone
screen. Do not reuse the author's device identifiers.

Help resolve missing dependencies. If device trust, Developer Mode, or unlocking
needs my input, explain the exact steps and wait for me to complete them.
```

A reconnect may be required to load newly installed tools. Device trust, Developer Mode, and unlocking must be completed by the device owner in the system UI. The repository is currently private; GitHub access or a source package is required.

## Install and connect

```sh
git clone https://github.com/zhongerxin/iPhone-use.git
cd iPhone-use
sh scripts/install.sh
```

The installer validates and stages the source, registers a local Codex marketplace, installs the plugin and skills, and registers the installed server as standard MCP `iphone_use`. Plugin and standard configuration share the same namespace to avoid duplicate tools. An extracted source package uses the same installation command.

Reconnect or start a new chat, enable **iPhone Use**, and ask:

> Use iphone-use-setup to configure my USB-connected iPhone, verify READY, and show the phone screen.

The skill runs `pua_setup` with `discover` then `configure` to pin the device UDID, then `pua_ready` proves both channels: `ready=true` only when `proof.coredevice` and `proof.axe` are both true. A locked phone returns `state: "locked"` instead of an error; unlock it and continue.

## Features

| Feature | Behavior |
| --- | --- |
| App navigation | Launch apps and navigate search, details, settings, or drafts |
| Screen reading | Compact accessibility trees, text, positions, and actual screenshots |
| Taps and gestures | Element or coordinate taps, swipes, and bounded waits |
| Text input | Complete ASCII text in one call, including newlines and tabs |
| Batch workflows | Combine known actions into one call to reduce model round trips |
| Visual recovery | Inspect a fresh screenshot when targets are occluded, ambiguous, or missing from the tree |
| Live preview | An iPhone frame, status, edge effects, and visible tap / drag indicators |
| Connection recovery | Refresh the preview; lock-induced pauses and explicit pauses stay distinct |

Example prompts:

```text
Open Notes, create a draft with the following text, verify it, and leave it there.

Open the target app, search for this name, and summarize the first few results.

Page through the whole list on this screen and record every title.
```

Text input and submission are separate; nothing is submitted for you. The model checks key screens, recipients, counts, and final results. Passwords, verification codes, and Face ID are handled by the user; preview can be paused during handoff.

Repeated preview opens in the same chat reuse the existing widget. Its toolbar offers Refresh, Home, and Screenshot. Without an image, the iPhone frame shows a dark screen with a centered status icon; the top badge displays the connection or pause state. The preview is for the user; model decisions use actual tool observations.

## Technical details

- **Direct local USB.** `pymobiledevice3` holds the tunnel and HID session in a separate process; the MCP server stays on the standard library and talks JSON lines to it. No open port, no on-device service.
- **Two explicit sources.** The element tree comes from AXe's accessibility snapshot; coordinates and HID come from CoreDevice. Only when both fail does the tool ask you to check USB, trust, and unlock.
- **Compact observations.** Repeated name / value fields are dropped and only negative states are named (`enabled:false`, `visible:false`, `in_viewport:false`); screenshots are model-sized JPEGs unless `IPHONE_USE_IMAGE=original`.
- **Screenshot fallback:** inspect the image and multiply pixels by `image.pixel_to_point` to obtain iPhone point coordinates for `pua_tap`.
- **Explicit failure semantics:** a shared operation lock guards concurrent processes; timeouts and `uncertain` results require reading the actual state before retrying.
- **Unlock is proved first.** The passcode is typed only after AXe reports a locked phone, and a fresh tree must confirm the result.
- **Preview lifecycle:** the stream comes from CoreDevice video capture and is never written to disk; lock-induced pauses and explicit pauses are distinguished, and Refresh can reconnect.

These changes reduce duplicate requests and model round trips. Overall task speed still depends on the app, the USB link, and model response time.

## Tools

There are 16 model tools and 2 additional widget-only tools.

| Tools | Purpose |
| --- | --- |
| `pua_doctor`, `pua_setup`, `pua_ready` | Diagnostics, device discovery and configuration, readiness proof |
| `pua_observe`, `pua_find` | Trees, screenshots, and target queries |
| `pua_apps`, `pua_launch_app` | App identifiers, installation evidence, app launch |
| `pua_tap`, `pua_swipe`, `pua_press_button` | Taps, swipes, Home and volume buttons |
| `pua_type_text`, `pua_wait` | ASCII input and bounded waits |
| `pua_unlock`, `pua_batch` | Unlocking and compound actions |
| `pua_screen`, `pua_metrics` | Preview controls and bounded timing statistics |

Abnormal UI states return a screenshot for the model to inspect before choosing another action. Occlusion, unproven scroll progress, input mismatch, and failed page expectations use the same fallback. Existing screenshots are reused, without automatic extra gestures or action replay.

## Runtime data and updates

New installations use `~/.local/share/iphone-use/`. Set `IPHONE_USE_STATE_DIR` for another external directory; the legacy `WDA_STATE_DIR` name remains supported. If the new default directory does not exist, an existing older configuration directory is reused so the pinned device is retained.

Runtime directories use mode 700 and private configuration / image files use 600. Explicit screenshots have retention limits; preview streams stay in memory. Device data, logs, and dependency directories are excluded from source packages.

```sh
git pull --ff-only
sh scripts/install.sh
```

Reconnect the chat after updating. For connection failures, check USB, unlocking, Developer Mode, and the specific `pua_doctor` error.

## Development and documentation

```sh
npm ci --prefix ui --no-audit --no-fund
npm run build --prefix ui
sh scripts/check.sh
python3 scripts/package.py
```

The UI builds into self-contained HTML. Source packages are written to `dist/iphone-use-<version>-source.zip`; normal installation and use do not require rebuilding the UI. Installed plugins retain the complete widget source, styles, build scripts, configuration, dependency lockfile, and widget tests for local maintenance.

See [screen preview and recovery](skills/iphone-use/references/screen.md), [app identifiers](skills/iphone-use/references/apps.md), [troubleshooting](skills/iphone-use-setup/references/troubleshooting.md), and [changelog](CHANGELOG.md).

## Dependencies and acknowledgements

This project uses CoreDevice tunnelling and HID from [pymobiledevice3](https://github.com/doronz88/pymobiledevice3), and the accessibility tree from [AXe](https://github.com/cameroncooke/AXe). iPhone Use adds the Codex plugin, MCP tools, setup guidance, and screen widget.

Thanks to the maintainers and contributors of pymobiledevice3, AXe, and related projects for making real iPhone automation possible.

MIT License. See [third-party notices](THIRD_PARTY_NOTICES.md).
