# kmcf：A股每日复盘

kmcf 是一个在 Windows 上运行的 A 股每日复盘工具。每个交易日收盘后，它从公开行情接口取数，生成一份离线 HTML 复盘报告（`reports/YYYY-MM-DD.html`），并提供一个桌面软件，用来查看历史报告、维护自选股和持仓、管理每天 19:00 的自动复盘。

报告只陈述客观数据和事实，例如“放量”“站上20日均线”“连续3日净流入”，不给出买入、卖出或持有之类的建议。

---

## 功能

### 四个复盘模块

| 模块 | 主要内容 |
|---|---|
| **大盘与情绪** | 7 个主要指数（上证、深证成指、创业板指、科创50、沪深300、中证1000、北证50）的涨跌幅；上证指数近 60 日 K 线；指数区间涨跌对比；沪深两市成交额及较前日变化，附 60 日趋势；涨跌家数与涨跌分布；涨停、跌停、炸板家数和炸板率；连板梯队与最高连板；昨日涨停股今日表现和连板晋级率；近 20 个交易日的情绪趋势；涨停池、炸板池、跌停池明细 |
| **板块轮动** | 行业和概念板块的涨跌幅排行（概念已剔除“融资融券”“昨日涨停”“指数成分”这类非题材概念）；行业和概念的资金净流入排行；涨停股的行业分布；今日最强行业近 5 日的涨跌热力图和持续性（近5日累计涨幅、上涨天数、连涨天数）；近 5 日各行业的涨停热度 |
| **龙虎榜与资金** | 龙虎榜个股（多个上榜原因合并为一行，当日口径和多日累计口径分开）；机构席位净买卖；活跃营业部；大盘主力资金流向和近 60 日趋势；个股主力净流入、净流出排行；北向成交总额；融资余额趋势 |
| **自选股/持仓** | 自选股总览：收盘价、涨跌幅、成交额、换手率、量比、均线位置、60 日区间位置；当日状态标签：涨停、N连板、炸板、跌停、上龙虎榜、放量、创60日新高或新低、上穿或跌破20日线；持仓市值、当日盈亏和浮动盈亏；每只股票的 K 线图 |

报告顶部的“今日要点”汇总各模块的前几条要点。某个子功能取数失败时，只在该模块顶部列出“部分数据未能获取”，其他内容照常生成。每个模块底部有数据说明，写明这次实际用了哪个数据源以及统计口径。

### 桌面软件

- 左侧列出历史复盘，点击即可在右侧查看报告。右键菜单可以“在浏览器中打开”“打开所在文件夹”“重新获取数据并生成”“删除该报告”。
- 「生成最新复盘」（F5）生成最近一个已收盘交易日的报告；「复盘指定日期…」只能选择已收盘的交易日，休市日显示为灰色，已有报告的日期显示为粗体。
- 「自选股 / 持仓…」可以编辑代码、名称、持仓股数、成本价和备注。支持从 Excel 粘贴多行；「补全名称」按代码自动查询股票简称。
- 「自动复盘」区域显示 19:00 定时任务的状态和下次运行时间，可以一键开启或关闭。
- 报告跟随系统的亮色或暗色主题，Ctrl + 加号 / Ctrl + 减号 缩放，Ctrl+0 恢复原始大小。
- 软件只允许运行一个实例，再次启动会切换到已经打开的窗口。

### 每天 19:00 自动复盘

开启后，Windows 任务计划程序会在每个工作日 19:00 自动生成当天的复盘报告，不需要打开桌面软件。节假日如果已有上一个交易日的报告，就跳过不生成。之所以选在 19:00：龙虎榜一般在 17:00～18:30 之间陆续发布完毕，到 19:00 时当天的盘后数据基本齐全。开启方法见下文“使用方法”。

### AI 分析（DeepSeek）

