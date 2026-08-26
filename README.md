# 豆瓣书影音记录抓取与分析

抓取自己豆瓣账号标记的电影、音乐、书籍，导出 CSV，生成可视化分析面板，并可调用大模型自动总结观影/听歌品味。也可为电影补充 IMDb 编号，用于迁移到 Letterboxd。

**只抓你自己账号的公开标记数据，需要你自己的登录凭据。** 请遵守豆瓣的使用条款，控制抓取频率。

## 快速开始

```bash
pip install -r requirements.txt
streamlit run douban_dashboard.py
```

浏览器打开后，在左侧填豆瓣 ID 并登录，即可在三个页签里完成抓取、分析、AI 总结。命令行脚本（`douban_export.py` / `douban_imdb_enrich.py`）是同一套逻辑的无界面版本，适合定时任务。

## 功能

- 三类内容 × 三种状态（想看/在看/看过）× 四种时间范围（近 3 个月 / 近 1 年 / 近 3 年 / 全部）
- 边抓边落盘，断点续抓，按条目 ID 去重
- 识别 403、登录跳转、验证码，以及返回 200 但内容为空的软限流，自动退避重试
- 电影可深度抓取详情页，补充导演、类型、IMDb 编号
- 分析面板：年代分布、逐年分布、评分分布、月度标记趋势、Top 导演/类型/艺术家/作者
- AI 总结支持 Anthropic、OpenAI、Gemini、DeepSeek、Moonshot 及任意 OpenAI 兼容接口

> **维护约定**：本 README 与三个脚本同步维护。任何脚本改动后，必须同时更新本文档的对应章节和底部的更新日志。如果由 AI 助手修改脚本，README 的更新是交付的一部分，不可省略。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `douban_export.py` | 抓取「看过的电影」「听过的音乐」「读过的书」列表页，导出 CSV |
| `douban_dashboard.py` | Streamlit 应用：抓取 + 分析面板 + AI 总结（主入口） |
| `douban_imdb_enrich.py` | 读取电影 CSV，逐条抓详情页，补充 IMDb 编号、原名、年份、导演 |
| `douban_movie_collect_<uid>.csv` | 电影标记数据（`douban_export.py` 生成） |
| `douban_music_collect_<uid>.csv` | 音乐标记数据（`douban_export.py` 生成） |
| `douban_book_collect_<uid>.csv` | 书籍标记数据（`douban_export.py` 生成） |
| `imdb_progress.csv` | IMDb 抓取进度文件（自动生成，不要手动编辑或删除） |
| `douban_movie_with_imdb.csv` | 最终合并结果，电影数据 + IMDb 编号等四列 |
| `douban_debug_*.html` | 被限流时保存的页面证据，诊断用，可删 |

## 环境准备

```bash
pip install -r requirements.txt
```

Windows PowerShell 下若 `python` 不在 PATH，用你的解释器全路径代替，例如 `D:\Python\python.exe -m pip install -r requirements.txt`。

## Cookie 获取（命令行脚本需要；Streamlit 应用可直接用账号密码登录）

豆瓣对匿名访问拦截很严，实测必须带登录 Cookie。

1. 浏览器登录 douban.com，按 F12（或 Ctrl+Shift+I）打开开发者工具
2. 切到 Network（网络）标签，按 Ctrl+R 刷新页面
3. 点请求列表第一条（一般是 www.douban.com），右侧 Headers → Request Headers，复制 `Cookie:` 后面整段
4. 在 PowerShell 里设置环境变量。**必须用单引号**，因为 Cookie 里含双引号：

```powershell
$env:DOUBAN_COOKIE = '粘贴Cookie到这里'
```

环境变量只在当前 PowerShell 窗口有效，换窗口要重新设置。Cookie 中 `dbcl2` 是登录凭证，注意保密；全部任务结束后建议退出豆瓣重新登录使旧 Cookie 失效。

## 使用流程

### 第一步：抓列表（douban_export.py）

```powershell
cd /path/to/douban-collection-analyzer
$env:DOUBAN_COOKIE = '你的Cookie'
# 先试跑一页确认能通
python douban_export.py --uid <你的豆瓣ID> --max-pages 1
# 确认没问题后跑全量
python douban_export.py --uid <你的豆瓣ID> --delay-min 6 --delay-max 12
```

常用参数：`--type movie|music|book|all`（默认 all，三类都抓；`both` 保留兼容，等同 all）、`--outdir` 输出目录（默认为脚本所在目录）、`--max-pages` 每类最多抓几页（调试用）。

