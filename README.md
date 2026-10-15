# 柔佛 Always Free 单机 workflow（SDK 版）

本地准备版，仅改动 workflow、离线测试、本说明和测试报告四个文件。**未发布、未调用真实 OCI、未启用或手动运行 GitHub Actions。** 远程已于 2026-10-15 只读核对：默认分支 `main` 头部 `d0d9f5e6`，其上 `.github/workflows/hunt-free-vm.yml` 仍是旧 CLI 3.94.2 版（blob `26c578ae`，27,594 字节），与本版 SDK 工作流（blob `713cde4c`，405 行）不同，远程还带着 `cron: '*/15 * * * *'`。hunt 的最近 5 次运行全是 `workflow_dispatch` 且结论失败，末次 2026-10-12T22:41:44Z；当时无排队、无进行中、无 schedule 运行。该快照不等于 hunt 已禁用，也不覆盖 `create-vm.yml`。`GET /actions/workflows` 返回 400，hunt-free-vm.yml 与 create-vm.yml 的启用/禁用状态都未知。仓库里另有遗留 `create-vm.yml`（cron `*/5`、区域 `ap-singapore-1`、A1.Flex 4 OCPU/24 GB、200 GB 启动盘、`continue-on-error: true`，把 Secrets 明文写入 runner 的 `~/.oci/config` 并把原始 OCI 输出发到 Discord webhook），既非免费档也有凭证暴露面。启用状态未知，其 cron 在发布前仍可能每 5 分钟触发；处理不能等到覆盖 hunt 的那一刻才开始，发布时必须一并停用或删除，不得由我们重新启用。诊断锁保持 `HUNT_DIAGNOSTIC_LOCK: 'true'`，本版不含 cron。目标是一个可审查的闭环，不保证上午内取得容量。

## 固定行为与边界

使用官方 Python SDK `oci==2.187.2` 的结构化返回，移除 OCI CLI 和 stdout/stderr 解析，不保留第二条调用路径。原有 GitHub Secrets 名称沿用，Telegram 和自动禁用当前 workflow 的逻辑保留；API 私钥只交给内存 signer，不读取 `~/.oci/config`，不写凭证文件。

只在 `ap-kulai-2` 的一个 AD 创建一台：优先 `VM.Standard.A1.Flex`，明确固定为 2 OCPU / 12 GB；仅当 ARM 返回 HTTP 500、code `InternalError` 且 message 精确归一化为 `Out of host capacity` 后，才发现 AMD 镜像并尝试 `VM.Standard.E2.1.Micro`。无跨 AD 创建循环；优先使用可选 `AD_NAME`，其次 subnet 的 AD，否则选择排序后的首个 AD。AMD 成功就结束，不再追 ARM。所有启动盘固定 50 GB、10 VPU/GB；不创建网络、附加卷、容量预留或付费镜像订阅。

每轮先完整列举目标 compartment 的实例；两种目标 shape 中任何非 TERMINATED 实例（包括 PROVISIONING、STOPPED、TERMINATING）均通知并停止，不再创建。正常创建前重新检查目标与 tenancy 资源；ARM 缺货后也重查，防止残留卷挤占存储或并行创建者已经取得实例。GitHub `concurrency.group: oracle-free-vm-hunt`、`cancel-in-progress: false`，只能协调当前仓库，不能给 OCI tenancy 加原子锁。

所有支持分页的集合沿 `opc-next-page` 读完，后页错误、坏集合、重复 token 均关闭创建；root 显式纳入盘点（SDK `list_compartments` 没有 CLI 的 `--include-root` 参数）。列举所有 ACTIVE 子 compartment，拒绝其他 compartment 的有效 A1/E2，并对所有 AD 的 boot volumes、各 compartment 的 regional block volumes 累计非 TERMINATED 容量，确认加 50 GB 后不超过 200 GB。查询失败绝不按空集合处理。

ARM、AMD 都明确缺货：输出固定 `capacity_unavailable` 结果，正常结束本轮，保留未来调度；launch 明确 429：输出 `rate_limit`，停止本轮，不转 AMD。普通查询明确 429：静默停轮，不创建、不通知、不禁用；创建结果不确定后的仲裁复查 429 仍按无法确认处理，告警并禁用、不重建。其他查询失败、认证、权限、配额、镜像或配置错误：发送固定阶段/类别/HTTP 告警并禁用。SDK 的 `NoneRetryStrategy` 同时用于客户端和调用；另显式设 `OCI_HEADER_PARSING_ERROR_MAX_RETRIES=0`，关闭 2.187.2 独立响应头重试。每次 launch 使用独立请求幂等 token，但不重试 launch。

