# Binary experiment audit v1

面向实验分析人员，接收唯一用户的随机分组快照与显式0/1转化结果，输出两组转化率、
差值/比值和人数分配诊断。输入control/treatment不同、expected_treatment_share为整数分子分母且0<p<1，
assignments(user,variant)和outcomes(user,converted)各最多20000行。user每表唯一，
全部分组用户必须有一个显式0或1结果；缺失不补0，未知分组/结果用户拒绝。两组都需至少1人。

以分组用户为分析单位；转化率、风险差T-C、risk ratio T/C和relative lift T/C-1精确Fraction文本。
控制组率0时比值和lift为null，不加伪计数；差值仍有效。每组给Wilson 95%近似区间，
差值给未合并正态近似区间和标准误，区间不强行裁到[-1,1]。小样本/全0时正态差值区间可能
退化或覆盖差，明确不作检验结论、显著性、自动停止或因果真实性认证。

按给定期望分配比例计算Pearson人数卡方统计量和期望人数，不计算p值或SRM通过标记。
数据假定一次随机分组、独立用户、固定观察窗口且结果完整；不实现序贯检验、聚类修正、
多重比较、事后筛选或未知曝光补齐。样例C1/4、T2/4，差1/4、比2、lift1、分配统计0。
statistics.py与model.py分别实现统计和数据身份链路；原创合成数据，不来自线上实验。

## 大型快照模式（--large-snapshot）

默认模式仍为上文所述的 8 MB / 每表 20000 行契约，行为不变。`--large-snapshot`
处理同一份输入契约下的大快照，字段、校验、统计语义和报告结构（schema_version、
source_sha256、result 及其全部字段与文本格式）与默认模式完全一致：

- 输入为 UTF-8 JSON 对象，字段顺序任意；`outcomes` 可出现在 `assignments` 之前，
  两组记录内部也允许任意顺序。仍拒绝重复 JSON 键、未知/缺失字段、非有限数值、
  非 UTF-8 字节和结束符后的多余数据。
- `assignments` 与 `outcomes` 各最多 1,000,000 行，文件最多 1 GiB（按原始字节计）。
- 保留全部既有规则：user 标识非空、去首尾空白后不变、长度≤128、无控制字符；
  variant 必须等于 control 或 treatment；每表 user 唯一；结果用户必须已分组；
  每个分组用户必须有显式 0 或 1 结果（缺失不补 0）；control≠treatment；期望份额为
  整数分子分母且 0<p<1；两组各至少 1 人。
- `source_sha256` 覆盖完整源文件的原始字节（含转义形式、空白与末尾换行），
  与解析结果无关。

实现上按 256 KiB 块增量解码 UTF-8 并逐条产出 JSON 值，跨块的多字节字符和转义由
增量解码器延续；行数据批量写入输出目录旁的私有 SQLite 临时库，以 user 主键完成
去重与分配/结果对账，只在内存保留两组计数与滚动 SHA256，因此不整体载入文件或用户
集合。临时库在成功或失败返回前删除；失败（校验错误、I/O 错误、资源限制）退出 2，
不写新报告，调用方保留旧报告。输出路径的符号链接拒绝沿用默认模式。
临时库需要与快照规模相当的本地磁盘空间，单操作者串行使用。

## 分组账本（init / assign / export）

同一 CLI 增加基于 SQLite 的分组账本；原有 `--source/--output` 入口与行为不变。
账本把分组身份、处理组概率 p 和随机种子固定在 `init`，每次 `assign` 只向新用户
发放剩余名额，已提交用户的 variant 永不改写。

### init

```sh
experiment-lift init --ledger run/ledger.db --control C --treatment T \
  --numerator 1 --denominator 3 --seed trial-2026
```

- control、treatment、seed 均按既有标识规则校验：非空字符串、去首尾空白后不变、
  长度 ≤128、无控制字符；control 与 treatment 必须不同。
- `--numerator/--denominator` 为整数，必须 0 < numerator < denominator，
  即 0<p<1；账本内保存原始分子分母，不折算为浮点。