自 v1.3 起，所有输出（CSV、进度文件、debug 文件）默认生成在**脚本所在目录**，与运行时所在目录无关，不 `cd` 也不会把文件落错地方。

CSV 字段：category, title（主标题）, alt_title（原名）, subject_id, url, my_rating（我的评分 1-5）, mark_date（标记日期）, my_tags（标签，`|` 分隔）, my_comment（短评）, intro（条目信息行）, year（年份，从 intro 提取，电影为上映年、音乐为发行年）, cover（封面链接）。

**关于导演**：豆瓣列表页的简介行只有上映日期和主演，不含导演，因此列表抓取阶段拿不到导演。导演由第二步 `douban_imdb_enrich.py` 从详情页提取，出现在最终合并文件的 directors 列。

**旧格式自动迁移**：用新版脚本续抓旧 CSV 时会自动升级列结构，已有行的 year 从 intro 回填，无需重抓。

### 第二步：补 IMDb 编号（douban_imdb_enrich.py）

必须在第一步完整跑完之后进行。

```powershell
cd /path/to/douban-collection-analyzer
$env:DOUBAN_COOKIE = '你的Cookie'
python douban_imdb_enrich.py --input douban_movie_collect_<你的豆瓣ID>.csv --limit 300
```

每次跑 300 条约一小时。**重复运行同一条命令**即可续抓下一批，直到「未抓: 0」。每批结束自动生成合并文件 `douban_movie_with_imdb.csv`。

常用参数：`--limit` 每次抓多少条（0 为不限，不建议）、`--progress` 进度文件路径（默认在脚本所在目录）、`--output` 合并输出路径（默认在脚本所在目录）、`--delay-min/--delay-max` 请求间隔（默认 8-15 秒）。`--input` 给相对路径时，当前目录找不到会自动去脚本所在目录找。

进度文件中 imdb_id 的特殊值：`NOT_FOUND` 表示详情页没标 IMDb 编号（部分华语片、冷门片），`GONE` 表示条目已被豆瓣删除。这两类不会重复抓取，合并输出时该字段留空。

## 断点续抓机制

两个脚本都是边抓边落盘，中断不丢数据：

- `douban_export.py`：重跑同一命令，从已有 CSV 的行数推算页码继续，按 subject_id 去重
- `douban_imdb_enrich.py`：重跑同一命令，自动跳过 `imdb_progress.csv` 里已有的条目

## 限流与封禁

这是本工具最大的不确定因素，脚本逻辑本身次要。

- 豆瓣返回 403、跳转登录页、验证码页时，脚本会识别并提示
- 豆瓣有时返回 200 状态但内容为空的「软限流」页面。`douban_export.py` 遇到时会保存证据到 `douban_debug_*.html` 并等 60/120/180 秒重试三次；`douban_imdb_enrich.py` 等 2/4/6 分钟重试三次，失败则保存进度退出
- 被拦后等几个小时再跑，不要缩短 delay 硬冲，只会延长封禁
- 带 Cookie 高频抓取时，风控对象可能是账号而不只是 IP，所以详情页抓取务必分批（`--limit 300`），分两三天跑完

## 常见问题

**`can't open file ... No such file or directory`**：路径不对，先 `cd` 到脚本所在目录或用完整路径。

**PowerShell 报「表达式或语句中包含意外的标记」**：Cookie 用了双引号包裹，改用单引号。

**第一页就 403**：没带 Cookie 或 Cookie 失效，重新从浏览器复制。

**抓到一半提示「返回空页面…疑似被限流」且三次重试失败**：正常现象，等几小时重跑同一命令续抓。

**音乐/电影数量对不上**：对照豆瓣主页「看过/听过」的总数和 CSV 行数（减表头），少了就重跑续抓。

## 可视化应用 (douban_dashboard.py)

Streamlit 本地网页，把抓取、分析、AI 总结整合成三个页签。

```powershell
python -m pip install -r requirements.txt
python -m streamlit run douban_dashboard.py
```

启动后浏览器自动打开 localhost:8501。左侧边栏填豆瓣 ID，然后登录豆瓣。登录有两种方式：默认「账号密码」，填手机号/邮箱和密码点登录即可（密码只发给豆瓣官方接口，不保存；如豆瓣要求验证码会提示换方式）；「Cookie (高级)」适合密码登录被风控拦住的情况，输入框旁有图文步骤说明。Anthropic API Key 供 AI 总结用，可留空。

