# SDK 单机方案离线验证与发布审查材料

日期：2026-10-15（Asia/Shanghai）。结论范围：本地静态检查与 mock 验收，尚无真实 OCI/GitHub/Telegram 验收。只改四个指定文件；没有 Git 元数据，未 init/clone/commit/push，未读取真实 OCI 配置、Repository Secrets 值或原始敏感日志，未启用/dispatch workflow，未访问 OCI 或发送真实通知。

远程只读核对（2026-10-15，GitHub 只读接口，不含任何凭证值）：默认分支 `main` 头部 `d0d9f5e6f61dc128cdbfc1f4a98a1e32572edc61`（2026-10-12T22:40:37Z，父提交 `ea78ee80…`）；其上 `.github/workflows/hunt-free-vm.yml` blob 为 `26c578aef1f4092925ce38e0231ff29d35906dc9`（27,594 字节），等于本地迁移前基线的 `git hash-object`，即远程仍是旧 CLI 3.94.2 版（含 `cron: '*/15 * * * *'`、`lifecycle-state` 连字符参数、`OCI_CLI_REGION`），与本版 SDK 工作流 blob `713cde4c`、405 行不同。hunt 运行记录 5 条，全部 `workflow_dispatch`、全部 `conclusion: failure`，末次 2026-10-12T22:41:44Z；当时无 queued、无 in_progress、无 schedule 触发。该快照不等于 hunt 已禁用，也不覆盖 `create-vm.yml`。`GET /actions/workflows` 被只读工具拒绝（400 not an allowed endpoint），因此 hunt-free-vm.yml 与 create-vm.yml 的启用/禁用状态都未知，不得假定。

同仓另有遗留 workflow `.github/workflows/create-vm.yml`：cron `*/5 * * * *`、region `ap-singapore-1`、shape `VM.Standard.A1.Flex` 4 OCPU/24 GB、启动盘 200 GB、`continue-on-error: true`，把 Secrets 写入 runner 的 `~/.oci/config` 并把原始 OCI 输出 POST 到 Discord webhook。它不属于免费档目标，且存在凭证落盘与原始输出外发两个不可接受面。启用状态未知，其 cron 在发布前仍可能每 5 分钟触发；hunt 的 5 条运行记录不能证明它没在跑，更不能证明仓库里没有竞争。处理不能等到覆盖 hunt 的那一刻才开始；发布本版时必须同时停用或删除，不得由本项目重新启用。

## 可行性与最小迁移

复用原有通知、自动禁用、实例与存储盘点、镜像校验、Secrets 和 GitHub concurrency。将 CLI 子进程/JSON/空 stdout 解释改为官方 Python SDK 2.187.2 方法、Response.data 和模型；SDK `to_dict` 使用 snake_case，已按真实契约迁移。没有 CLI fallback、数据库、服务、额外 workflow 或跨 AD 创建循环。已有隔离临时环境可复用，无新增本地依赖。此选择消除对 CLI 展示层的依赖，不能证明历史 `block_volumes/stdout_empty` 根因。

当前锁 true、无 cron，只保留手动入口；未发布。hunt 运行快照当时无排队/进行中/schedule，不等于已禁用，也不覆盖 create-vm；两个 workflow 的启用/禁用状态都未知。后续须先通过新授权、单次真实验收，才能加入约 15 分钟 cron。

## 实际执行和证据边界

执行：`/tmp/oci-boundary-20261013-sy0k0dzy/venv/bin/python -I tests/offline_test.py`。真实 SDK 2.187.2 与已有 PyYAML；**最终28 个 unittest 测试组，0 失败、0 错误**。直接提取实际 workflow heredoc 执行，不保留第二份生产逻辑。生产 OCI 调用经过真实 SDK 客户端方法、参数校验、NoneRetryStrategy 及真实模型，mock 位于 BaseClient.call_api；另一个用例经过真实 call_api/request 层，断言真实 wire JSON 中的 50 GB、10 VPU/GB、2 OCPU/12 GB、公网 IP 字段，以及响应头错误只有一次 HTTP 尝试。