- **AI 复盘点评**：生成报告时，DeepSeek 会根据当天报告数据写一段客观综述，显示在“今日要点”下方。同一份数据只调用一次接口，结果缓存在 `data/cache/ai/`，重复生成报告不会再花费额度。AI 点评最多等待 90 秒（配置项 `ai.review_budget`），超时就跳过，不影响报告生成。
- **AI 分析面板**：左侧「AI 分析」（Ctrl+K）在右边打开对话面板。面板会自动带上当前报告的数据和近 20 个交易日的历史指标，可以直接提问，或点“总结今日市场”“板块轮动分析”“龙虎榜与资金解读”“短线情绪分析”“我的自选股表现”“对比近几日变化”。回答边生成边显示，可随时停止。模型可选 `deepseek-chat`（快）或 `deepseek-reasoner`（推理更强、较慢）。
- **API Key 只从环境变量读取**，不会写进软件、配置文件、日志或报告。默认依次读取 `DEEPSEEK_API_KEY`、`DEEPSEEK-API`。设置方法：Windows 搜索“编辑账户的环境变量”，新建变量 `DEEPSEEK_API_KEY`，值为你的 Key，保存后在 AI 面板里点「重新检测」即可，不用重启软件。没有 Key 时报告照常生成，只是没有 AI 内容。
- **隐私**：持仓股数、成本价、市值和盈亏默认**不会**发给 AI；需要时在面板上勾选“包含我的持仓数据”。
- **换用其他模型**：「AI 设置…」里可以修改服务名称、接口地址、模型和 Key 所在的环境变量名。任何兼容 OpenAI Chat Completions 接口的服务都可以使用。
- AI 只做客观的数据分析与解读，被要求不给出买卖建议或涨跌预测。AI 生成的内容仅供参考，不构成投资建议。

---

## 安装

### 环境要求

- Windows 10 或 11
- Python 3.13（开发环境为 Anaconda 的 Python 3.13.9）
- 能访问国内行情网站（东方财富、新浪财经、同花顺、腾讯证券、上交所、深交所等）

### 创建虚拟环境并安装依赖

项目目录下已经有 `.venv`，可以直接使用。需要重新创建时，在项目根目录执行：

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

`requirements.txt` 中的依赖：akshare、pandas、jinja2、PySide6（含 Qt WebEngine，用于在软件里显示报告）。目前验证过的版本组合是 akshare 1.19.1、pandas 3.0.6、numpy 2.5、jinja2 3.1.6、PySide6 6.12。AKShare 更新比较频繁，接口返回的列可能变化，升级前建议先备份 `.venv`。

### 说明：Anaconda 环境下的 Qt DLL 冲突

如果虚拟环境是用 Anaconda 的 Python 创建的，导入 PySide6 时可能报错：

```
ImportError: DLL load failed while importing QtCore: 找不到指定的程序。
```

原因是 Anaconda 会把 `anaconda3\Library\bin` 加进 DLL 搜索路径，那里有一份 ICU 73 的 `icuuc.dll`，而 pip 安装的 Qt 6 依赖的是 Windows 系统自带的 ICU，结果加载错了版本。

`kmcf/qt_compat.py` 中的 `prepare()` 会在导入 PySide6 之前，按完整路径先加载 `C:\Windows\System32` 下的 `icuuc.dll`、`icuin.dll` 和 `icu.dll`，Qt 随后就会复用这几份 DLL。`main.py` 和 `kmcf/gui/app.py` 已经调用了它。自己写的脚本如果要导入 PySide6，也要先执行：

```python
from kmcf import qt_compat
qt_compat.prepare()   # 必须在 import PySide6 之前
```

---

## 打包与安装（绿色版）

绿色版就是一个文件夹：`kmcf.exe` 加一个 `_internal` 子目录，里面已经带好了 Python、Qt WebEngine 和全部依赖。不需要安装 Python 或 Anaconda，放到任意位置双击就能运行，也可以复制到别的电脑上使用。

### 构建

