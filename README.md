# Oracle Always Free 抢建 workflow v3.1

仅使用 `.github/workflows/hunt-free-vm.yml`、内嵌 Python 脚本与 GitHub Secrets。固定 `ap-kulai-2`，每 15 分钟运行，也支持手动运行；GitHub 原生 concurrency（`group: oracle-free-vm-hunt`）串行执行，`cancel-in-progress: false`。当前目录原为空，未初始化 Git，未部署、未连接真实 OCI 创建资源。

优先创建 `VM.Standard.A1.Flex`（2 OCPU / 12 GB）。只有每个候选 AD 的 ARM 创建均明确返回 `500 InternalError: Out of host capacity`，才尝试 `VM.Standard.E2.1.Micro`（固定规格，约 1/8 OCPU / 1 GB）。AMD 被接受后即停止并禁用 workflow，后续不会继续抢 ARM。所有候选 AD 都限制在上述区域；指定 `AD_NAME` 或使用 AD 专属 subnet 时，只使用对应 AD。

启动盘通过 `--source-details` 显式固定为 50 GB、10 VPU/GB（Balanced），不挂载额外卷、不建立容量预留、不创建网络、负载均衡或付费镜像订阅。使用 `--assign-public-ip true`，要求指定 subnet 明确允许分配公网 IP（`prohibit-public-ip-on-vnic` 为 `false`）；禁止分配或无法确认时停止、禁用 workflow 并告警，不创建实例。实例主 VNIC 将请求分配临时公网 IPv4，供远程 SSH 使用；workflow 不会创建网络资源或修改路由、安全组。公网 IP 本身不保证 SSH 可达，需完成下面的网络检查。

## 部署前必须人工确认

1. OCI Console 中确认 tenancy 的 **home region 为 ap-kulai-2**，该区域支持所选 Always Free shapes，Ubuntu 镜像标记为免费适用。运行时也会查询 home region，任何不匹配都停止。
2. 确认 **本月历史用量**与现有资源不会超过账号当前免费额度。官方当前列出的 A1 免费额度为每月 1,500 OCPU 小时、9,000 GB 小时；本脚本不查询账单历史。删除旧实例不会抹去其本月已消耗的额度。试用余额也不能作为 Always Free 资格证明。
3. 确认 tenancy 内没有其他有效 A1/E2 实例，boot/block volume 合计仍有免费余量，且已有 subnet、网络出口与依赖服务不会造成额外费用。脚本会列举全部 ACTIVE compartments、所有 AD 的 boot volumes 及 regional block volumes，累计非 TERMINATED 卷，确保添加启动盘后不超过 200 GB；其他 compartment 有 A1/E2 则停止并要求人工确认。
4. 确认 API 用户有遍历 tenancy compartment 的权限，并能读取各 compartment 中的实例及卷、读取 AD、region subscriptions、subnet 和 images，以及在目标 compartment 创建实例、启动盘、VNIC。查询失败不视为空库存；不完整的 IAM 权限会阻止运行。建议配合 OCI compartment quotas 限制本 workflow 可使用的 CPU、内存及存储，并配置预算告警。Budget 告警不是自动硬性费用上限。
5. 运行期间不要让其他仓库、其他自动化或 Console 操作在该 tenancy 中并行创建资源。GitHub concurrency 只协调当前仓库，无法对 OCI tenancy 加锁。只部署这一份 workflow，不同时运行上游 workflow。
6. 确认 GitHub Actions 分钟额度与组织策略允许 `GITHUB_TOKEN` 的 `actions: write` 权限；设置 Telegram bot，先在目标聊天与 bot 建立可发送消息的关系。缺少通知凭据时 GitHub 日志会提示人工处理。
7. 确认指定 subnet 是允许分配公网 IP 的**公网子网**，VCN 已有可用的 **Internet Gateway**，subnet 使用的路由表包含指向该网关的 `0.0.0.0/0` 路由。当前脚本仅检查 subnet 的公网 IP 许可字段，不自动创建或验证网关与路由。
8. 在 subnet 现有 Security List 中配置 **TCP 22 入站**，优先只允许你的公网来源 IPv4（单地址用 `/32`）；同时确认实例系统防火墙允许 SSH。当前脚本不指定额外 NSG，如有其他适用规则，也需人工核实。不要开放全部端口；如果来源 IP 变化，人工更新允许的来源。创建后使用自己的 SSH 私钥连接 `ubuntu@<实例公网IP>`，到 Console 核实实际地址及最终状态。

任何一项免费资格无法确认，请保持 workflow 禁用，先人工核实；本实现不会自动升级账号、切换付费 shape、扩容或追加实例。

## Secrets 设置