**① 抓取数据**：类别（电影/音乐/书籍）x 状态（想看/在看/看过等）x 时间范围（近3个月/近1年/近3年/历史所有）。时间范围抓取按标记时间倒序早停，只抓范围内的新条目；历史所有走完整续抓。数据写入 `douban_{类别}_{状态}_{uid}.csv`，与 `douban_export.py` 的文件完全兼容（已抓的电影/音乐数据直接复用）。页面上有两个导出按钮：「导出本轮抓取结果到 CSV」只含刚刚这次抓取覆盖到的条目（续抓时即使新增 0 条，本轮翻到的条目也会包含在内）；「导出本地全部数据」按当前时间范围导出本地累积的全部条目。深度抓取按钮抓电影详情页的导演/类型/IMDb 编号，进度存 `movie_detail_progress.csv`，自动导入已有的 `imdb_progress.csv`（缺类型的条目会补抓一次）。可选快速（4-6 秒/条）或保守（8-15 秒/条）速度，页面显示本批预计耗时；被拦截时进度已保存，重新点击即续抓。

**② 分析 Dashboard**：选类别/状态/时间范围后一键生成。指标卡（条目数/平均评分/年份跨度/标记跨度）+ 年份分布、我的评分分布、每月标记趋势、Top 导演（电影，需深度抓取数据）、Top 类型、Top 艺术家（音乐）/作者（书籍）、高频标签。

**③ AI 总结**：基于 ② 的统计摘要（发送的是数字统计不是图片）调用大模型生成中文品味分析。侧边栏可选厂商：Anthropic (Claude)、OpenAI (GPT)、Google (Gemini)、DeepSeek、Moonshot (Kimi)，以及「其它 OpenAI 兼容接口」（自填 base URL，可接智谱、通义等兼容服务）。选好厂商后填该厂商的 API Key，模型名从下拉里选（各家列表取自官方文档，2026-08 核对：Claude 为 claude-sonnet-5 / opus-5 / fable-5 / haiku-4-5，Gemini 为 gemini-3.7-flash / 3.6-flash / 3.5-flash 等，OpenAI 为 gpt-5.6-terra / sol / luna，DeepSeek 为 deepseek-v4-flash / pro，Kimi 为 kimi-k3 / k2.6 等）。厂商发布新模型后列表会过时，选「自定义…」可手工填任意模型 ID。Key 只用于直接调用对应厂商接口，不写入任何文件。

书籍抓取为新增功能，页面结构与影音不同（subject-item 布局），解析器已按已知结构编写但未在真实页面验证过，首次抓书如果解析为空，把生成的 debug HTML 发回来即可修。

## 后续计划

拿到 `douban_movie_with_imdb.csv` 后，转换为 Letterboxd 导入格式（官方支持 CSV 导入，imdbID 可精确匹配；Rating 豆瓣 1-5 星直接对应，mark_date → WatchedDate，短评 → Review，标签 → Tags）。转换脚本待数据齐后编写。IMDb 本身无官方导入功能，仅有第三方脚本方案，风险自担。

## 更新日志

