# data-qd-126 · 随机实验效果分析

Binary randomized experiment effect estimation with exact count audits。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/experiment-lift --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

## 大型快照

加 `--large-snapshot` 审计最多 1 GiB、每表 1,000,000 行的快照：

```sh
.venv/bin/experiment-lift --source big.json --output run/large.json --large-snapshot
```

字段顺序任意、结果表可先于分组表；输入校验、统计语义和报告与默认模式逐字段一致。
解析按块增量进行，行数据写入输出目录旁的 SQLite 临时库做外存对账，内存不随用户数
增长；临时库在成功或失败后自动删除，因此输出目录需有与快照相当的可用磁盘。
失败仍退出2并保留旧报告。默认模式（8 MB / 每表 20000 行）行为不变；
百万行测试在 Linux 子进程 128 MiB 地址空间限制下运行。

src/experiment_lift/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
large.py 提供大快照的流式解析与外存对账，ledger.py 提供 SQLite 分组账本，cli.py 衔接数据链路。fixtures 为明确自制样例，
代码与样例采用MIT许可。

## 分组账本

同一 CLI 增加基于 SQLite 的分组账本，原有 `--source/--output` 入口不变：

```sh
.venv/bin/experiment-lift init --ledger run/ledger.db --control C --treatment T \
  --numerator 1 --denominator 3 --seed trial-2026
.venv/bin/experiment-lift assign --ledger run/ledger.db --source batch.json --output run/assignments.json
.venv/bin/experiment-lift export --ledger run/ledger.db --output run/assignments.json
```

`init` 固定 control/treatment、整数比例 p（0<p<1）和 seed，标识校验沿用既有规则，
账本已存在时拒绝。`assign` 读 `{"batch_id": ..., "users": [...]}`（至多 20000 个
用户），提交即导出：批内唯一、跨批重复拒绝；同批号同用户集合重试幂等，换集合拒绝；
任何冲突整批回滚。每次提交后处理组人数恰为 `floor(累计人数*p)`，旧分组不变；新名额
按 seed 与 user 的无歧义编码 SHA256 排序分配（同值按 user）。写事务内计算配额与校验
身份，锁冲突可退出 2。`export` 按 user 排序流式输出，字段可直接追加 `outcomes` 供
原分析入口使用；提交后导出失败可仅从账本恢复，不重新分组或累加；百万用户导出在
Linux 子进程 128 MiB 地址空间限制下通过。细节见[数据契约](docs/contract.md)。

## 业务与样例结果

C组1/4、T组2/4；风险差1/4、风险比2、相对lift1。缺失结果拒绝，控制率0的比值明确null。
