# workflow v3.1 本次离线修复验收

日期：2026-10-12。基线：用户提供并确认的 PLAN-v3.1 终审文本及本次会话三项歧义解释。只修改 workflow、维护测试、README 和本报告；未联网、安装依赖、调用真实 API、部署、创建资源、发送真实通知、commit 或 push。

## 历史证据与本次证据

2026-10-11 的旧报告是实现 Agent 自测，历史结果为五组通过，不能替代本次基线验收。其中“TERMINATING 静默退出”和“禁用失败合并为一次通知”的旧 PASS 不满足已确认基线，现已修复并替换断言。修复开始前核对四文件 SHA-256 均与独立审计记录一致：

| 文件 | 修复前 SHA-256 |
| --- | --- |
| .github/workflows/hunt-free-vm.yml | 012ab664b165039260c47a81d1f31dac2d4350ac44ac3ebd39d945efb54d8517 |
| tests/offline_test.py | 0cdc346ff8e3c215ea209057fcc6cd3ad4698dcfc310dc61f5eea9ab1d8c0850 |
| README.md | 5dd586ad3370e491f14d6733e467b2e0b4a8277b52acf358cfdb1ec0a98c492e |
| OFFLINE_TEST_REPORT.md | 739aed6f3453ce3b3f850d758702bf595f084e720d1c924c59a506207d7f1e06 |

## 隔离与执行入口

测试从当前 workflow 的 Python heredoc 原样提取源码并 compile/exec，不维护第二份业务实现。每个场景清空环境，只注入明显虚构的凭据，使用隔离 HOME、TMPDIR、不存在的 OCI config 路径，并把 Python tempfile 限定在该目录。生产脚本临时文件权限断言为目录 700、文件 600，退出后验证删除，原始私钥环境变量在 CLI 调用前已移除。

所有 subprocess.run 被 mock：pip、oci、GitHub curl、Telegram curl 均无真实执行。未知命令或操作会记录并在场景结束后强制失败；mock 参数断言失败也不能被生产异常处理吞掉而误记 PASS。OCI 原始输出捕获，不进入通知或 stdout；测试校验虚构敏感标记和实例 OCID 不泄露。curl 凭据从 stdin 提交，调用具有 25 秒 Python 超时及连接/总时间上限；无无限重试。

实际测试命令（在项目目录，外层使用系统标准库 Python；没有安装或下载依赖）：

```zsh
task_tmp=$(mktemp -d /private/tmp/oci-repair.XXXXXX)
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME="$task_tmp" TMPDIR="$task_tmp" /usr/bin/python3 -I tests/offline_test.py 2>&1 | sed 's|/Users/[^/]*/|$HOME/|g'
result=${pipestatus[1]}
rm -f "$task_tmp/xcrun_db"
rmdir "$task_tmp"
exit "$result"
```

外层目录清理只处理系统 Python 可能生成的 xcrun_db 并移除空目录；测试自己的临时目录由 TemporaryDirectory 清理。本次最终维护测试结果：10 个测试方法通过（包含原五组及补充分支）；退出码 0。以下记录子场景断言，不能用测试方法总数替代它们。

## 本次分支结果

次数按 launch / Telegram 发送尝试 / disable 尝试记录；“通知成功”在 mock 中必须满足 HTTP 200 且 JSON ok:true，不代表真实送达。

| 场景 | 次数 | 关键断言与结果 |
| --- | --- | --- |
| ARM 成功 | 1 / 1 / 1 | PASS：仅 ARM，204，初始通知后禁用，退出 0，无后续创建 |
| ARM 明确缺货 → AMD 成功 | 2 / 1 / 1 | PASS：ARM→AMD，两种镜像 ID 不同且各自正确；AMD 后停止 |
| ARM/AMD 全缺货 | 2 / 0 / 0 | PASS：stdout 为空、退出 0，不禁用 |
| ARM/AMD × RUNNING/PROVISIONING（四组合） | 0 / 1 / 1 | PASS：实际 shape/状态在通知内，查询一次，通知后禁用 |
| 单独 TERMINATING | 0 / 1 / 1 | PASS：实际状态/shape 通知，204，停止，不 launch |
| 初始实例查询 401 | 0 / 1 / 1 | PASS：不创建，告警后禁用，退出 1 |
| 初始实例查询 429 | 0 / 0 / 0 | PASS：静默停轮，不禁用，不 launch |
| 创建 429 | 1 / 0 / 0 | PASS：静默停止，不转 AMD |
| 创建 401/403/400 配额拒绝 | 1 / 1 / 1 | PASS：创建后不 re-list；目标查询总数 2（初始及创建前安全复查） |
| 创建超时 → 找到 PROVISIONING | 1 / 1 / 1 | PASS：只 re-list 一次，目标查询总数 3，成功停止 |
| 创建超时 → 找到 AMD TERMINATING | 1 / 1 / 1 | PASS：任一已识别非 TERMINATED 目标均按已有实例通知并停止 |
| 创建超时 → 空 / 查询 401 / 查询 429 / 连接异常 | 1 / 1 / 1 | PASS：每种结果只复查一次，退出 1，禁用，不重建、不转 AMD |
| 创建空对象 / JSON 损坏 / data 缺失 / 连接异常 / 含糊 500 | 1 / 1 / 1 | PASS：不当作成功、不盲目 fallback，复查后保守停止 |
| 初始查询 JSON 损坏 / data 缺失 / 字段缺失 / 未知状态 / 连接异常 | 0 / 1 / 1 | PASS：fail-closed，不创建 |
| 超时复查 JSON 损坏 / data 缺失 / 字段缺失 | 1 / 1 / 1 | PASS：结果未明，告警禁用，停止 |
| 禁用 HTTP 204 | 1 / 1 / 1 | PASS：只有初始成功通知，通知→禁用，退出 0 |
| 禁用 HTTP 200/202/403/500 或传输超时 | 1 / 2 / 1 | PASS：独立追加“自动禁用失败，请手动去 Actions 页 Disable workflow”，顺序通知→禁用→告警，退出 1 |
| 初始异常告警后禁用失败 | 0 / 2 / 1 | PASS：独立追加人工禁用告警，不创建 |
| 初始通知 HTTP 500 / JSON 损坏 / 传输超时 | 1 / 1 / 1 | PASS：仍尝试禁用，固定失败提示，退出 1，不创建第二台 |
| 禁用失败且追加告警超时（初始成功或也超时） | 1 / 2 / 1 | PASS：无循环或后续创建，仅固定失败提示 |
| 两种镜像发现成功 | 1 或 2 / 1 / 1 | PASS：安装→AD→两种镜像→目标查询→launch；镜像仅各发现一次 |
| ARM/AMD 镜像发现 401 或 429 / AD 发现 429 | 0 / 1 / 1 | PASS：任一失败均在初始目标查询和首次创建前告警禁用 |
| 两种显式镜像覆盖 | 2 / 1 / 1 | PASS：分别检查兼容性并使用各自镜像 ID |
| 原有 home region、subnet、存储、其他 compartment、镜像边界 | 0 / 1 / 1 | PASS：保留原有安全盘点及 fail-closed 断言 |

