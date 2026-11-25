# Any2CLI

Any2CLI 让 Agent 从桌面应用中发现操作，并把它们变成可重复调用的命令。它保存每个已观察页面的控件、可选参数和实际走过的导航路径。首次探索通常生成 4 至 12 个子命令，其他操作可以在树中搜索，第一次调用时再生成。

实时后端支持 macOS。对闭源应用，Any2CLI 优先读取辅助功能树。对于缺少可访问控件的自绘界面，它也能从目标窗口识别部分带高亮的侧栏和带下划线的页签。Apple Music 资料库和 QGIS 地理处理还可以直接使用各自的本地接口。

## 先试一次离线回放

在仓库根目录运行以下命令即可试用，需要 Python 3.9 或更高版本，以及 macOS 或 Linux。样例只包含网易云音乐公开的菜单和导航名称。

```sh
python3 -m any2cli explore com.netease.163music \
  --trace examples/netease.zh.trace.json --profile .any2cli/netease.json
python3 -m any2cli tree .any2cli/netease.json
.any2cli/netease.cli --help
.any2cli/netease.cli 精选页签 --value 排行榜 --replay
```

从推荐页调用排行榜时，返回的 `route` 包含前置操作 `侧栏 = 精选`。结果中的 `mode: recording` 和 `status: replayed` 表示这次调用的结果来自已记录的状态转换。此例有 4 个页面状态和 6 条转换。[样例操作树](examples/netease.zh.trace.json)

要试按需生成，可以找一个尚未注册的读操作，并查看它的调用计划。

```sh
python3 -m any2cli find .any2cli/netease.json 当前精选页签 --limit 1
.any2cli/netease.cli 当前精选页签 --dry-run
.any2cli/netease.cli --help
```

首次规划返回 `newly_compiled: true`。生成的命令保存在同一档案里，后续进程可以直接复用。

## 在 macOS 上探索已打开的应用

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install '.[macos]'

any2cli explore 网易云音乐 --profile .any2cli/live.json
any2cli commands .any2cli/live.json
.any2cli/live.cli 当前页面
```

实时后端使用 Python 3.10 至 3.12。默认探索只观察一次已打开的窗口。读取原生控件需要给启动 CLI 的应用授予「辅助功能」权限，识别自绘区域还需要「屏幕与系统音频录制」权限。应用有多个窗口时，可以用 `--window` 指定标题。

需要探索下一级页面时，可以指定已观察到的入口。例如，在网易云音乐推荐页开始，让目标窗口保持在前台，以下命令会试走一次“精选”并核对回程。

```sh
any2cli explore 网易云音乐 --profile .any2cli/live.json \
  --follow '侧栏=精选' --steps 1 --depth 1 --foreground
```

`--steps` 限制向前探测的次数，`--depth` 限制深度。打开菜单和实体点击需要显式指定 `--foreground`。切走窗口会停止后续前台输入。探索结束时，`exploration.restored` 表示是否已核实回到起点。

## 其他入口

| 任务 | 命令 |
| --- | --- |
| 导出完整操作树 | `any2cli tree PROFILE --json` |
| 查找或单独生成一个操作 | `any2cli find PROFILE QUERY`、`any2cli convert PROFILE QUERY` |
| 按名称、路径或操作 ID 调用 | `any2cli run PROFILE QUERY` |
| 导入 GUIrilla 操作记录 | `any2cli explore APP --graph graph/data.json --profile PROFILE` |
| 读取 Apple Music 资料库的统计项 | `any2cli explore 音乐 --library --profile PROFILE` |
| 索引本机 QGIS Processing 算法 | `any2cli explore QGIS --processing --profile PROFILE` |

`--dry-run` 用来检查路径，`--replay` 用来重放已有记录。两者均可离线运行。回归测试使用 `python -m unittest discover -s tests`，macOS 后端用模拟输入测试焦点和鼠标行为。

## 来源与许可证

项目起始于 MacPaw 的 GUIrilla，保留其 MIT 许可证和署名。桌面操作复用 OpenAdapt Flow。此仓库包含 Any2CLI 运行所需的代码。[GUIrilla 上游版本](https://github.com/MacPaw/GUIrilla/tree/15896d8c3158b7c4bbfaafc57582f1a28ff50074) · [OpenAdapt Flow](https://pypi.org/project/openadapt-flow/3.4.0/) · [MIT 许可证](LICENSE)