创建超时、传输失败、响应不完整或模糊 5xx：仅重新查询目标一次；找到有效目标则通知并禁用，否则告警、禁用，绝不盲目重建或转 AMD。创建成功表示 OCI 接受并返回已知生命周期，可能仍是 PROVISIONING；不等同启动完成或 SSH 可用。Telegram 失败仍尝试禁用；禁用必须 HTTP 204，失败追加独立人工告警并使 job 失败。先通知、再禁用，通知与禁用都没有循环重试。

诊断锁缺失或非精确 `false` 都只读：可以认证和查询，但不 launch、不 Telegram、不禁用。手动 `diagnostic_only: true` 也强制只读。日志只输出固定阶段/类别/HTTP，不转发异常正文、响应、OCID、密钥、请求参数或身份。SDK 导入异常会使用固定标签区分 SDK 包缺失、依赖缺失、系统加载错误、导入错误和其他导入异常；标签不包含异常原文。SDK 日志关闭；HTTP 凭证经 curl stdin，调用输出捕获。runner 强制终止、GitHub/Telegram 不可达时无法保证复查、通知或禁用；此时必须人工禁用，核实 OCI 再恢复。

## 免费资格仍需人工核对

官方当前 A1 额度为每月 1,500 OCPU 小时、9,000 GB 小时，E2 Micro 最多两台，boot/block 合计 200 GB；免费 Compute 和卷须位于 home region。运行时验证 home region 为 `ap-kulai-2`。**本月历史用量、账号资格、其他区域费用、已有网络及依赖服务费用不在脚本证明范围；目标 compartment 为空不等于 tenancy 没有费用。** 删除旧实例不会清除本月历史用量。创建前在控制台核对 Always Free 标识、Limits/Quotas/Usage，并确认“本月历史已用量＋拟建实例在本月剩余时间的预计用量”仍在免费额度内，必要时用 compartment quotas 限制资源；Budget 告警不是硬费用上限。

需要遍历 tenancy compartments、读取其实例和卷、列举 AD/region subscriptions/subnet/images，以及在目标 compartment 创建实例、启动盘、VNIC 的 IAM 权限。无法完整查询时保守停止。运行期间禁止其他仓库、旧 workflow 或控制台并行创建；本脚本不会自动修改 IAM、升级账户或删除资源。

subnet 必须 AVAILABLE 并明确允许公网 IP。脚本请求临时公网 IPv4，但不验证或修改 Internet Gateway、路由、安全列表及系统防火墙。人工确认已有网关和默认路由，以及仅允许所需来源的 TCP 22 入站；创建后去控制台核对地址、最终状态与 SSH。

## 现有 Secrets

仍使用九个必填 Repository Secrets：`OCI_CLI_TENANCY`、`OCI_CLI_USER`、`OCI_CLI_FINGERPRINT`、`OCI_CLI_KEY_CONTENT`、`OCI_COMPARTMENT_ID`、`OCI_SUBNET_ID`、`SSH_PUBLIC_KEY`、`TG_BOT_TOKEN`、`TG_CHAT_ID`。名称中的 CLI 是兼容保留，不再安装 CLI。无需提供这些值给审查者，也不要放入日志、输入、文档或提交；`GITHUB_TOKEN` 继续使用内置 token，权限为 `actions: write`。

可选 `AD_NAME`、`IMAGE_ID_ARM`、`IMAGE_ID_AMD`。默认按 shape 查询全部镜像页，选择官方 Canonical Ubuntu 24.04 / 24.04 Minimal。校验 AVAILABLE、platform image、区域、镜像尺寸和 aarch64/AMD 架构；覆盖镜像也须通过同样校验和 image-shape compatibility API。ARM 与 AMD 镜像、shape_config 不混用。

## 六步落地与授权门