每个生产入口用例清空环境、隔离 HOME，Signer 只接收虚构值，pip/HTTP 全 mock，config.from_file 和底层网络另被禁止；虚构敏感标记不许进入日志和通知，HOME 结束为空。正常 NoneRetryStrategy 的调用路径被观测，SDK 独立 header parser 重试设为零并单独验证。初次测试 fixture 的 HTTP 模块路径和 SDK endpoint 模板断言不符合本地 2.187.2，已修正；未为此安装依赖、扩建测试框架或连接服务。

| 测试组 | 验证范围 |
| --- | --- |
| 01 | YAML 锁、无 cron、actions:write、固定 concurrency、无 CLI/凭证文件路径 |
| 02 | ARM/AMD 的 RUNNING/PROVISIONING/STARTING/STOPPING/STOPPED/MOVING/TERMINATING，全部禁止 launch |
| 03–06 | ARM 成功、明确缺货才 AMD、双方缺货、launch 429；只有一台被接受，单一 AD |
| 07–11 | 认证/权限/配额/配置拒绝；超时后找到、为空、复查失败；坏创建响应，均不盲目二次创建 |
| 12–15 | 所有查询失败关闭；实例后页命中；实例/compartments/卷/镜像后页 403；坏集合和重复 token |
| 16–18 | root/其他 compartment 有 A1/E2 禁止创建；存储150/151GB边界；ARM缺货后重盘点残留卷 |
| 19–20 | 错架构/版本/自定义/过大镜像拒绝；镜像覆盖经官方兼容 API |
| 21–23 | 通知失败仍禁用；禁用非204追加人工告警；锁true/空/非false/缺失及单次diagnostic均无写调用 |
| 24–27 | 模糊500不转AMD；未知状态、非home、公网子网禁止关闭；真实SDK wire格式/独立header重试 |
| 28 | 普通查询429：8类查询分别验证静默退出0、零创建、零通知、零禁用；组10单独验证超时仲裁复查429告警禁用、不重建 |

锁下读操作可运行，但没有创建、Telegram 或 GitHub 禁用。所有离线成功均不证明真实 IAM、容量、免费资格、网络、消息送达、自动禁用或 SSH 可用。SDK 真正签名未用真实密钥验证，Ubuntu runner 上 pip 安装未实际执行；官方接口调用及响应由 mock 提供。HTTP强制中断/runner强杀不在自动恢复保证内。

## 独立审查

未参与实现的独立上下文（GPT-6.1 high）已只读审查四文件、原始基线与官方 SDK 2.187.2 本地源码，并独立复跑全部 27 组测试，0 失败、0 错误。未发现 P0/P1 代码问题；确认 SDK 参数、真实序列化、显式禁用两层重试、分页失败关闭、全 compartment 盘点、不确定创建只复查一次、明确缺货才 AMD、通知失败仍禁用与禁用失败追加告警在当时审查范围内未发现阻塞；随后主控核对发现普通查询429偏离用户基线，已按下述专项修正收口。

发现并完成一轮必要文档修正：P2——原先“真实验收正常后启用 cron”可能包含成功已停任务，现已明确创建/已有目标保持禁用，仅双明确缺货且无目标/无旧竞争可另授权 cron；同时明确费用授权须计算历史已用＋剩余月预计用量。审查者随后只读复核指定段落，确认 P2 关闭、费用措辞明确，在静态与离线范围内无剩余阻塞。代码和测试在该文档修正阶段未变化，未无必要地重复测试。没有预设 PASS，也没有自审冒充独立审查。

补充配置检查：真实 YAML 解析、生产 Python 编译与 `/bin/bash -n` 均通过；手动入口独占、锁 true、20分钟 job 上限确认。当时审查不覆盖真实签名、IAM、容量、免费资格、网络、消息送达或 workflow 远程状态。

