# A2A 协作图

[返回首页](../../README.md) · [接入说明与代码依据](../a2a-integration.md)

图类型为 **sequence**，展示 AI Helpdesk 向 AI SQL Agent 委派业务查询时的 A2A 流式成功路径。默认 `/query` 路径、`send` 回退和失败行为在接入说明中分别解释。

| 文件 | 用途 |
| --- | --- |
| [SVG](a2a-sequence.zh-CN.svg) | GitHub README 内嵌静态图，支持深浅主题 |
| [HTML](a2a-sequence.zh-CN.html) | 下载后用浏览器打开的独立交互图 |
| [JSON](a2a-sequence.zh-CN.archify.json) | Archify 可编辑源文件 |
| [验证摘要](a2a-sequence.receipt.json) | 文件哈希、9 项检查与浏览器尺寸记录 |
| [Archify 许可证](ARCHIFY-LICENSE.txt) | 随导出的运行时保留上游 MIT 许可证 |

## 英文版

默认英文 README 使用英文图，中文版使用上方的中文图；两版保持相同节点、消息和布局，只翻译展示文案。

[English SVG](a2a-sequence.en.svg) · [Interactive HTML](a2a-sequence.en.html) · [Editable JSON](a2a-sequence.en.archify.json) · [Validation receipt](a2a-sequence.en.receipt.json)

英文版同样通过 showcase 9/9、四种桌面尺寸检查与深浅主题视觉检查。复现时将下方命令中的 `zh-CN` 替换为 `en`。

## 生成与复现

生成工具：[Archify](https://github.com/tt-a1i/archify)，固定提交 `5289f6867f048a7450ec5718f58459613a84cf41`（包版本 `2.17.0-dev.1`）。工具只用于文档生成，不是两个应用的运行依赖；本次未安装全局 skill。

准备该提交后，在本仓库根目录执行，替换示例中的工具路径：

```bash
ARCHIFY_CLI=/path/to/archify/archify/bin/archify.mjs
node "$ARCHIFY_CLI" validate sequence docs/diagrams/a2a-sequence.zh-CN.archify.json --quality showcase --json
node "$ARCHIFY_CLI" deliver sequence docs/diagrams/a2a-sequence.zh-CN.archify.json docs/diagrams/a2a-sequence.zh-CN.html --quality showcase --json
node "$ARCHIFY_CLI" visual-check docs/diagrams/a2a-sequence.zh-CN.html --json
```

在交互 HTML 的“导出”菜单选择 SVG 可重新生成静态图。本次使用同一原生导出入口 `Archify.exportMenu.run('svg')`，没有另画一份近似图。

## 验证记录

- **产物验证：** showcase 9/9；composition 0 errors、0 warnings。
- **浏览器验证：** 1440×900、1600×1000、1920×1080、2048×1320 均无页面横向或纵向溢出。
- **视觉检查：** 已查看 1440×900 浅色与 2048×1320 深色截图，节点、消息、图例均完整且无文字重叠。
- **语义依据：** [接入说明](../a2a-integration.md)链接到固定提交的客户端、服务端、信任判定及超时逻辑。

确定性产物检查、浏览器尺寸检查、视觉检查分别记录，均不代表生产链路性能或查询准确率已通过验收。完整哈希见 [receipt](a2a-sequence.receipt.json)。