- **2026-08-23 v2.7** `douban_dashboard.py`：深度抓取新增「同时补抓旧数据的类型字段」开关，默认关闭——旧的 `imdb_progress.csv` 只有导演没有类型，此前会把这些条目全部重抓，导致待抓量从 599 膨胀到 1322；关闭时只抓从未抓过的条目。抓取前显示实际待抓量与耗时预估（区分「从未抓过」与「缺类型待补」）。速度可选快速 4-6 秒 / 保守 8-15 秒，默认快速；单批上限提到 3000。
- **2026-08-19 v2.6** `douban_export.py`：新增书籍抓取（`--type book`，默认 `all` 三类全抓），书籍收藏页为 `li.subject-item` 结构，单独解析；分页与总数正则同时兼容影音与书籍。与 Dashboard 应用的书籍解析逻辑保持一致（应用自 v2.0 起即支持书籍）。
- **2026-08-19 v2.5** `douban_dashboard.py`：模型名由手填改为按厂商提供的下拉列表，模型 ID 取自各家官方文档（2026-08 核对）；保留「自定义…」选项以应对列表过时。
- **2026-08-19 v2.4** `douban_dashboard.py`：AI 总结支持多厂商（Anthropic / OpenAI / Gemini / DeepSeek / Moonshot / 其它 OpenAI 兼容接口），按厂商切换 Key 与默认模型名；Cookie 输入增加「确认 Cookie」按钮，并校验误填 API Key（sk- / AIza 开头）或缺少 dbcl2/bid 字段的情况；导出按钮拆为「导出本轮抓取结果到 CSV」和「导出本地全部数据」，抓取完成提示同时显示本轮条数与新增条数。
- **2026-08-19 v2.3** `douban_dashboard.py`：深度抓取新增速度选择，快速模式 4-6 秒/条（默认，1323 部约 1.8 小时），保守模式 8-15 秒/条（约 4.5 小时，被风控概率更低）；界面实时显示本批预计耗时。
- **2026-08-19 v2.2** `douban_dashboard.py`：登录改为双通道。默认「账号密码」直接登录（走豆瓣移动端官方登录接口，密码不保存；触发验证码时提示改用 Cookie），「Cookie (高级)」保留并内置图文获取步骤。抓取与深度抓取统一从登录会话取连接。注意：密码登录接口未在真实环境验证过，风控严时可能始终要求验证码，Cookie 通道是兜底。
- **2026-08-19 v2.1** `douban_dashboard.py`：删除高频标签图；导演/类型分析改为全量口径，详情数据未覆盖全部条目时不出图，提示还差多少条（AI 总结同样口径）；新增电影类型横向柱状图（Top 15，带数值标签）；Top 榜柱状图统一加数值直标；深度抓取默认批量改为 500。
- **2026-08-19 v2.0.1** `douban_dashboard.py`：修复读取 v1.4 之前的旧格式 CSV（无 year 列）时分析页 KeyError 'year' 的问题，读取时自动补齐缺失列并从 intro 解析年份。已用真实数据（1323 条电影）验证全部图表和统计。
- **2026-08-19 v2.0** 新增 `douban_dashboard.py`（Streamlit 应用）与 `requirements.txt`：抓取（三类别 x 三状态 x 四时间范围，时间范围早停）、CSV 导出、分析 dashboard（年份/评分/月度趋势/Top 导演·类型·艺术家·作者·标签）、电影详情深度抓取（导演/类型/IMDb，兼容并导入 imdb_progress.csv）、Claude AI 总结模块。书籍解析器为新结构（subject-item），待真实页面验证。
- **2026-08-19 v1.4** `douban_export.py`：新增 year 列（从 intro 提取，1880-2039 范围的首个四位年份）；续抓旧格式 CSV 时自动迁移列结构并从 intro 回填 year。导演说明：列表页不含导演，由 `douban_imdb_enrich.py` 详情页阶段提供。`douban_imdb_enrich.py`：合并时详情页字段有值才覆盖，未抓到的条目保留输入 CSV 已有的值（避免把列表页提取的 year 抹空）。
- **2026-08-19 v1.3** 两个脚本：所有输出文件（CSV、进度、debug）默认生成在脚本所在目录，不再依赖运行时的当前目录；`douban_imdb_enrich.py` 的 `--input` 支持在当前目录找不到时回退到脚本目录查找。注意，早前在其他目录（如 D:\）生成的半成品 CSV 需手动移入脚本目录才能续抓。
- **2026-08-19 v1.2** `douban_export.py`：修复软限流空页面被误判为「抓取结束」的问题（实际总数 1324 只抓到 75 即停）。空页面且未达总数（或总数未知，如续抓首页即空）时判定为疑似限流，保存页面证据并等 60/120/180 秒重试最多三次；拦截检测增加对 HTML 内 sec.douban.com 跳转和「禁止访问」的识别。
- **2026-08-19 v1.1** 新增 `douban_imdb_enrich.py`：抓详情页提取 IMDb 编号、原名、年份、导演；进度逐条落盘、分批（--limit）、被拦自动重试三次后保存退出；自动生成合并文件。
- **2026-08-19 v1.0** `douban_export.py` 初版：抓「看过的电影」「听过的音乐」列表页导出 CSV，支持 Cookie、断点续抓、按 subject_id 去重、403/登录跳转/验证码识别。

## 免责声明

本项目仅用于导出与分析**使用者本人**在豆瓣上的标记数据，供个人备份与统计之用。使用者需自行遵守豆瓣的服务条款与 robots 协议，自行承担使用风险。请保持克制的抓取频率，不要用于批量采集他人数据或任何商业用途。作者不对因使用本工具产生的账号限制或其他后果负责。

## License

MIT，见 [LICENSE](LICENSE)。
