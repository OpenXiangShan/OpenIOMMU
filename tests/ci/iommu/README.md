# OpenIOMMU CI

GitLab、GitHub 和本地共用 `ci.py`，以及唯一的 `dependencies.json`、
`assets.lock.json`。平台 YAML 只适配事件、runner、凭据、作业依赖和制品展示。
两边从同一 JFrog 下载同一份工具和镜像，不维护平台专用测试脚本。

## 输入和构建

被测 RTL 是当前 checkout 的 `iommu_wrap.sv`、有序 `iommu_wrap.f` 及其依赖。
CI 使用 `CONFIG_RISCV_IOMMU_BOSC_V2_LITE`，不启用 `IOMMU_IMPLEMENTATION`。
源码变化后必须使用新的 work-dir；不会复用旧 RTL 模型或覆盖旧测试结果。

每条流水线在 select 阶段通过 HTTPS 查询 `https://github.com/OpenXiangShan/qemu`
的 `refs/heads/devel` 最新 commit，记录在 `pipeline.json`；
解析命令、远端 SHA 和错误保存在 `logs/qemu-resolve.log` 及配套 command JSON，
同时输出到 job 页面；解析失败直接阻塞，不回退本地缓存。
后续阶段只取这个 commit。io-system 和 IOMMU API 使用该 QEMU 内置版本。
QEMU 的 native wrap archives 及 packagefiles 也经过 revision/SHA256 校验。
依赖分支更新导致 wrap/API 不兼容时 CI 失败，不回退到旧预编译 QEMU。

Picker/XSPComm 从 JFrog 获取完整、固定的 Linux x86_64 SDK。包内包括 picker、
模板、头文件、libxspcomm、许可证及 provenance；coroutine ABI 一致。
Picker/XSPComm 工具路径全部显式传递，不从系统 PATH 拼凑组件。Picker 解析
RTL 所需的 Verible 由 runner 的 `IOMMU_CI_VERIBLE_DIR` 提供。当前包来自 XSV 固定 commit
`0221570d2434062d7ea869ef13548520e35ed409` 中的 picker/xcomm 源码。
`package_tools.py` 记录实际源码 archive 的 SHA256、compiler 和模板适配。
RTL 仿真使用 runner PATH 中的 Verilator；安装目录通过
`verilator --getenv VERILATOR_ROOT` 获取，不写死工具安装路径。
固定 SDK 的生成模板由 `build_picker.sh` 在本次输出目录内适配：拆分 simulator
flags，通过 `-f` 传递有序 filelist，并补齐 SDK 无波形提示的换行，避免提示与
APB 错误行粘连；严格 APB 检查保持不变。不修改下载的 SDK 或被测 RTL。

Linux、fw_jump、rootfs 使用现有 QEMU CI 的固定 JFrog 输入。
只读来源镜像每次复制为私有 rootfs/NVMe 磁盘；guest 不写下载缓存或 bundle。
Runtime bundle 包含 QEMU、API/UT/XSPComm 和启动输入，
通过摘要校验后可在新的工作目录运行。完整日志不截断，网页输出默认限 256 KiB。
Linux 启动和 guest 命令的串口输出实时转发到两平台 job 页面，同时完整保存到
`results/qemu-linux-nvme-ext4/{uart.log,driver.log}`；页面上限由
`IOMMU_CI_LIVE_LOG_KIB` 配置，达到上限后仍继续采集完整日志和执行检查。
页面输出去除串口回车字符，避免 GitLab 渲染时吞掉 `\r\r\n` 行；原始日志保留。

## 本地运行

