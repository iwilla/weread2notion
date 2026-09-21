# WeRead2Notion

将微信读书的书籍、划线和笔记同步到 Notion。

本项目使用微信读书 API Key 读取数据，并通过 GitHub Actions 定时同步到 Notion。新版不再需要复制微信读书 Cookie。

预览效果：https://malinkang.notion.site/weread2notion

> [!WARNING]
> WeRead2Notion 会在检测到书籍笔记更新时删除原来的同步页面，然后重新写入微信读书数据。请不要在同步生成的 Notion 书籍页面里添加自己的笔记、批注或其他重要内容，否则下次同步时可能会被删除且无法恢复。

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

每次正常同步都会刷新微信读书笔记本列表中已有书籍的属性，即使笔记没有变化。逐本比较 Sort，避免其他书籍的较新时间导致漏同步。阅读时长写入文本（如 `2时30分`）；接口未提供时长时保留已有值。未读完时不写入完成日期。旧模板普通数字 Progress 使用 0–100，百分比格式使用 0–1；中文阅读进度沿用 0–1。

### 补齐已有书籍的属性

在 GitHub Actions 的 **weread sync → Run workflow** 中勾选“只补齐书籍属性，保留笔记正文”。此模式更新已有书籍的作者、分类、评分、阅读时长、进度等属性，不创建或删除页面、不修改笔记正文，也不推进 Sort。尚未存在的书籍会跳过。同步范围是微信读书 API 返回的笔记本列表，不是整个书架。

本地命令：`weread2notion sync --properties-only`。

普通同步遇到笔记更新时仍会重建对应页面，保留上方的数据覆盖警告。Actions 禁止两个同步任务并发运行。日志末尾显示笔记同步、属性刷新及跳过数量。

运行回归测试：`python -m unittest discover -s tests -v`（需先 `pip install -e .`）。