在准备好的 GitHub 仓库中打开 **Settings → Secrets and variables → Actions → New repository secret**。不要将真实值放入 README、提交、Issue、运行输入或聊天中。

| 必填 Secret | 内容 |
| --- | --- |
| `OCI_CLI_TENANCY` | Tenancy OCID |
| `OCI_CLI_USER` | OCI API 用户 OCID |
| `OCI_CLI_FINGERPRINT` | 已登记 API key 的 fingerprint |
| `OCI_CLI_KEY_CONTENT` | 对应未加密 PEM 私钥的完整内容，仅存于 Secret |
| `OCI_COMPARTMENT_ID` | 目标 ACTIVE compartment OCID，也可为 tenancy root |
| `OCI_SUBNET_ID` | ap-kulai-2 中已有、AVAILABLE 且允许公网 IP 的公网 subnet OCID |
| `SSH_PUBLIC_KEY` | 单行 SSH 公钥，支持 ed25519、RSA、ECDSA；不要填 SSH 私钥 |
| `TG_BOT_TOKEN` | Telegram bot token |
| `TG_CHAT_ID` | 接收通知的 chat ID |

| 可选 Secret | 内容 |
| --- | --- |
| `AD_NAME` | 同区域 AD 的完整名称；不填则自动发现，并遵守 subnet 的 AD 限制 |
| `IMAGE_ID_ARM` | 同区域、ARM 兼容的官方 Canonical Ubuntu 24.04 镜像 OCID |
| `IMAGE_ID_AMD` | 同区域、AMD 兼容的官方 Canonical Ubuntu 24.04 镜像 OCID |

无需提供 `GITHUB_TOKEN` Secret；workflow 使用内置 token。无需区域 Secret。没有镜像覆盖时，按 shape 查询全部镜像页，按创建时间倒序选择官方 Ubuntu 24.04 / 24.04 Minimal。ARM 必须为 aarch64，AMD 必须为非 aarch64；镜像须 AVAILABLE、无自定义 compartment、位于固定区域且大小不超过启动盘。提供覆盖时仍验证上述条件并调用 image-shape compatibility API，不能绕过检查。若镜像已从默认列表下架，可用经过上述验证的覆盖 OCID。

私钥、公钥仅在 runner 临时目录使用，目录权限 700，文件权限 600，退出时删除。Shell 关闭调试回显，Secrets 经环境变量传入；OCI 输出被捕获且不写入日志、报告或 Telegram。HTTP 凭据经 curl stdin 传递，通知仅含固定状态说明及经白名单验证的实际 shape / 生命周期状态，不包含实例 OCID、原始错误或其他凭据。OCI CLI 固定版本为 `3.94.2`，在 GitHub runner 内安装，安装诊断同样不回显。

## 本次保留的方案差异

安装固定版本 CLI，未实现原文的 `~/.local` 缓存；认证使用 API-key 环境变量和隔离临时密钥文件，未还原 `~/.oci/config`。保留原有 tenancy / 存储 / subnet 安全盘点及额外 IAM 要求，仍支持同区域候选 AD，严格镜像验证允许 Ubuntu 24.04 Minimal；这些实现选择未在本轮重构，不代表与方案原文逐项等同，也不证明真实免费资格。每轮先完成 AD、安全盘点和 ARM/AMD 镜像发现，再查目标实例；已发现镜像在本轮复用，每次 launch 前仍复查实例与资源余量。

## 停止和异常规则

