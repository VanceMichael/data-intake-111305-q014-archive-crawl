# 馆藏机构 LDIF 导出的重命名与变更接收

接收程序请求来源接口，保留响应状态、头部、原始内容与每次观察的时间，并把收到的 LDIF 导出解释成可查询的机构演进记录。原始采集与业务解释彼此独立：来源原文永远保留，业务版本只追加，不覆盖旧 DN 或旧属性。

## 它能回答的问题

- 当前某机构在目录中的地址是什么，`entryUUID` 指向哪个实体；
- 某个 DN 曾经属于谁、何时被另一个实体重新使用；
- 某机构在某次变更之后的完整属性，以及每一步变更来自哪份文件、哪一行、哪次 HTTP 接收；
- 哪些机构关系仍待补证（外部值引用、无法解释的记录、尚未接续的变更段）。

## 运行环境

使用 Python 3.11 及以上，业务运行依赖均为标准库；SQLite 由 Python 自带。时区资料使用运行环境内的系统数据库。运行过程不安装依赖，也不下载数据。本地来源服务器和接收程序在同一进程环境中即可运行。

构建工具及其传递依赖精确锁定在 `requirements-build.txt`。首次准备开发环境时先执行下面命令；之后测试、构建和演示均可离线运行，不在运行命令中安装包。

```sh
python3 -m pip install -r requirements-build.txt
```

```sh
python3 -m unittest discover -s tests -v
python3 -m build --wheel --no-isolation
python3 -m compileall -q archive_crawl tests
python3 -m archive_crawl demo
```

演示从空的临时目录启动本地来源，真实完成 HTTP 获取、归档与 LDIF 接入，并输出当前目录状态。默认场景故意在 11 后交付 13，演示会显示第 12 段缺失、第 13 份被暂存、当前目录拒绝发布，以及待领取的外部引用。结束自动清理。

## 常驻来源与接收

```sh
python3 -m archive_crawl serve
```

终端会输出本次分配的回环地址。在另一终端将下面地址替换为实际地址：

```sh
python3 -m archive_crawl capture --base-url http://127.0.0.1:实际端口 --archive .runtime/raw.sqlite3 --source local-source --path /feed
```

完整的目录接入会读取交付清单、逐份下载并解释：

```sh
python3 -m archive_crawl intake --base-url http://127.0.0.1:实际端口 --archive .runtime/archive.sqlite3 --directory union-a
```

`--export 名称` 可重复使用，只接入清单中的指定批次。归档库只追加来源观察；重复导出会记为 `duplicate`，不会再次应用。`archive.py` 管理原始内容及完整性，`client.py` 执行一次请求，`decoding.py` 负责来源格式解码，`ldif.py` 读取 RFC 2849 文件，`catalog.py` 负责文件接入、条目演进和版本查询，`origin.py` 提供协议演练。

## 设计要点

### 文件级接收

每份导出都登记为一行 `intake_files`，关联原始 `captures` 记录与摘要，并带接收时的状态：

| 状态 | 含义 |
| --- | --- |
| `applied` | 已解释并纳入基线或变更流 |
| `blocked` | 变更段暂时不连续，文件保留等待 |
| `baseline_required` | 尚无全量基线，文件保留等待 |
| `duplicate` | 同批次同内容重复到达，未再次应用 |
| `conflict` | 批次标识被不同内容复用，或序号被另一批次占用 |
| `superseded` | 已被更新基线或已应用序号覆盖 |
| `invalid` | LDIF 无法解析，原文仍保留 |

每条 LDIF 记录另存一行 `intake_records`，保留文件内顺序、原始行号和原始文本。记录解释失败时整条回滚，只留下待补证标记，不会产生半个版本。

### 条目演进

- 实体身份是 `entryUUID`，地址是 DN。DN 变更写入 `dn_bindings`，旧地址以 `active=0` 关闭，不覆盖。
- 每次变更追加一行 `entity_versions`，记录版本号、DN、属性、原因和来源记录。原因包括 `snapshot`、`add`、`modify`、`modrdn`、`ancestor_rename`、`reference_update`、`delete`、`absent_from_baseline`。
- `modify` 的增删与 `replace` 严格按文件顺序执行；`delete` 指向不存在的属性或值会被视为无法解释，不静默忽略。
- `modrdn` 同时更新子条目地址，并按已知 DN 型属性（`member` 等）改写指向被改名条目及其子树的引用。外部值引用不改写，只登记。
- 同一 DN 被新实体使用时，旧绑定保留为历史，新绑定成为当前地址。
- 更名链缺前驱时，该条记录登记为待补证；它不会凭空创建实体。

### 可查询版本

`Catalog.current_catalog()` 发布当前目录；`Catalog.as_of()` 按变更序号回看；`Catalog.entity()` 返回一个实体的全部版本、地址历史和外部引用；`Catalog.dn_history()` 返回某地址的全部持有者；`Catalog.record_source()` 从记录回到文件、捕获与行号。

### 接续边界

全量导出声明 `last_change_sequence`，变更文件从下一序号连续接续。缺失一段时：

- 后续变更文件仍被收下并暂存为 `blocked`，不丢弃；
- `status()` 报告缺口序号与暂存批次；
- `current_catalog()` 默认拒绝发布，因为此时拼出的“当前目录”基线是错的；
- 补上缺口后，暂存文件按序自动应用。

`current_catalog(require_complete=False)` 可返回最后一次一致状态，供档案员调查，而不是发布。

### 待补证关系

`pending()` 汇总三类未决事项：外部值引用、无法解释的记录、被阻塞的导出。它们与正常目录分开登记，补证后引用状态从 `pending` 变为 `withdrawn` 或随实体删除关闭，不会混入有效属性值。

## 开发资料

来源协议与 LDIF 口径见 `docs/source-protocol.md`。`fixtures/origin.json` 是合成交付清单与导出内容，`fixtures/business_cases.json` 保存业务侧关注的交错情况。测试覆盖折行、base64、外部引用、属性选项、多值属性、修改顺序、更名与子树、DN 复用、重复导出、缺段接续和待补证。所有条目、机构、文件内容和门户回执均为本地合成资料，不包含真实用户身份。

## Wheel 分发

构建产物位于 `dist/`，包含 `archive_crawl` 和命名空间内的合成来源 JSON。安装 wheel 后，即使当前目录不是源码目录，也可执行 `python3 -m archive_crawl demo`。提供 `serve --scenario` 时仍以指定文件为准，找不到指定文件会直接报错，不回退到默认资料。
