<p align="center"><b>简体中文</b> | <a href="./README.en.md">English</a> | <a href="./README.ru.md">Русский</a></p>

# ZCode Session Viewer

一个**零依赖**的本地 Web 工具,用于浏览、搜索和管理 [ZCode](https://z.ai) 的全部历史会话(含已归档)。

> ZCode 桌面端只展示当前项目的会话列表,且不方便深度检索历史内容。本工具直接读取 ZCode 的本地数据库,在浏览器里提供跨项目、全文检索、归档管理与会话内定位等能力。

## 功能

- **全量会话列表**:合并桌面索引与会话库,跨项目浏览所有会话;归档 / 置顶 / fork / 仅 CLI(子代理等内部会话)均有徽章标识
- **两种视图**:`🗂 按项目分组`(组可折叠/全部展开/全部折叠,状态记忆)+ `⏱ 按时间排序`(升序/降序)
- **归档管理**:一键「归档 / 移出归档」,直接写 ZCode 界面读取的同一字段,两端互通
- **全局搜索**:标题 / 项目 / 会话 ID / 全文内容(毫秒级 SQLite LIKE)
- **会话内搜索**:命中高亮、计数、▲▼ 循环跳转,自动展开折叠的工具/思考块
- **ZCode 风格阅读体验**:markdown 渲染(标题/代码块/表格/列表)、工具调用行(状态色点 + 参数摘要,展开看输入/输出)、思考过程折叠、系统注入内容弱化展示
- **亮暗主题 / 多语言界面**:🌙/☀️ 一键切换深浅主题;界面支持中文(默认)、English、Русский,导出的 Markdown 标签语言跟随界面选择;所有偏好本地记忆
- **会话导出**:一键导出单个会话,文件名自带类型标识(`标题_md.md` / `标题_json.json` / `标题_jsonl.jsonl` / `标题_raw.jsonl`)—— `Markdown`(直观阅读,工具调用/思考过程以折叠块呈现)、`JSON`(完整结构化数据)、`JSONL`(逐行消息,流式友好)、`RAW`(ZCode 写入库的原始记录,session/message/part 整行原样输出,零过滤零截断)
- **可调布局**:左栏宽度拖拽调整(自动记忆)
- **一键复制会话 ID**:回 ZCode 里用 `#sess_xxx` 引用即可恢复上下文
- 当前打开会话在列表中高亮标识并自动滚动定位

## 快速开始

```bash
python src/viewer.py
# 自动打开 http://127.0.0.1:8787
```

要求:Python 3.8+(仅标准库,无任何第三方依赖,无需编译安装)。

```bash
python src/viewer.py --port 9000        # 换端口
python src/viewer.py --no-open          # 不自动打开浏览器
python src/viewer.py --db-main PATH --db-tasks PATH   # 手动指定数据库路径
```

## 数据来源与隐私

本工具**完全本地运行**,不发起任何外部网络请求,不上传任何数据。

| 库(自动探测,见下) | 用途 | 访问方式 |
| --- | --- | --- |
| `<数据目录>/cli/db/db.sqlite` | 会话内容(message / part) | 只读 |
| `<数据目录>/v2/tasks-index.sqlite` | 会话列表索引(归档/置顶/项目) | 只读 + 归档字段写入 |

唯一的写操作是归档/移出归档(`UPDATE tasks SET archived = ...`),与 ZCode 界面使用同一字段,因此在浏览器里的归档操作会同步反映到 ZCode 界面(重新打开会话列表后生效)。

**数据目录自动探测**(按序尝试):环境变量 `ZCODE_HOME` → `~/.zcode`(所有平台默认布局)→ `~/.config/zcode` → `~/Library/Application Support/ZCode`(macOS 兜底)。**都没找到时,页面会自动弹出配置窗口**,填入数据目录(或高级模式里分别填两个数据库路径)保存即可——配置记忆在 `~/.zcode-session-viewer.json`,下次启动直接使用,不再询问;顶栏 ⚙ 按钮可随时重新配置。命令行参数 `--db-main` / `--db-tasks` 优先级最高。

### 平台支持

代码只用 Python 标准库(pathlib / sqlite3 / http.server),无任何平台专属 API,Windows / macOS / Linux 均可直接运行。Windows 已实测;mac 与 Linux 为理论兼容(同一 home 目录布局),如你的 ZCode 安装把数据放在别处,用启动参数指定即可。若你在这些平台验证过,欢迎提 issue 反馈。

## 目录结构

```
zcode-session-viewer/
├── src/
│   └── viewer.py      # 全部源码(单文件)
├── assets/
│   └── icon.svg       # 站点图标源文件(页面内嵌同款 SVG)
├── README.md          # 中文文档(默认)
├── README.en.md       # English docs
├── README.ru.md       # Русская документация
├── LICENSE            # MIT
└── .gitignore
```

无需构建步骤;没有任何依赖清单文件,因为依赖为零。

## 已知边界

- 归档写入与 ZCode 并发写同一行时,最多等待 3 秒(库级锁),失败会返回错误提示,不会损坏数据
- ZCode 界面对会话列表有内存缓存:在本工具里归档后,ZCode 端需重新打开会话列表才能看到变化
- 「仅 CLI」会话不在 ZCode 界面索引里,因此不提供归档按钮

## License

[MIT](./LICENSE)

## 免责声明

本项目为**非官方**社区工具,与 Z.ai 无关联亦未获其认可。它仅读取 ZCode 已存储在**你本人电脑**上的会话数据库,并对任务索引做一处归档标记写入。作者对任何数据丢失不承担责任——建议首次使用前备份 ZCode 数据目录。ZCode 及相关名称归其各自所有者所有,此处仅作标识性使用。