远程状态文档专项复核：未参与实现的独立上下文只读核对 README 与本报告新增远程段落。主体事实（`main` 头部、hunt 旧 CLI blob、5 条失败 dispatch、启用/禁用未知、无本地 git）一致，未见凭证值。发现 P1：不得把 hunt 运行快照写成当前无竞争，且须写明 `create-vm.yml` 在发布前仍可能按 `*/5` cron 触发；已按该结论改文档。P2 已把启用/禁用改为两个文件都未知，把“运行状态已核对”改为“内容与运行记录”，并去掉审查清单外的提交说明。此专项不覆盖代码、真实签名或再次联网。

## 普通查询429专项收口

主控复核发现普通查询429被错误改为告警禁用。本次只恢复这一项：顶层 OciError 分支在普通模式遇429静默结束本轮，不创建、不通知、不禁用；诊断模式错误仍只读失败，不放松诊断锁。reconcile() 仲裁分支原样保留：创建结果不确定后若复查429，告警禁用，不再提交创建或转AMD；既有launch明确429分支未改。

有区分力的两个用例为组28和组10：生产修正前，组28的8类普通查询429全部因退出1而失败，组10原已通过；最小修正后两个用例均通过，随后完整28组均通过。组10明确断言只有一次ARM launch、初始/创建前/一次仲裁共3次目标查询、一次告警、一次禁用及rate_limit/http429；组28分别对实例、region subscriptions、AD、subnet、compartments、block volumes、boot volumes、images查询429断言退出0、业务输出为空、零创建/通知/禁用。所有调用仍mock，未读取真实凭证、发布或真实调用。

本次收口前基线 `/tmp/oci-sdk-query429-baseline`，增量diff `/tmp/oci-sdk-query429-review.diff`；原四文件完整diff仍在 `/tmp/oci-sdk-six-step-review.diff`。原独立审查者已完成专项只读复核并独立复跑这两个区分用例，均通过，未发现新增阻塞。审查确认：workflow第397行静默分支只作用于普通模式查询429；reconcile第297–300行仍内部捕获并告警禁用，不会落入静默分支；launch429未改。当前完整28组由实现上下文执行通过，原审查者本次仅复跑两个专项用例，未重复完整套件、修改文件、联网或真实调用。

## 主控可审批的具体下一步

审批对象只有四文件最终 diff；部署需 workflow，测试与说明一起归档。远程默认分支、hunt 工作流内容与运行记录已于 2026-10-15 只读核对：远程为旧 CLI 3.94.2 版、hunt 5 次运行全失败、当时无排队/进行中/schedule 运行。`GET /actions/workflows` 返回 400，hunt 与 create-vm 的启用/禁用都未知。hunt 的运行快照不等于已禁用，也不覆盖 create-vm，不能据此判断仓库无其他任务在跑；create-vm 自带 `*/5` cron，发布前仍可能持续触发，处理不能等到覆盖 hunt 的那一刻。发布动作是显式写入 `.github/workflows/hunt-free-vm.yml` 覆盖旧版，并同时停用或删除 `create-vm.yml`；本地无 `.git`，不能 `git init`，也不能据远程状态推断本地可提交。不查看 Secret 值。用户核实“本月历史已用量＋拟建实例本月剩余时间预计用量”未超免费额度，并核对 home region 免费标识、IAM及网络后，另行明确授权本版本发布和一次真实运行。授权后修改固定锁为 false，保持无 cron；单次运行最多 ARM 1次、仅明确缺货后 AMD 1次，至多接受一台，可能发送通知和禁用当前 workflow。创建被接受/已有实例或双方明确缺货才算主链路验收；认证/配额错误需按固定阶段类别行动，不能记作通过。