1. **可行性和状态核对（本地完成，远程已只读核对）**：四文件指纹已记录；当前目录没有 Git 元数据，不 init、不 clone，差异只能用 `diff -u` 对 `/tmp/oci-sdk-six-step-baseline` 生成。已复用临时隔离环境内 SDK 2.187.2；不继续诊断旧 CLI 故障。远程 `main` 头部提交、hunt 工作流内容为旧 CLI 版、hunt 运行记录 5 次全失败且当时无排队/进行中/schedule、以及 `create-vm.yml` 遗留，均于 2026-10-15 只读确认。`GET /actions/workflows` 返回 400，hunt-free-vm.yml 与 create-vm.yml 的启用/禁用状态都未知；hunt 的运行快照不等于无竞争，也不覆盖 create-vm。create-vm 自带 `*/5` cron，状态未知时发布前仍可能持续触发。授权后发布时须再次确认两个 workflow 的 state。
2. **最小迁移（本地完成）**：保留调度容器、Secrets、通知、防重复和费用检查；仅 OCI 操作改为 SDK。锁为 true，暂时移除 cron。
3. **必要离线验证（见报告）**：执行真实生产 heredoc，真实 SDK 方法/模型和序列化，外部调用全部 mock，隔离 HOME，不读取真实配置或真实凭证。
4. **独立审查（见报告）**：未参与实现的另一上下文只读审查生产入口、测试契约、费用/分页/不确定创建/凭证/发布边界，记录结论与修正。
5. **待主控取得新授权后发布并单次验收**：hunt 工作流内容与运行记录已只读核对，当时无排队、无进行中、无 schedule 运行；`GET /actions/workflows` 返回 400，hunt 与 create-vm 的启用/禁用都未知。hunt 的运行快照不等于已禁用，也不覆盖 create-vm；create-vm 自带 `*/5` cron，发布前仍可能持续触发，处理不能等到覆盖 hunt 的那一刻。发布时须停用或删除 `create-vm.yml`，再核对两个 workflow 的 state。人工核对本月额度、IAM、网络及现有 Secrets 的存在性。写入文件是 `.github/workflows/hunt-free-vm.yml`（覆盖远程旧 CLI 版）；测试和两份文档一起交付审查。由于本地没有 `.git`，发布只能是显式的文件内容写入或重新 clone 后提交，不得 `git init`。审核具体 diff 后，单独授权 commit/push、解除锁为 false 并手动 dispatch 一次；仍不加 cron。一次最多 ARM 一次、明确缺货才 AMD 一次，最多接受一台，可能通知及自动禁用。验收以“创建被接受/发现已有实例”或“两种 shape 明确缺货”为主链路证据，不能把认证等错误算通过。
6. **只在双明确缺货后另行授权调度**：创建被接受或发现已有目标即完成验收并保持 workflow 禁用，不启用 cron；只有 ARM、AMD 均明确缺货，复核仍无目标实例且旧 workflow 不竞争时，才可另行授权加入 `schedule: [{cron: '*/15 * * * *'}]` 并启用。429、查询/配置错误、未知创建结果、通知或禁用失败均不构成启用依据。GitHub cron 可能延迟，不是准点保证。成功/已有实例即停；异常告警停止。一次真实验收加一次有明确证据的修正仍无法进入创建或明确缺货，就停止扩建，改为人工控制台单次创建验证或官方 Resource Manager；不自动部署替代。

授权前不得 commit/push、调用 OCI、发送真实通知、启用或 dispatch workflow。历史批准不是本版本授权。本地基线在 `/tmp/oci-sdk-six-step-baseline`，具体差异与指纹见报告。远程 `main` 仍是旧 CLI 版 hunt workflow，另有遗留 `create-vm.yml`。hunt 当时无排队或进行中运行，不等于 hunt 已禁用，也不等于仓库无竞争；create-vm 启用状态未知，其 cron 在发布前仍可能每 5 分钟触发。不要假称旧 workflow 已禁用，也不要把“最近无运行”当成已禁用。发布必须用显式文件写入或另 clone 后提交，不要在当前目录 `git init`。

停止/回滚：Actions 页面 Disable 当前 workflow 与遗留的 `create-vm.yml`，取消尚未开始的队列并等待已发送请求的运行结束；禁用不会撤销 OCI 请求。到固定区域逐个 compartment 核实实例和启动盘，不确定结果时等待库存稳定。已有实例则保持禁用。代码需要回滚时恢复审查前版本但保持 workflow 禁用及诊断锁，不恢复旧自动抢建。任何删除操作另行逐项授权。

## 离线复现

需要官方 SDK `oci==2.187.2` 和已有 PyYAML。本轮直接复用 `/tmp/oci-boundary-20261013-sy0k0dzy/venv/bin/python -I tests/offline_test.py`，没有安装新依赖。其他机器在专用无凭证虚拟环境准备同版本后运行 `python -I tests/offline_test.py`。测试 mock pip 安装、OCI 网络、Telegram、GitHub；真实 config 读取和网络连接另被拒绝。不可用普通生产 workflow 代替离线测试。

## 官方依据

2026-10-15 使用 ego-browser 读取 [Always Free 资源](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) 与 [Python SDK 2.187.2 ComputeClient](https://docs.oracle.com/en-us/iaas/tools/python/2.187.2/api/core/client/oci.core.ComputeClient.html)，并核对本地同版本 SDK 的配置、分页参数、模型和请求重试源码。首选浏览器在默认沙箱不可达，获准以外部只读方式成功读取，未触发 fallback，task space 已关闭。此次 SDK 迁移不说明历史 `block_volumes/stdout_empty` 根因已经定位。

上游 MIT 许可声明（保留归属）：

```text
MIT License

Copyright (c) 2025 Kapil Chaudhary

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
