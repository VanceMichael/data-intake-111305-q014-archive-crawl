# 来源协议

## 记录与身份

默认来源入口为 `/feed`，媒体格式为 `json`。来源身份字段是 `directory`、`export`、`sequence`。这些字段表示来源自身声明，不等同于已经解决的业务实体身份。输入顺序是一次接收的观察顺序，并不保证业务修订有序。

`/observations/1` 等地址允许逐条读取合成观察，返回 `items` 数组。正文、页内嵌入数据与CSV均保留供方原文，不在协议层推断当前有效值。网页中的 `source-records` 是门户向下载客户端提供的公开数据块；表单还带有隐藏控件，令牌用于本地协议状态检查。

## 传输响应

`/feed` 有 ETag `feed-1`，可发送 `If-None-Match` 得到304。`/rate-limited` 返回429和两秒的Retry-After；`/unstable` 首次503，其后200；`/truncated` 声明长度大于实际传送长度并断开。`/range` 支持单段字节范围及416，`/range-ignored` 故意返回整份200，不能假定所有供方都会履行范围请求。

每次独立启动来源都有独立计数器。一个路由在 `origin.json` 中若是响应数组，依请求次序前进，最后一个响应保持不变。可通过编辑一份场景副本再用 `serve --scenario 文件` 启动不同交错序列。

## 表单回执

演练门户只接受本地POST `/portal/submit`，表单字段为 `id`、`csrf`，当前令牌由场景的 `portal.csrf` 声明。过时令牌409，缺少身份422。添加 `lose_response=yes` 会先受理再断开连接；随后GET `/portal/status?id=标识` 可看到真实回执。门户不提供提交幂等键，接收程序默认不执行提交。该端点用于受理确认和供应商核对的本地演练，不连接任何真实门户。

## 领域样例

- `rename_reuse`：旧DN后来被另一个entryUUID使用；业务侧关注区分实体与当前目录地址。
- `change_gap`：11后先收到13；业务侧关注缺少12时不发布伪连续目录。
- `attribute_operations`：多值属性增删及更名交错；业务侧关注原文、作用范围和演进原因可追查。

另外提供 `/exports/full.ldif`、`/exports/rename.ldif`、`/exports/modify.ldif`。响应内容和版本由场景文件逐项声明，便于核对实际收到的来源资料。

## 领域接入契约

LDIF语法参考RFC 2849。目录、导出编号和sequence为本项目交付清单字段，不是LDIF标准字段；全量声明已含至last_change_sequence，后续从下一序号接续。只读导出，不连接LDAP服务器、不改账户。所有外部值URL只能成为受控待领取依据，演练不访问外网。

### 交付清单字段

| 字段 | 含义 | 必填 |
| --- | --- | --- |
| `directory` | 目录或联盟成员，接续边界按此隔离 | 是 |
| `export` | 供方导出批次标识，重复导出据此识别 | 是 |
| `sequence` | 变更流序号；全量导出为交付批次号 | 是 |
| `kind` | `snapshot` 或 `changes` | 是 |
| `path` | 该批次的下载路径，相对来源根 | 是 |
| `last_change_sequence` | 全量导出已含至的变更序号，仅 `snapshot` 使用 | 全量必填 |

`sequence` 与 `last_change_sequence` 不是同一概念：前者标识交付批次，后者声明全量的接续边界。变更文件必须从基线边界后的下一序号连续到达；缺段时允许继续收件，但不会发布当前目录。

## LDIF 读取口径

- 物理折行在解释值之前先还原，折行后的 base64 与外部值引用按一个值处理。
- `attr::` 的 base64 值与 `attr:` 的明文字节都可读，但保留原编码，不合并成同一种字符串。
- `attr:< URL` 只登记为外部引用，永不访问该地址；引用不是属性值。
- 属性选项保留为独立形式：`description` 与 `description;lang-zh` 是不同属性描述。
- 同名多值属性按文件顺序全部保留，`add`/`delete`/`replace` 分别按集合语义处理。
- `changetype: modify` 的各操作按文件顺序执行，顺序不同结果不同；`add` 不带值表示建属性，`delete` 不带值表示删属性，`replace` 不带值表示删属性。
- `changetype: modrdn` 保留 `newrdn`、`deleteoldrdn`、`newsuperior`；`deleteoldrdn: 0` 表示新旧命名属性都保留。
- `entryUUID` 是实体身份；DN 是可复用、可变更的地址。修改 `entryUUID` 会被拒绝并登记为待补证。

## 目录发布条件

`current_catalog()` 默认要求同时满足：

1. 已应用一份全量基线；
2. 变更序号从基线边界起连续，没有缺口；
3. 所有记录都已解释成功，没有待补证记录。

任一条件不满足时抛出 `ContinuityError`，返回上次一致状态供调查，而不是生成一份基线拼错的当前目录。需要检视中间状态时可显式 `require_complete=False`。