- `--ledger` 已存在（含符号链接）时拒绝；初始化在私有临时库完成后再原子链接到
  目标路径，失败不会留下可用账本，也不会覆盖已有文件。

### assign

```sh
experiment-lift assign --ledger run/ledger.db --source batch.json --output run/assignments.json
```

`--source` 为严格 JSON 对象，只允许 `batch_id` 和 `users` 两个字段：

```json
{"batch_id":"2026-11-15-b001","users":["u-1001","u-1002","u-1003"]}
```

- 拒绝重复 JSON 键、非有限数值、未知/缺失字段、非 UTF-8 字节和结束符后的多余
  数据；`batch_id` 是标识；`users` 为 1 至 20000 个标识，批内必须互不相同。
- 每个用户在所有已提交批次中至多出现一次；跨批重复导致整批拒绝。
- 同一 `batch_id` 以同一用户集合（顺序任意）重试是幂等空操作，不新增行、不重新
  分组；同一 `batch_id` 换用户集合被拒绝。
- 任何冲突（批内重复、跨批重复、批号换集合、配额不变量异常）都在单一事务内整批
  回滚：不记录批次、不消耗名额、不改动已提交用户。
- 提交成功后立即把整个账本导出到 `--output`；`--source`、`--ledger` 与 `--output`
  不得相同。若提交成功但导出失败，命令退出 2，批次仍保留在账本中；重跑相同
  `assign`（幂等）或执行 `export` 即可恢复，不会重新分组或累加人数。

### 名额与确定性

设提交后账本累计人数为 N，处理组人数必须恰为 `floor(N*p)`；批次只在新用户中
分配本次新增名额（`floor(N*p) - 已用名额`），已提交用户的 variant 不变，因此每次
提交都满足配额，且各批次之间的用户集合保持只增不改。

新增名额的排序键为固定、无歧义编码的 SHA256：

```text
SHA256(b"experiment-lift/assign/v1\0" + seed + b"\0" + user)
```

seed 与 user 按 UTF-8 编码（JSON 转义形式不影响结果）；NUL 分隔符和固定域前缀
保证不同 (seed,user) 对不会拼出相同字节串。键相同时按 user 的 UTF-8 字节序升序。
排序只作用于本批新用户：新增名额取该批排序键最小的若干用户，因此单个用户得到的
variant 除 seed/user 外还取决于其所在批次及此前累计人数；已提交用户不参与重排。
同一批次内该结果是确定性的，与批内输入顺序无关。

### 并发与锁

`assign` 先取 SQLite 写锁（`BEGIN IMMEDIATE`）再读取累计人数、批号历史并写入，
因此配额计算与身份校验在写事务内串行化；提交前再次核对累计人数和处理组人数。
并发写者按锁顺序逐个提交，后到者看到前一提交后的状态。等待超过 10 秒仍取不到
写锁时以“锁冲突”退出 2；调用方可以重试同一批次，幂等性保证不重复计数。
`export` 在只读事务内取一致快照，可与写者并发。

### export

```sh
experiment-lift export --ledger run/ledger.db --output run/assignments.json
```

输出为可独立读取的 JSON，字段恰为原分析入口所需：按 user 排序的
`assignments(user,variant)`、`control`、`treatment`、`expected_treatment_share`。
在同一对象中追加 `outcomes(user,converted)` 后可直接作为原 `--source` 输入使用，
统计数据链路不变；超过默认模式 8 MB / 20000 行的导出结果按同一字段契约改用
`--large-snapshot` 分析。

导出按 user 的 UTF-8 字节序（即账本主键的顺序）流式写出、逐块落盘，内存不随用户数
增长；连续写出百万用户输出在 Linux 子进程 128 MiB 地址空间限制下通过。输出经临时文件后原子替换，失败保留旧
文件；输出符号链接拒绝，输入/账本与输出不得相同。账本名为 `.db` 的 SQLite 文件
（WAL 模式），单机本地使用；数据库自带事务保证“提交后导出失败”可从账本恢复。
