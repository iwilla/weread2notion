# WeRead2Notion

将微信读书的书籍、划线和笔记同步到 Notion。

本项目使用微信读书 API Key 读取数据，并通过 GitHub Actions 定时同步到 Notion。新版不再需要复制微信读书 Cookie。

预览效果：https://malinkang.notion.site/weread2notion

> [!NOTE]
> 新版保留书籍页面、原有正文和页面评论，在独立的「微信读书摘录 · WeRead2Notion」子页面里逐条同步。不会删除旧页面、旧快照或无法确认归属的内容。首次升级建议先预览一本书，确认后再正式运行。

## 使用文档

完整教程请查看：

https://www.notionhub.app/docs/weread2notion.html

文档里包含：

- Notion 模板复制和授权
- 微信读书 API Key 获取
- GitHub Fork 和 Actions 配置
- 常见问题排查

## 关注公众号

如果你想获取后续更新，或了解更多 Notion 自动化工具，欢迎关注公众号：**Notion自动化**。

![公众号：Notion自动化](https://cdn.notionhub.app/notionhub/gzh.jpg)

## 阅读信息与旧模板兼容

自动识别中文字段以及旧模板的 Author、Categories、Rating、URL、ReadingTime、Progress、Status、Date，无需重命名。中英文字段同时存在时优先写入中文字段。BookId 和 Sort 仍为必填字段。

每次同步会读取笔记本列表及阅读信息，属性值发生变化才写入。ISBN、评分只在缺失或全面核对时查询。阅读时长写入文本（如 `2时30分`）；接口未提供时长时保留已有值；未读完不写入 1970 年完成日期。旧模板普通数字 Progress 使用 0–100，百分比格式使用 0–1；中文阅读进度沿用 0–1。接口值 1 正确表示 1%。

## 首次升级与日常使用

1. 在 Actions → **weread sync → Run workflow** 填写一本书的 `book-id`，保留默认勾选的 `dry-run`，先查看计划变更。
2. 确认后再次运行，取消 `dry-run`。程序复用原书籍页面，并建立独立同步子页面。
3. 检查原页面评论、个人笔记及划线链接，再留空 `book-id` 同步其他书籍。

旧版正文/快照没有可靠的逐条身份记录，不能确定哪些是用户修改，因此原样保留。首次迁移会在新区域生成一份可增量维护的摘录；旧内容可能与新区域重复，程序不会自动删除。已有书籍页面和链接保持不变。

手动运行默认仅预览；每日北京时间 08:00 的定时任务正常写入。Actions 使用同一 concurrency group，防止定时与手动任务重叠。本地运行时请自行避免并发。

| 选项 | 行为 |
| --- | --- |
| 默认同步 | 对比后新增/更新变化的笔记和属性 |
| `--dry-run` | 读取并预览计划，不写入属性、正文或同步记录 |
| `--properties-only` | 仅更新已有书籍属性，不创建页面、不处理正文、不推进笔记同步进度 |
| `--full-refresh` | 全面读取笔记并刷新静态属性，仍按差异写入，不删除或重建页面 |
| `--book-id ID` | 限定一本书，可与其他选项组合 |

```bash
weread2notion sync --dry-run --book-id 3300072535
weread2notion sync --book-id 3300072535
weread2notion sync --properties-only
weread2notion sync --full-refresh --dry-run
```

## 增量同步与评论保护

- 一次分页读取 Notion 书籍索引，按 BookId 复用页面。重复 BookId 会报告错误，避免选错副本。
- 根据笔记本的 Sort 和笔记数量判断是否读取笔记明细；每 7 天全面核对一次，弥补源端时间戳未变化的情况。需要立即核对时选择 `full-refresh`。
- 划线使用 bookmarkId、想法/书评使用 reviewId 对应 Notion 块；缺少稳定 ID 时停止该书，不用文本猜测身份。
- 内容无变化不写正文；源端变化且 Notion 未被手改时原位更新，保留块 ID 和附着的评论。新增内容尽量按章节和位置插入；已有块不自动移动，避免打乱个人整理。
- 仅 Notion 内容变化时保留用户版本；两端都变化时保留原块并添加「微信读书新版本」冲突副本。相同冲突副本不会重复添加，任务会提示失败，需要处理冲突后再运行。
- 接口明确返回 removed ID 时，标记「源端已删除」并保留正文。**不会根据“这次列表里没出现”推断删除**；接口没有明确删除信号的记录继续保留。
- 同步不会操作评论接口、删除页面或清理用户自定义属性；受管属性如阅读进度仍以微信读书为准。

同步记录存储在 Notion 受管块末尾的「同步记录」链接中，包含身份和内容摘要，不包含密钥；块内容与该记录在一次请求中写入。无需配置额外数据库或依赖临时文件缓存。请保留这些链接；若手工移除或删除受管块，后续核对可能将对应记录视为缺失并重新添加。

解决冲突时可先把个人修改另存到主页面，再把原同步块的正文/格式/阅读链接恢复成源端版本，保留其「同步记录」链接，随后运行全面核对。冲突副本不会自动删除。程序更新前会重新读取目标块检查并发编辑，但 Notion API 没有跨读取与写入的事务锁，建议避免在任务运行的同时编辑同一受管块。

## 划线定位链接

每条记录优先使用微信读书接口返回的官方 `deepLink`，显示「在微信读书中查看」。没有记录级链接时使用接口提供的章节链接，显示「查看所在章节」；都没有时显示「打开书籍」并使用网页版书籍链接。不会自行拼接未经验证的 App 定位协议。

链接能否唤起 App、精确定位，取决于接口提供的链接、客户端支持及登录/购书状态。电脑与手机端的实际跳转需用户实测；没有定位信息时不会宣称能精确跳到划线。

接口依据：[腾讯微信读书官方说明](https://github.com/Tencent/WeChatReading/blob/main/skills/SKILL.md)、[笔记与划线接口](https://github.com/Tencent/WeChatReading/blob/main/skills/notes.md)、[阅读进度单位](https://github.com/Tencent/WeChatReading/blob/main/skills/book.md)。

## 扩展书籍信息

现有书籍数据库可以添加下列可选属性（英文旧模板和中文名称均支持）。无需改动 BookId、Sort 或现有笔记。

| 英文属性 | 中文属性 | 类型 | 来源 |
| --- | --- | --- | --- |
| ReadingMinutes | 阅读分钟 | 数字 | 单本累计阅读秒数 / 60，可排序、求和、绘图 |
| LastRead / StartedReading | 最近阅读 / 开始阅读 | 日期 | 阅读进度回包的 updateTime / startReadingTime |
| BookmarkCount / ReviewCount / NoteCount | 划线数 / 想法数 / 笔记数 | 数字 | 笔记本列表的对应计数字段，缺失时不推算 |
| Translator / Publisher | 译者 / 出版社 | 文本 | 书籍详情 |
| PublishedDate | 出版日期 | 日期 | 书籍详情 publishTime |
| Intro | 简介 | 文本 | 书籍详情 intro，长文本自动拆分 |
| WordCount / RatingCount | 字数 / 评分人数 | 数字 | wordCount / newRatingCount，接口未返回时保留原值 |

详情属性缺失或 `--full-refresh` 时查询 `/book/info`，已填充的详情平时不反复刷新。实际回包的评分 925 转为 9.25 分，百分比格式写入 0.925；历史评分可用全面核对刷新。

同步仅维护原书籍数据库及其笔记，不再创建或更新每日统计数据库和阅读中心。可直接在书籍数据库使用最近阅读、阅读时长排行等普通表格视图，无需图表功能。

## 失败恢复与边界

阅读进度响应缺少有效进度时，会输出警告并保留已有状态、阅读进度、阅读时长和完成日期，继续同步该书的笔记及其他属性；下次运行会重新查询阅读信息。请求错误或接口要求升级仍按失败处理。

阅读时长缺失或为非正值时不覆盖已有时长，非正值会输出警告；正值不足一分钟时显示秒数，避免取整成 `0分`。已被旧运行覆盖的时长需要接口重新返回有效值或从 Notion 历史版本恢复。

阅读时长优先读取 `/book/getprogress` 实际回包中的 `book.readingTime`（秒），仅当此字段不存在时兼容官方文档所列的 `book.recordReadingTime`。2026-10-13 实测同一本书的 `readingTime=97425` 与 `/readdata/detail` 总计排行中的 `readLongest[].readTime=97425` 一致，而 `recordReadingTime=0`；因此不将两字段相加。写入文本属性 `阅读时长` 或旧模板 `ReadingTime`。启动日志显示实际目标名称和类型；异常响应会区分字段缺失、null、无效数值和非正秒数，不输出原始回包或凭据。统计接口的 `recordReadingTime` 是朗读/记录类时长；其书籍时长排行也最多只有 10 条，不能作为全量书籍数据源。

每条笔记独立保存同步基线，整本完成后才提交检查点。中断后重新运行会核对已写内容，只补未完成部分。创建请求超时时先读取远端确认结果，不立即重复创建；重复身份记录会停止该书的自动处理。读取及限流请求有有界退避重试，业务错误不反复重试。单本失败后继续处理其他书，最后以非零状态退出；服务端要求升级时立即停止。

日志统计 `new_pages`（新页面）、`property_updates`（属性更新）、`added`（新增块）、`updated`（更新块）、`unchanged`（未变）、`local_edits`（个人修改保留）、`conflicts`（冲突）、`source_deleted`（源端删除标记）、`skipped_notes`（跳过未变化书籍）及 Notion 读写请求数。`added` 包含章节标题。全面核对即使正文无变化，也会更新同步检查时间。

同步范围是微信读书 API 返回的笔记本列表，不是整个书架。仍需扫描列表与读取阅读时长，增量的重点是避免无效写入和反复导出未变化的笔记。

## 测试

```bash
pip install -e .
python -m unittest discover -s tests -v
```

测试使用内存中的 Notion 状态及 HTTP 模拟，不访问或修改真实笔记。覆盖评论和块 ID 保留、重复运行零正文写入、属性差异、冲突、删除标记、迁移保留、超时恢复、分页、预览模式及阅读进度单位。GitHub Actions 在 Python 3.11 下执行同一套回归测试。