- 每次创建前用 `oci compute instance list --all` 查指定 compartment，跨全部 AD，不按名称筛选。RUNNING、PROVISIONING、STARTING、STOPPING、STOPPED、MOVING、TERMINATING 都按非 TERMINATED 的已有目标实例处理：通知中包含实际 shape 和状态，说明目标已达成，然后禁用 workflow，不再创建。TERMINATED 可忽略；未知状态保守停止、告警并禁用。
- 所有 OCI 命令均包含 `--no-retry`；无 CLI 内部重试、等待器或自动退避。两次候选创建之间重新查询实例和整个 tenancy 的免费资源余量。库存为空不等于 API 提供原子锁。
- 明确容量不足允许继续候选；全部无货时不打印业务消息、不发送 Telegram、不禁用，留待下一次定时运行。GitHub runner 自身仍有系统运行日志。
- 初始目标实例查询明确 429 或创建明确 429：静默停轮、不禁用、不再创建或转 AMD。其他查询失败告警并禁用；初始 AD、镜像或安全盘点发现失败（包括 429）在创建前告警并禁用。认证、配额、配置和其他已知创建拒绝异常同样停止、告警并禁用；401/403 不复查。
- 创建超时、连接错误、无法解析响应或其他结果不明时，只重新查询一次。找到任一已识别的非 TERMINATED 目标实例（包括 TERMINATING）按已有实例处理；空结果、查询失败、复查 429、解析或状态异常均按无法确认处理：告警、禁用，不再创建或转 AMD。
- 成功创建以 OCI 接受创建并返回有效生命周期为准，可能仍在 PROVISIONING；请到 Console 检查实例最终状态。未验证 SSH 或系统启动成功。
- 禁用的是 `GITHUB_WORKFLOW_REF` 对应的当前 workflow 文件，调用 GitHub REST `PUT .../disable`，严格要求 **HTTP 204**。先发送初始成功通知或异常告警，再尝试禁用；初始通知失败也必须继续尝试禁用。非 204（包括 HTTP 200）或传输失败时追加独立告警“自动禁用失败，请手动去 Actions 页 Disable workflow”，job 返回失败并停止。发送和禁用调用均有超时且不循环重试；追加告警无法送达时输出固定、不含敏感值的失败提示。
- Telegram 必须返回 HTTP 200 且 JSON `ok: true` 才视为送达；失败会在 GitHub 日志显示固定提示并使 job 失败。缺失 Secret、依赖安装失败等同样保守停止。
- runner 强制终止、基础设施故障或无法访问 GitHub/Telegram 时，脚本无法保证完成复查/通知/禁用。尤其不要在提交创建请求时手动取消运行；发生此类中断应立即人工禁用并核实 OCI，再决定恢复。

## 放入 GitHub 与手动恢复

把 workflow 文件放入准备好的仓库默认分支 `.github/workflows/`，配置 Secrets，并在上述配额检查完成后才启用 Actions / 手动触发。GitHub schedule 运行默认分支上的文件，调度可能延迟，并非精确 SLA。本交付没有执行这些步骤。

**60 天无活动限制属于 GitHub：** 公共仓库连续 60 天没有仓库活动时，GitHub 会自动禁用定时 workflow。本实现不做自动保活。定时运行本身不能代替维护检查；无实例时可确认免费资格后手动重新启用。Oracle 的空闲实例回收另按官方 7 天监测窗口、CPU/网络利用率及 A1 内存利用率规则执行，不能把它理解为 60 天保留承诺。

人工恢复顺序：

1. GitHub **Actions → Hunt OCI Always Free VM v3.1 → … → Disable workflow**，并取消未开始的队列；等待任何正在发送创建请求的运行结束。禁用 schedule 不会撤销已提交的 OCI 请求。
2. 在固定区域的 OCI Console 查看目标及其他 compartments 的 A1/E2 实例和全部启动盘。对于不明创建结果，等待库存最终稳定；已有有效实例就保持禁用。
3. 人工修复 IAM、Secrets、镜像或配额问题；不要通过增加规格或付费资源解决。若需删除失败资源，人工逐项确认，workflow 不执行清理。
4. 重新确认本月额度、免费存储及没有并行创建者；若仍需一台实例，在 Actions 菜单选 **Enable workflow**，再 **Run workflow**。AMD 已成功时不要为了追 ARM 恢复此任务。

## 离线验收

在本目录运行：

```sh
python3 -I tests/offline_test.py
```

只用 Python 标准库。直接提取并运行 YAML 中的实际内嵌脚本，不另存第二份生产逻辑。OCI、GitHub curl、Telegram curl 和 pip 安装全部被替换为 mock；未知命令会失败，不读本机 OCI 配置，不连接真实服务。保留原五组并补充通知失败、前置发现、损坏响应和精确 workflow 配置检查；每轮测试隔离 HOME、环境和临时目录，具体断言和本次结果见 [OFFLINE_TEST_REPORT.md](OFFLINE_TEST_REPORT.md)。mock 的成功不代表真实 OCI 部署成功。

## 实现来源与官方依据

通过 ego-browser 通道读取以下来源，未触发 fallback，task space 已关闭。保留 [heykapil/oci-free-arm-instance](https://github.com/heykapil/oci-free-arm-instance) 的 GitHub Actions + OCI CLI + Secrets 思路及 launch 参数组织方式，替换其超过本任务上限的规格、完整响应通知与人工停止行为。

- [OCI Always Free 资源、home region、配额与空闲回收](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [OCI CLI launch 参数](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/compute/instance/launch.html)
- [OCI CLI 镜像查询与 --all 分页](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/compute/image/list.html)
- [OCI Ubuntu 24.04 平台镜像](https://docs.oracle.com/en-us/iaas/images/ubuntu-2404/)
- [GitHub 禁用 workflow：HTTP 204](https://docs.github.com/en/rest/actions/workflows#disable-a-workflow)
- [GitHub 定时触发与 60 天无活动规则](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

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