创建被接受或发现已有目标即完成验收并保持 workflow 禁用，不启用 cron。只有 ARM、AMD 均明确缺货，复核仍无目标实例且旧任务无竞争时，才可另行授权添加 `*/15 * * * *` 调度并启用。429、查询/配置错误、未知创建结果、通知或禁用失败均不是启用依据。一次验收加一次证据明确的修正仍无法进入创建/缺货则停止扩建，交人工控制台单次创建或官方 Resource Manager，不自动执行替代。容量不保证上午获得。

停止/回滚：Disable workflow，取消未开始队列，等待已提交请求结束；控制台核对所有相关 compartments 的实例与启动盘，状态不明不能重启。必要时恢复旧文件但维持禁用与锁 true，不恢复旧自动抢建。任何资源删除另行授权。GitHub禁用失败时下一轮仍可能存在，必须立即人工禁用；concurrency仅本仓库，不等于tenancy原子锁。免费盘点只覆盖固定区域当前实例/卷，不覆盖历史使用、其他区域及已有依赖服务费用。

## 差异与指纹

本轮写入前四文件指纹二次核对一致，未发现并发改写；本次仅改 README 与本报告，workflow 与测试指纹未变。审查前无凭证基线存于 `/tmp/oci-sdk-six-step-baseline`；工作目录仍未初始化 Git。可用 `diff -u` 对比各同名文件，禁止把未审查版本发布。原 workflow 452行，本版405行；完整差异另存 `/tmp/oci-sdk-six-step-review.diff`，按以下最终指纹确认（报告自身不自引用）：

- `.github/workflows/hunt-free-vm.yml`：SHA-256 `1f7e06cd23f985746abe2f1bfece453faa86847434f103ffe163e01ba80a944a`
- `tests/offline_test.py`：SHA-256 `b1be56544c85865086562b6a65ffc394d367a5a6bba6620e61012ab74ffae4e8`
- `README.md`：SHA-256 `a8cb0b4b177ccef20a55f8a8181541a8041282750e953b9a9b6ab6846d28fb05`

联网核对实际使用 ego-browser；默认沙箱连接失败后用获准的外部只读执行成功，未触发 fallback，task space 7 已关闭。官方免费额度文档和 SDK 版本链接见 README。


## 2026-10-15：SDK 导入错误的固定分类（本地变更，未发布）

GitHub Actions #6 的可见诊断为 `stage=credentials credential_step=sdk_import category=runner_error http=none`。工作流代码显示 SDK 安装命令返回成功后才执行 `import oci`，因此当前证据把失败范围缩小到 SDK 导入阶段；它没有给出底层异常，也不能证明 Secrets 有误。

本地改动仅在 SDK 导入处把 `ModuleNotFoundError`、`OSError`、`ImportError` 和其他普通异常映射到固定白名单标签；缺失模块名只用于判断是否为 `oci` 本身，绝不写入日志。`HUNT_DIAGNOSTIC_LOCK` 仍为 `true`。尝试运行离线测试套件时，当前本地 Python 因未安装 `oci` SDK、在测试开始前即退出；未安装依赖，因此新增用例及当前完整套件均未在本机验证。此前 31 项 mock 测试通过属于早先状态，不作为当前文件版本的测试证据。当前工作流内嵌 Python 与测试脚本的语法解析、锁定状态和无定时触发器的静态检查通过。独立只读复核已完成，未发现阻塞问题；真实 OCI 根因仍未知。


## 2026-10-15：SDK 导入路径刷新与解除诊断锁

根本原因已查明：runner 用户环境在 Python 启动后执行 pip install，新创建的 user site-packages 目录未自动载入当前进程的 sys.path。通过 site.addsitedir(site.getusersitepackages()) 与 importlib.invalidate_caches() 刷新导入路径与缓存，彻底解决 ModuleNotFoundError。
同时按用户明确授权将 HUNT_DIAGNOSTIC_LOCK 设为 'false'，准备执行单次真实验收（至多 ARM 1次，明确缺货才 AMD 1次，无 cron）。
离线测试：完整 32 个测试全部通过（0 失败，0 错误）。