所有创建参数均断言：区域 ap-kulai-2、--no-retry、目标 shape/AD/subnet/compartment；ARM 2 OCPU/12GB，AMD 不传 flex 参数；启动盘 50GB、10 VPU/GB；公共 IP 请求明确 true、空 capacity reservation、无 waiter。所有 list 必须 --all，禁止 --limit、debug。

## YAML 与 shell 检查

维护测试使用顶层锚定映射检查 concurrency 精确字段/值并拒绝重复键。另实际执行系统 Ruby Psych YAML.safe_load，断言 triggers 精确为 schedule 与 workflow_dispatch、cron 为 */15、permissions 仅 actions:write、concurrency 精确为 oracle-free-vm-hunt / false、单个 bash step；把解析所得 run 传给 /bin/bash -n。结果 PASS，退出码 0；未运行 actionlint。

实际 YAML / shell 检查命令：

```sh
env -i PATH=/usr/bin:/bin /usr/bin/ruby - <<'RB'
require 'yaml'
require 'open3'
w = YAML.safe_load(File.read('.github/workflows/hunt-free-vm.yml'), aliases: false)
t = w['on'] || w[true]
raise unless t.keys.sort == ['schedule', 'workflow_dispatch'] && t['schedule'] == [{'cron'=>'*/15 * * * *'}]
raise unless w['permissions'] == {'actions'=>'write'}
raise unless w['concurrency'] == {'group'=>'oracle-free-vm-hunt', 'cancel-in-progress'=>false}
steps = w.fetch('jobs').fetch('hunt').fetch('steps')
raise unless steps.size == 1 && steps[0]['shell'] == 'bash'
_, err, status = Open3.capture3('/bin/bash', '-n', stdin_data: steps[0].fetch('run'))
raise 'Shell syntax failed' unless status.success? && err.empty?
puts 'YAML exact triggers/permissions/concurrency and bash syntax: PASS'
RB
```

## 保留偏离与覆盖限制

本轮按用户指示保留：固定 OCI CLI 版本且未缓存 ~/.local；使用环境 API-key 认证和临时文件替代 ~/.oci/config；额外 tenancy/存储/subnet 盘点及 IAM 需求；区域内候选 AD 逻辑；Ubuntu 24.04 Minimal 支持、镜像严格校验和覆盖兼容性检查；固定 10 VPU/GB、公网 IP 请求、运行预算及其他保守拒绝分类。没有新增盘点、状态文件、自动保活、新依赖或重试。故不声称逐项完全等同原文。

离线只证明已覆盖的脚本控制流和参数，未验证 OCI SDK 真实分页/错误格式、IAM、CLI 环境认证和服务兼容性、GitHub 调度与仓库外并发、Telegram 真实送达、SSH、最终实例状态或账单。强制终止 runner 或基础设施故障可能阻止仲裁/禁用/通知，需人工禁用并核实资源；脚本不能撤销已被接受的创建请求。

四项真实环境验证仍全部为“未验证”：实际 2C/12GB 配额语义；ap-kulai-2 的 --shape 镜像返回；GITHUB_TOKEN + actions:write 的真实禁用效果；首次手动端到端。没有真实抢建成功或零费用结论。

## 独立复核

修复完成后，未参与修改的独立审查者 repair_review 只读复核四文件、修复前会话记录、关键控制流和测试证据；没有 Git 基线快照，不把文本变化比较声称为 Git diff。审查者独立重跑维护测试：10 方法、0.193s、退出 0；独立执行 Ruby 精确 YAML 结构检查及 bash -n：退出 0。独立审查最终结论：本轮指定修复及离线验收通过，未发现阻塞项或本轮引入的高/中风险回归，与实现者结论无分歧；审查者未修改任何文件。测试隔离、未知调用拒绝及生产脚本一致性均已核实。
