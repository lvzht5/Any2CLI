# Any2CLI

[简体中文](README.zh-CN.md)

Any2CLI gives agents a command-line interface to desktop apps on Windows and macOS. Point it at an open window and it builds a searchable tree of the operations exposed by its controls. It generates an initial set of commands, then lets an agent turn another operation into a command when it needs it. The target can be closed-source and does not need a public application API.

## Install

Clone the repository and, in an activated Python environment, install the backend for your desktop. Python 3.12 is supported by both backends.

```sh
git clone https://github.com/lvzht5/Any2CLI.git
cd Any2CLI
```

| Platform | Install from the checkout | Identify an open application |
| --- | --- | --- |
| Windows | `python -m pip install ".[windows]"` | Executable filename such as `<process>.exe`, or the full path to the executable |
| macOS | `python -m pip install ".[macos]"` | Bundle identifier, installed name or full `.app` path |
| Offline use | `python -m pip install .` | Work with an existing profile or import a trace on any supported Python platform |

## From a window to commands

Replace the uppercase arguments below with the target, operation, command and value for your window. Keep the target open. Add `--window "Exact title"` to live calls if several windows match.

```sh
any2cli explore "APPLICATION" --profile workspace.json
any2cli commands workspace.json
any2cli tree workspace.json
any2cli find workspace.json "SEARCH TERMS"
any2cli convert workspace.json "OPERATION"
any2cli app workspace.json COMMAND --help
any2cli app workspace.json COMMAND --dry-run
```

The first scan observes the current window. By default, it registers 4–12 commands when enough distinct controls exist. Set `--primary N` to choose the initial count. On subsequent scans of the same profile, newly discovered states and commands are merged with the existing registry.

To explore beyond the first state, give the agent a navigation budget. `--steps` limits forward probes and `--depth` limits nesting. The explorer attempts to restore the starting state and reports any incomplete return. `--follow "CONTROL"` or `--follow "SELECTOR=OPTION"` directs a probe to a particular operation.

```sh
any2cli explore "APPLICATION" --profile workspace.json --steps 3 --depth 2
any2cli tree workspace.json --json
```

For a live call, omit `--dry-run`. A field accepts text, a choice accepts one of its listed options, and a switch accepts `true` or `false`. The generated launcher sits beside the profile.

| Interface | Call a command that accepts a value |
| --- | --- |
| Windows, from PowerShell | `.\workspace.cmd COMMAND --value "VALUE"` |
| macOS | `./workspace.cli COMMAND --value "VALUE"` |
| Either platform | `any2cli run workspace.json "OPERATION" --value "VALUE"` |

The Windows launcher uses the installed `any2cli.exe` on `PATH`. Agents passing arbitrary text can call that executable directly with an argument list. Both the launcher and `run` can resolve an uncompiled operation by its ID or unique name, register it, and invoke it in one call. Use `--replay` to check an outcome recorded in the profile. A live call reports `verified_ui` when its postcondition is met, `observed_ui_change` when the UI changes without a more specific postcondition, and `unverified` when it cannot confirm an effect.

## Desktop backends

**Windows** uses Microsoft UI Automation through the Python `uiautomation` package. It attaches to a visible window in the same desktop session, binds it by process and window handle, and uses control patterns to invoke buttons, read and set fields, change switches, select tabs, and expand or collapse controls. It sends no global mouse clicks or keystrokes. Password fields and bulk document contents are skipped. Coverage depends on what an app exposes through UI Automation. [UI Automation control patterns](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controlpatternsoverview) · [Python UIAutomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)

**macOS** uses Accessibility and OpenAdapt. Grant Accessibility access to the app launching the CLI for live use. Native controls expose names, roles and values. Where the accessibility surface is sparse, the optional visual path can identify selected navigation groups if Screen Recording access is already granted. Foreground-only actions require `--foreground` and a target window that already has focus. [OpenAdapt Flow](https://github.com/OpenAdaptAI/openadapt-flow)

Profiles are local JSON files and can contain window titles and control labels. Free-form field values are kept out of saved navigation evidence. The default location is `.any2cli/`. For offline exploration, `explore` accepts `--trace FILE` for an `any2cli.trace.v1` recording or `--graph FILE` for a GUIrilla graph. [GUIrilla](https://github.com/MacPaw/GUIrilla)

## Development

Run the regression suite with `python -m unittest discover -s tests -v`. The Windows integration test opens a temporary WPF window in the test session and exercises it through the public CLI. Any2CLI started from MacPaw’s GUIrilla and retains its original [MIT license](LICENSE).