在项目根目录执行（需要先按上文准备好 `.venv`，`requirements.txt` 已包含 PyInstaller）：

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1
```

一次构建大约 2～3 分钟，`build.ps1` 依次完成：

1. 按 `kmcf.spec` 用 PyInstaller 构建到 `build\pyi-dist\kmcf`（中间文件在 `build\pyi-work`）。
2. 用 `robocopy /MIR` 同步到 `dist\kmcf`，但跳过 `dist\kmcf` 下的 `data`、`reports`、`logs` 三个目录，所以重新构建不会丢失用户数据。
3. 首次构建（`dist\kmcf\data` 还不存在）时，把项目里的 `data\config.json`、`data\history.csv`、`data\cache\` 和 `reports\`（含 `assets\echarts.min.js`，不含开发预览 `_dev_*.html`）复制过去，软件一打开就有自选股、趋势数据和已有的报告。
4. 检查打包结果：不能含有 `icu*.dll`，必须有 OpenSSL（`libssl`、`libcrypto`）、Qt WebEngine、中文翻译、报告模板、akshare 数据文件、mini_racer、curl_cffi 和证书。
5. 打印 `dist\kmcf` 的总大小（程序部分约 430 MB）。

可选参数：

| 参数 | 作用 |
|---|---|
| `-Clean` | 删除 `build\pyi-work` 缓存后完整重建，升级依赖后建议使用 |
| `-Selftest` | 构建后运行 `kmcf.exe --selftest`：打开主窗口、加载最新报告、等图表画完后截图，结果保存为 `build\selftest.png` 和 `build\selftest.json`，退出码 0 表示正常 |
| `-Icon` | 重新生成程序图标 `assets\kmcf.ico`（由 `scripts\make_icon.py` 绘制，与软件窗口图标一致） |
| `-Dist <目录>` | 输出到别的目录，例如直接升级已经在用的 `D:\Apps\kmcf`。为防误删，目录已存在且不是空目录时，里面必须已有 `kmcf.exe` |
| `-NoBuild` | 跳过 PyInstaller，只把上次的构建结果同步到输出目录 |

构建前请先关闭正在运行的 `dist\kmcf\kmcf.exe`，否则文件被占用，脚本会提示后退出。

> 打包环境是用 Anaconda 的 Python 创建的虚拟环境，`kmcf.spec` 处理了两个问题：一是不把 Anaconda 的 `icu*.dll` 打进包，Qt 必须使用 Windows 自带的 ICU（原因见上一节）；二是 OpenSSL 等动态库固定使用构建所用 Python 实际加载的那一份，不会因为 PATH 里排在前面的 Git 等程序而打错版本。

### 产物与首次运行

```
dist\kmcf\
├─ kmcf.exe          双击运行
├─ _internal\        程序本体（Python、Qt、依赖库），不要改动
├─ data\             配置和数据：config.json、history.csv、cache\、raw\
├─ reports\          生成的报告；assets\ 中是 echarts.min.js
└─ logs\kmcf.log     运行日志（第一次运行时创建）
```

- 双击 `kmcf.exe` 打开软件，功能与源码版完全相同。
- 复制到别的电脑（Windows 10/11 64 位）时，复制整个 `dist\kmcf` 文件夹，不要只复制 `kmcf.exe`。不想带上自己的数据时，只复制 `kmcf.exe` 和 `_internal` 即可：第一次运行会自动创建空的 `data`、`reports`、`logs`，第一次生成报告时要联网下载图表组件并回补近期历史数据，耗时会长一些。
- 文件夹要放在当前用户能写入的位置（如 `D:\Apps\kmcf`、文档目录），不要放到 `C:\Program Files` 下，因为数据都写在 exe 旁边。
- 程序没有数字签名，第一次运行时 Windows 可能提示“Windows 已保护你的电脑”，点「更多信息」→「仍要运行」。开启了“智能应用控制”的 Windows 11 偶尔会直接拦下新构建的 kmcf.exe（提示“应用程序控制策略已阻止此文件”），稍后再打开一次通常就能运行；如果一直被拦，需要给 kmcf.exe 做代码签名，或者改用源码版。
- 刚复制过去后第一次启动会慢一些（杀毒软件要扫描几百个文件，约十几到三十秒），之后启动只要一两秒。

### 创建桌面快捷方式

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
```