需要 Linux x86_64、Python 3.12+、C/C++20 compiler、CMake、Make、Ninja、Git、
pkg-config、dtc、Verible、GLib/pixman/zlib 开发包、libnuma 开发包和 Verilator
（本地验收版本 5.052，需支持 `--timing`、`--threads` 和 CMake 集成）。
模型默认使用 8 个宿主线程，关闭 coverage 和波形输出；Linux guest 仍使用
1 CPU、8 GiB 内存和原有 `-icount shift=3,sleep=off`。Linux case 的外层超时为
2400 秒，启动等待 1200 秒，以容纳 Verilator 的运行时间。
安装 `requirements.txt` 到 runner 的独立 Python 环境；CI 不自动修改主机环境。

```bash
python3 -m pip install -r tests/ci/iommu/requirements.txt
python3 -m unittest discover -s tests/ci/iommu -p test_ci.py -v
python3 tests/ci/iommu/lint_ci.py

export IOMMU_CI_JF_BASE_URL=http://10.13.18.1:8082/artifactory
# IOMMU_CI_JF_TOKEN 由环境安全注入，不写入命令或日志。
python3 tests/ci/iommu/ci.py all --jobs 8
```

`all` 默认创建 `.ci-work/<时间-随机值>`。分阶段调用显式传递相同 `--work-dir`。
两平台的 check 作业共用 `ci.py check`，先运行框架自测、lint 和 diff 检查，
全部通过后才进行 select。完整检查日志保存在 `logs/`；检查失败时保留
`stage-errors/check.json` 和失败报告，即使尚未生成 `pipeline.json` 也会上传证据。
`--case ID`/`--group rtl|linux` 支持定向运行；`--full` 强制全量；`--base SHA`
用于本地 diff 选测。普通 CLI 未提供可靠 diff 时默认全量。仅根 README/LICENSE
或 `docs/` 下的纯文档变化可以不跑 runtime；测试说明或脚本变化仍跑全量。

离线输入通过 `--assets-dir DIR`，目录按锁文件各 asset 的 `path` 布局组织。
`IOMMU_CI_CACHE` 可指向下载缓存，键为 SHA256，使用前仍验证大小和摘要。

报告为 `reports/all/{summary.md,results.json,junit.xml}`，每项结果带 run ID、
nonce 和证据摘要。`NOT_SELECTED` 不冒充 PASS；缺结果、陈旧 bundle、超时、
非预期 fault、DMA/内核/文件系统错误、非零退出均阻塞。故障测试验证准确的
cause/DID/TTYP/IOVA，不全局忽略 fault。取消和超时只清理本次进程组。

## 两平台入口

GitLab 根 `.gitlab-ci.yml` 使用现有 Shell runner，tags 为
`x86_64,tcg,bosc-ci`，可用 `IOMMU_CI_RUNNER_TAG` 覆盖最后一个 tag。
先把现有 runner 授权给 OpenIOMMU 项目，并配置只读源码和 JFrog 权限。

GitHub 使用内网 self-hosted runner，labels 为
`self-hosted,Linux,X64,openiommu-ci`。本 CI 不自动注册 runner；由仓库/组织管理员
注册到同等环境，并允许执行本仓库工作流。push 同时覆盖默认分支 `master` 和开发分支 `open_iommu_v2`；
默认分支变更时同步调整入口。

两边都配置以下同名环境变量：

| 变量 | 用途 |
| --- | --- |
| `IOMMU_CI_JF_BASE_URL` | 共用 JFrog 的 `/artifactory` URL |
| `IOMMU_CI_JF_TOKEN` | 只读下载 access token；GitLab masked variable / GitHub secret，统一使用 Bearer 认证 |
| `IOMMU_CI_VERIBLE_DIR` | `verible-verilog-syntax` 所在目录；默认使用共享工具目录 |
| `IOMMU_CI_CACHE` | 可选下载缓存 |
| `IOMMU_CI_JOBS` | 构建并发，默认 8 |
| `IOMMU_CI_MODEL_THREADS` | Verilator 模型宿主线程数，默认 8；构建时固定 |

