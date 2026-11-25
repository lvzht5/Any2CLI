# Any2CLI

[English](README.md)

Any2CLI 让 Agent 通过命令行操作 Windows 和 macOS 桌面软件。给它一个已打开的窗口，它会从控件中识别可用操作，建立可搜索的操作树，并先生成一批常用命令。遇到尚未封装的功能，Agent 可以在树中定位操作，按需生成命令。目标软件可以是闭源软件，也无需提供公开的业务 API。

## 安装

克隆仓库，在已激活的 Python 环境中安装当前平台的后端。两个后端均支持 Python 3.12。

```sh
git clone https://github.com/lvzht5/Any2CLI.git
cd Any2CLI
```

| 平台 | 在仓库目录内安装 | 如何指定已打开的软件 |
| --- | --- | --- |
| Windows | `python -m pip install ".[windows]"` | 可执行文件名，如 `<进程名>.exe`，或可执行文件的完整路径 |
| macOS | `python -m pip install ".[macos]"` | Bundle ID、已安装的应用名或完整的 `.app` 路径 |
| 离线使用 | `python -m pip install .` | 在受支持的 Python 平台上读取已有配置或导入探索记录 |

## 从窗口生成命令

将下方大写参数换成实际的应用、操作、命令及输入值。保持目标窗口打开。如果同一软件有多个匹配窗口，在实时探索和调用时加上 `--window "完整窗口标题"`。

```sh
any2cli explore "APPLICATION" --profile workspace.json
any2cli commands workspace.json
any2cli tree workspace.json
any2cli find workspace.json "SEARCH TERMS"
any2cli convert workspace.json "OPERATION"
any2cli app workspace.json COMMAND --help
any2cli app workspace.json COMMAND --dry-run
```

首次扫描只观察当前窗口。可区分的操作足够多时，默认先生成 4–12 个命令。`--primary N` 可指定首批数量。后续对同一配置进行探索，会合并新发现的状态和命令。

需要探索下一层时，可以用 `--steps` 限制向前探测的次数，用 `--depth` 限制深度。探索器会尝试回到起始状态，并报告未能还原的路径。用 `--follow "控件名"` 或 `--follow "选择器=选项"` 可以指定探测对象。

```sh
any2cli explore "APPLICATION" --profile workspace.json --steps 3 --depth 2
any2cli tree workspace.json --json
```

去掉 `--dry-run` 即可实际调用。文本框接受字符串，选择器接受已列出的选项，开关接受 `true` 或 `false`。生成的启动脚本位于配置文件旁边。

| 调用方式 | 执行带参数的操作 |
| --- | --- |
| Windows PowerShell | `.\workspace.cmd COMMAND --value "VALUE"` |
| macOS | `./workspace.cli COMMAND --value "VALUE"` |
| 两个平台通用 | `any2cli run workspace.json "OPERATION" --value "VALUE"` |

Windows 启动脚本使用当前 `PATH` 中已安装的 `any2cli.exe`。启动脚本和 `run` 都可以接受尚未转换的操作 ID 或唯一名称，在同一次调用中完成注册和执行。使用 `--replay` 可以检查配置中记录过的结果。实时调用会区分三种情况：`verified_ui` 表示已核对目标状态；`observed_ui_change` 表示观察到了界面变化，但没有更具体的结果可核对；`unverified` 表示未能确认效果。

## 桌面后端

**Windows** 通过 Python 的 `uiautomation` 库使用微软 UI Automation。后端在当前桌面会话内按进程和窗口句柄绑定可见窗口，借助控件公开的模式调用按钮、读写文本、切换开关、选择页签和展开或折叠控件。整个过程无需全局鼠标点击或模拟键入。扫描会跳过密码框和大段文档内容。可发现的功能取决于软件暴露的 UI Automation 控件。[微软 UI Automation 控件模式](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controlpatternsoverview) · [Python UIAutomation](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)

**macOS** 使用辅助功能接口和 OpenAdapt。实时调用需要为启动 CLI 的应用授予辅助功能权限。后端从原生控件读取名称、角色和值。如果窗口暴露的控件较少，且已经授予屏幕录制权限，可选的视觉识别可以补充已选中的导航项。需要前台交互的操作必须显式传入 `--foreground`，目标窗口也必须已经拥有焦点。[OpenAdapt Flow](https://github.com/OpenAdaptAI/openadapt-flow)

配置以 JSON 保存在本地，可能包含窗口标题和控件标签。自由输入的文本不会写入导航记录。默认目录是 `.any2cli/`。已有 `any2cli.trace.v1` 记录可以通过 `explore --trace FILE` 导入，GUIrilla 的图文件可以通过 `--graph FILE` 导入。[GUIrilla](https://github.com/MacPaw/GUIrilla)

## 开发

运行 `python -m unittest discover -s tests -v` 执行回归测试。Windows 集成测试会在测试会话中创建临时 WPF 窗口，通过公共 CLI 操作并核对结果。项目使用 [MIT 许可证](LICENSE)。