默认指向 `dist\kmcf\kmcf.exe`（还没有打包时改为用 `pythonw` 运行源码版 `main.py`），在桌面创建“kmcf A股复盘”快捷方式，图标取自 exe。绿色版放到别处后，用 `-Target` 指定 exe 或它所在的文件夹：

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1 -Target D:\Apps\kmcf\kmcf.exe
```

### 数据目录

绿色版的数据全部放在 exe 旁边的 `data\`、`reports\`、`logs\` 中，结构与源码版相同，和项目根目录下源码版的数据互不影响。备份或迁移时复制这三个目录即可。

### 开启 19:00 自动复盘

二选一：

1. 在软件左下角的「自动复盘」区域点「开启」；
2. 在绿色版文件夹中执行 `kmcf.exe schedule install`（另有 `schedule status`、`schedule uninstall`）。

绿色版注册的定时任务执行的是 `kmcf.exe --scheduled`（起始位置为 exe 所在文件夹），不需要 Python。注意：

- `kmcf.exe` 是窗口程序，在 cmd 里直接带参数运行时看不到输出，可以在命令后面加 `| more`，例如 `kmcf.exe schedule status | more`。
- 定时任务记录的是 exe 的完整路径。把绿色版文件夹移到别处，或者以前在源码版里开启过自动复盘，都要在新位置的软件里把「自动复盘」关闭再开启一次，否则任务仍会运行旧位置的程序，报告也生成在旧位置。

### 更新软件

代码更新后重新运行 `build.ps1` 即可：只替换 `kmcf.exe` 和 `_internal`，`dist\kmcf` 里的 `data`、`reports`、`logs` 不会被覆盖或删除。如果绿色版已经复制到别的位置，可以用 `build.ps1 -Dist D:\Apps\kmcf` 直接更新那个目录；也可以先关闭软件，再手动用新的 `kmcf.exe` 和 `_internal` 替换旧的，保留三个数据目录。

---

## 使用方法

### 启动桌面软件

- 双击项目根目录下的 **`启动kmcf.bat`**。它用 `pythonw` 启动，不会出现黑色控制台窗口。
- 想在桌面放一个快捷方式，可以在项目根目录执行：
  ```bat
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
  ```
  桌面上会出现“kmcf A股复盘”快捷方式。
- 也可以在命令行运行：`.venv\Scripts\python.exe main.py`
- 已经构建了绿色版的话，直接双击 `dist\kmcf\kmcf.exe`，见上文“打包与安装（绿色版）”。

第一次使用，建议先打开「自选股 / 持仓…」，填入自己的股票（持仓股数填 0 表示只关注、不持仓），再点「生成最新复盘」。

### 开启 19:00 自动复盘

二选一：

1. 在桌面软件左下角的「自动复盘」区域点「开启」，默认时间为每个工作日 19:00。
2. 在命令行（项目根目录）执行：

   | 命令 | 作用 |
   |---|---|
   | `.venv\Scripts\python.exe -m kmcf.cli schedule install` | 开启，时间取配置里的 `schedule_time`，默认 19:00 |
   | `.venv\Scripts\python.exe -m kmcf.cli schedule status` | 查看是否已开启、下次运行时间、上次运行结果 |
   | `.venv\Scripts\python.exe -m kmcf.cli schedule uninstall` | 关闭自动复盘 |

定时任务的细节：

- 任务名为 `kmcf_daily_review`，触发时间为周一至周五 19:00，执行命令是 `.venv\Scripts\pythonw.exe run_daily.py`（绿色版是 `kmcf.exe --scheduled`），没有窗口。
- 节假日：如果“最近一个交易日”的报告已经存在，就直接跳过。
- 任务以当前用户身份运行，**只在你登录 Windows 时才会执行**，不需要输入密码。如果 19:00 时电脑关机或没有登录，会在下次开机登录后尽快补跑。
- 单次运行最长 30 分钟，同一时间只允许一个实例运行。
- 一次生成通常需要 10～40 秒。新电脑上第一次运行时要回补近期历史数据，大约需要 1～3 分钟。

> 实时快照类数据（涨跌分布、板块实时排行、个股资金排行）只能在复盘日收盘后到下一个交易日 9:00 之间取得。如果补跑发生在第二天的交易时段（9:00～15:05）或更晚，这部分会被跳过，报告顶部会给出提示，**事后也无法补回**。涨停池、龙虎榜、K 线这类按日期查询的数据不受影响。所以请尽量让电脑在交易日 19:00 时开机并已登录。

### 命令行生成报告

| 命令 | 作用 |
|---|---|
| `.venv\Scripts\python.exe -m kmcf.cli generate` | 生成最近一个已收盘交易日的报告 |
| `.venv\Scripts\python.exe -m kmcf.cli generate --date 20260930` | 生成指定交易日的报告 |
| `.venv\Scripts\python.exe -m kmcf.cli generate --force` | 忽略缓存，重新取数 |
| `.venv\Scripts\python.exe -m kmcf.cli generate --open` | 生成后用浏览器打开 |

生成结果为 `reports\YYYY-MM-DD.html`，同目录还有一份同名 `.json`，保存报告的全部数据。日期不是交易日或还没有收盘时，命令会给出提示并以退出码 2 结束。

### 修改配置

配置文件为 `data\config.json`，一般通过桌面软件修改即可：

```json
{
  "watchlist": [{"code": "600519", "name": "贵州茅台", "shares": 0, "cost": 0.0, "note": ""}],
  "top_n": 20,
  "schedule_time": "19:00"
}
```

- `shares`：持仓股数，0 表示只关注。`cost`：持仓成本价（元/股）。
- `top_n`：各类排行显示的条数，范围 5～100。
- `schedule_time`：自动复盘时间，默认 19:00。

---

## 数据源与口径

所有网络请求都经过统一的取数器，它带磁盘缓存（`data/raw/YYYYMMDD/`）、失败重试 3 次和全局限速。同一交易日内，同一个接口加同一组参数只请求一次，重复生成报告会很快。每个模块都有主数据源和备用源，报告中写明实际用了哪一个。

| 内容 | 主数据源 | 备用数据源 |
|---|---|---|
| 指数日线 | 东方财富历史 K 线（push2his） | 新浪财经指数日线（没有成交额，K 线下方的柱改为成交量） |
| 两市成交额 | 东方财富指数日线中上证指数与深证成指的成交额 | 上交所、深交所官网每日成交概况（“股票”成交金额）；再不行就用本地历史记录 |
| 涨跌家数 / 涨跌分布 | 东方财富涨停板专题“涨跌分布”（沪深京A股，含北交所，不含ST） | 乐咕乐股市场活跃度（口径不同，单独存放） |
| 涨停、炸板、跌停、昨日涨停池 | 东方财富涨停板专题（按日期查询，不含ST，只能回查约 30 个自然日） | 更早的日期使用本地历史记录 |
| 行业 / 概念排行与资金 | 东方财富板块行情（push2） | 同花顺行业一览和资金流向；新浪财经行业和概念板块 |
| 近 5 日板块轮动 | 本地逐日累积的排行（`data/cache/sector_daily.csv`） | 同花顺行业指数日线，或东方财富板块日线 |
| 龙虎榜、机构、营业部 | 东方财富数据中心（按日期查询） | 无 |
| 大盘主力资金流 | 东方财富大盘资金流（push2his） | 新浪财经个股资金流，只统计主力净流入和净流出的家数，不给出合计金额 |
| 个股主力资金排行 | 东方财富个股资金流（push2） | 新浪财经资金流向；同花顺个股资金流 |
| 北向资金 | 东方财富数据中心沪深港通成交 | 无 |
| 两融余额 | 东方财富数据中心两融账户统计 | 东方财富数据中心沪深两市融资融券合计 |
| 自选股日 K 线 | 东方财富历史 K 线（前复权） | 腾讯证券日 K 线（前复权，单位已换算一致） |
| 股票名称 | 新浪财经实时行情 | 腾讯行情；上交所、深交所、北交所的股票名单 |
| 交易日历 | 新浪财经交易日历（本地缓存 7 天） | 获取失败时按工作日估算，报告上会标注“交易日历为估算” |

### 需要了解的几点

- **东方财富 push2 / push2his 可能被拦截。** 开发期间（2026-10-11～12）实测，东方财富实时行情列表（push2）和历史 K 线（push2his）的连接一直被服务器重置，可能是按 IP 限流。软件会自动切换到上表的备用源。同一次生成中，某个站点失败一次后，后续请求直接跳过该站点，不会每个请求都等待重试。这两个接口恢复后会自动重新使用，不需要任何设置。
- **不同数据源的口径不同。** 新浪和东方财富划分“主力资金”的标准不同，合计值可能相差很大，甚至方向相反，所以新浪备用源只统计家数。同花顺有 90 个行业，新浪只有 49 个粗分行业，涨停股的行业则用东方财富或申万二级行业。报告中都有注明。
- **龙虎榜的发布时间。** 龙虎榜通常在 17:00～18:30 之间分批发布。20:00 之前生成当天的报告时，龙虎榜、资金流、自选股日 K 这类盘后数据每次都会重新获取，不读缓存。17:00 之前生成，会显示“龙虎榜尚未发布”。
- **北向资金不再披露每日净买入。** 从 2024 年 8 月起，沪深港通不再公布每日净买入额，报告只展示北向成交总额，以及龙虎榜上沪股通、深股通专用席位的净额，两者都不代表北向资金整体的净流向。
- **两融数据按 T+1 披露。** 19:00 生成时通常只有上一交易日的两融数据，报告会标明实际的数据日期。
- **实时快照类数据只能代表最近一个交易日。** 涨跌分布、板块实时排行、个股资金排行这类接口不能按日期查询。复盘历史日期时，只能使用当天曾经缓存的数据，没有缓存的部分会跳过，报告顶部有提示。涨停池、龙虎榜、K 线这类按日期查询的数据不受影响。
- **趋势图会逐日累积。** 情绪趋势、成交额趋势、近 5 日板块轮动依赖本地累积的 `data/history.csv` 和 `data/cache/sector_daily.csv`。新安装后的前几天，趋势图的天数会少一些，每天运行后会自动补齐。

---

## 目录结构

```
kmcf/
├─ 启动kmcf.bat            双击启动桌面软件
├─ main.py                 软件入口（无参数打开界面；generate / schedule / --scheduled 走命令行；--selftest 为打包冒烟测试）
├─ run_daily.py            定时任务入口（任务计划程序用 pythonw 调用）
├─ requirements.txt
├─ build.ps1               构建绿色版到 dist\kmcf（保留其中的 data、reports、logs）
├─ kmcf.spec               PyInstaller 打包配置（由 build.ps1 调用）
├─ assets/                 程序图标 kmcf.ico 和各尺寸 PNG
├─ kmcf/
│  ├─ cli.py               命令行：generate / schedule
│  ├─ report.py            生成报告：依次调用各模块、渲染 HTML
│  ├─ scheduler.py         Windows 任务计划（注册、删除、查询状态）
│  ├─ fetch.py             带缓存、重试、限速的取数器
│  ├─ context.py           传给各模块的运行上下文
│  ├─ history.py           逐日指标累积（data/history.csv）
│  ├─ trade_calendar.py    交易日历
│  ├─ section.py           报告模块构建器（KPI、图表、表格、梯队）
│  ├─ render.py            HTML 渲染（ECharts 5.5.1）
│  ├─ net.py               国内行情域名绕过系统代理
│  ├─ qt_compat.py         Anaconda 下 Qt DLL 冲突修复
│  ├─ config.py / paths.py 配置读写和路径
│  ├─ templates/report.html.j2
│  ├─ sections/            四个复盘模块：market、sectors、funds、watchlist（_ 开头的是各模块的辅助文件）
│  └─ gui/                 桌面软件（PySide6）
├─ scripts/
│  ├─ dev_section.py       单独运行一个模块并生成预览（开发用）
│  ├─ make_icon.py         生成 assets\kmcf.ico
│  └─ create_shortcut.ps1  创建桌面快捷方式
├─ tests/                  单元测试
├─ data/                   运行时数据（不要手动修改 history.csv）
│  ├─ config.json          自选股和配置
│  ├─ history.csv          逐日累积的指标，用于画趋势
│  ├─ cache/               交易日历、板块逐日排行（sector_daily.csv）、股票名称缓存
│  └─ raw/YYYYMMDD/        每个交易日的原始数据缓存
├─ reports/                生成的报告（YYYY-MM-DD.html / .json），assets/ 中是本地 echarts.min.js
├─ logs/kmcf.log           运行日志
├─ build/                  打包中间文件（可以删除）
└─ dist/kmcf/              绿色版：kmcf.exe + _internal\，以及它自己的 data\、reports\、logs\
```

---

## 常见问题

**电脑开了代理软件，取数失败？**
`kmcf/net.py` 会把东方财富、新浪、同花顺、乐咕乐股、交易所等国内数据域名加入 `NO_PROXY`，让这些请求绕过系统代理直连。如果代理软件开了 TUN 或全局模式，流量仍可能被接管，请在代理软件里把这些国内域名设为直连，或者生成报告时暂时关闭代理。

**报告里有“部分数据未能获取”，或者某个表格是空的？**
- 先看该模块底部的数据说明，以及 `logs/kmcf.log` 中的 WARNING 记录，里面写明了失败的接口和原因。
- 常见原因：东方财富接口被限流（会自动用备用源，有些内容备用源也没有，例如大盘主力资金的金额）；复盘的是历史日期，实时快照类数据没有缓存；在交易时段生成；17:00 之前生成，龙虎榜尚未发布。
- 网络恢复后，在左侧列表中右键该报告，选「重新获取数据并生成」，或在命令行加 `--force` 重新生成。这对按日期查询的数据有效。实时快照类数据过了时间窗口就取不到了，只能使用当时缓存下来的数据。

**报告里的图表不显示？**
图表依赖 `reports/assets/echarts.min.js`，第一次生成报告时会自动下载。下载失败时，报告会依次尝试几个国内外 CDN。如果把 HTML 单独复制到别的地方，又没有网络，图表就无法显示。可以连同 `assets` 文件夹一起复制，或者联网后打开。仍然不行的话，删除 `reports/assets/echarts.min.js` 后重新生成一次，让它重新下载。

**到了 19:00 没有自动生成？**
- 运行 `.venv\Scripts\python.exe -m kmcf.cli schedule status`，或查看软件左下角的「自动复盘」，确认已经开启。
- 定时任务只在你登录 Windows 时运行。19:00 时电脑关机、睡眠或没有登录，会在下次登录后补跑。
- 查看 `logs/kmcf.log`，了解当天的生成过程和错误。

**为什么“上涨/下跌家数”和别的软件不一样？**
东方财富涨跌分布统计的是沪深京A股，包括北交所，不含 ST 股。很多软件只统计沪深两市，或者包含 ST 股。报告里已注明口径。

**数据文件越来越大怎么办？**
`data/raw/` 下每个交易日约 2～3 MB，可以删除较早日期的文件夹，只影响用新数据重新生成那些日期的报告。**不要删除** `data/history.csv` 和 `data/cache/sector_daily.csv`，趋势图和近 5 日板块轮动都靠它们逐日累积。

**桌面软件打不开，提示 DLL load failed？**
参见上文“Anaconda 环境下的 Qt DLL 冲突”。请通过 `启动kmcf.bat` 或 `main.py` 启动，不要直接 `import PySide6`。

---

## 开发与测试

运行全部单元测试（只测试不联网的计算逻辑，使用合成数据）：

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

单独运行一个模块，并生成预览 `reports\_dev_<模块>.html`。模块名可选 market、sectors、funds、watchlist，还可以加 `--date YYYYMMDD`、`--no-snapshot`、`--force`、`--dump` 参数：

```bat
.venv\Scripts\python.exe scripts\dev_section.py market --date 20261009
```

在 Git Bash 里运行时，建议加上环境变量 `PYTHONIOENCODING=utf-8 PYTHONUTF8=1`，避免中文乱码。

新增复盘模块的方法：在 `kmcf/sections/` 下新建模块，提供 `ID`、`TITLE` 和 `build(ctx) -> Section`，再加到 `kmcf/sections/__init__.py` 的 `SECTIONS` 中。网络请求一律通过 `ctx.fetch(...)` 发出，每个独立子功能用 `with sec.guard('名称'):` 包起来。

---

## 免责声明

本软件的数据全部来自公开的第三方行情接口（东方财富、新浪财经、同花顺、腾讯证券、上交所、深交所、乐咕乐股等，部分通过 AKShare 获取）。数据可能延迟、缺失或有误，各数据源的统计口径也不完全相同。报告只是对市场数据的客观整理，仅供个人复盘参考，**不构成任何投资建议**，也不对任何证券的未来走势做出判断。据此操作，风险自担。股市有风险，投资需谨慎。