普通流水线通过公开 GitHub HTTPS 拉取 QEMU，不需要 GitLab SSH 身份。
runner 服务账户必须能够访问 `https://github.com/OpenXiangShan/qemu`；
网络代理需在该服务账户的环境中配置，不能只依赖交互式 shell。
JFrog 下载只使用 `IOMMU_CI_JF_TOKEN` 的 Bearer 认证。`doctor` 阶段会在
正式构建前对一个已锁定文件发起一字节读取探测，确认 token 具有下载权限；
HTTP 401/403/404 会使预检失败。网络错误和摘要校验失败同样阻塞构建。
也可单独检查下载权限：

```bash
python3 tests/ci/iommu/ci.py check-jf
```

成功时只输出 HTTP 200/206 状态，失败时输出 HTTP 状态码，不会显示 token。
`tool_source` 和 SDK 的 `source_repository` 中的 XSV 地址仅记录工具源码来源，
普通流水线从 JFrog 下载锁定 SDK，不在运行时克隆 XSV。
RTL case 在各自组内依次执行；不使用 VCS license 或 VCS 主机锁。
运行阶段不要求安装 Verilator，只使用 bundle 内的已编译库；`.simulator` 标记
必须为 `verilator`，后端冲突会失败。
GitHub artifacts 用 tar 保留 Unix 权限，GitLab artifacts 同样只传 runtime bundle
和证据，不传 Meson build 树。report 始终执行，缺失选中的 case 视为失败。

外部 fork MR/PR 不自动执行内网任务。GitHub 仅运行无凭据 hosted 检查；审查后
通过 workflow_dispatch 的 `revision` 输入完整 40 位 SHA 来运行完整门禁。
GitLab 在目标项目以审查后的固定 revision 手动运行。没有 PR target 事件执行
未审查代码，也不设置 runtime allow_failure。

## 更新工具包

普通流水线只下载。维护者更新固定源码 commit 后，使用独立目录构建工具：

```bash
python3 tests/ci/iommu/package_tools.py \
  --source-repo /path/to/xsv --revision FULL_COMMIT \
  --output /absolute/new/tools-run --jobs 8
```

先验证真实 picker/RTL/API/Linux 链路，再显式调用 `publish_tools.py` 发布。
它需要单独的 `IOMMU_CI_JF_WRITE_TOKEN`，拒绝覆盖已有路径，下载校验后保存证据。
把生成的 `tool-spec.json` 合入 `assets.lock.json` 的 `picker_sdk`，升级作为独立
变更评审。写入 token 不配置到普通 GitLab/GitHub job。

验收区分框架自测、本地 Verilator/QEMU 实跑、JFrog 下载、GitLab 真实 pipeline、
GitHub 真实 workflow。尚未注册 GitHub runner 时，本地通过不代表线上通过。

## GitHub runner 故障排查

在实际 runner 服务账户及其 Python 环境中执行：

```bash
git ls-remote --exit-code --refs https://github.com/OpenXiangShan/qemu refs/heads/devel
python3 -m pip install -r tests/ci/iommu/requirements.txt
python3 tests/ci/iommu/ci.py check
```

`check` 默认生成新的 work-dir；确认 `pipeline.json` 的 `dependencies.qemu.commit`
与本次远端查询一致。不要复用失败运行的目录。后续 `doctor`、`build`、`run`
使用该目录，以保证整条流水线使用同一个 QEMU SHA。

如果依赖解析失败，查看 `logs/qemu-resolve.log` 和 `stage-errors/check.json`，
检查 runner 服务账户的 DNS、HTTPS 出站、代理设置。`check` 通过后，运行
`ci.py doctor --work-dir <本次目录>` 检查 Verilator、Verible 和编译依赖；构建还需要
`IOMMU_CI_JF_BASE_URL`、`IOMMU_CI_JF_TOKEN` 对应的下载权限。

修复需进入 GitHub 上的目标分支后，再触发新的 workflow；直接重跑旧提交的失败
运行仍会使用旧配置。线上验收以新运行的 `check/build/test/report` 全部通过为准。
