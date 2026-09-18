# OpenIOMMU

<!-- vim-markdown-toc GFM -->

* [简介（Introduction）](#简介introduction)
* [目录结构（Directory Structure）](#目录结构directory-structure)
* [使用方法（Usage）](#使用方法usage)
  * [XiangShan 仿真（XiangShan Simulation）](#xiangshan-仿真xiangshan-simulation)
  * [其他仿真工程（Other Simulation Projects）](#其他仿真工程other-simulation-projects)
* [许可证（License）](#许可证license)

<!-- vim-markdown-toc -->

OpenIOMMU 是北京开源芯片研究院（BOSC）开发的开源 RISC-V IOMMU 实现。
本仓库中的 `bosc-iommu-v2` 是当前用于 XiangShan 仿真集成的 SystemVerilog RTL
版本，顶层模块为 `iommu_wrap`。

OpenIOMMU is an open-source RISC-V IOMMU implementation developed by the Beijing
Institute of Open Source Chip (BOSC). The `bosc-iommu-v2` directory contains the
SystemVerilog RTL version currently integrated with XiangShan simulation. Its
top-level module is `iommu_wrap`.

## 简介（Introduction）

当前实现包括：

* IOMMU RTL：`bosc-iommu-v2/rtl_atd` 和 `bosc-iommu-v2/rtl_acd`
* 集成顶层：`bosc-iommu-v2/iommu_wrap.sv`
* APB 配置接口
* 面向设备请求的 AXI/ACE-Lite 从接口和转换后请求的主接口
* 页表遍历及相关数据通路接口
* 用于仿真集成的有序 RTL manifest：`bosc-iommu-v2/OpenIOMMU.mk`

This implementation includes:

* IOMMU RTL under `bosc-iommu-v2/rtl_atd` and `bosc-iommu-v2/rtl_acd`
* Integration top level at `bosc-iommu-v2/iommu_wrap.sv`
* An APB configuration interface
* AXI/ACE-Lite slave interfaces for device requests and master interfaces for
  translated requests
* Page-table-walk and related data-path interfaces
* An ordered RTL manifest for simulation integration at
  `bosc-iommu-v2/OpenIOMMU.mk`

## 目录结构（Directory Structure）

```text
OpenIOMMU/
└── bosc-iommu-v2/
    ├── iommu_wrap.sv       # 集成顶层 / integration top level
    ├── OpenIOMMU.mk        # Verilator/VCS 共用 RTL manifest
    ├── iommu_wrap.f        # 参考 filelist，保留 RTL 顺序，香山环境没有使用
    ├── rtl_atd/            # CDW/PTW地址转换、APB/CQ/FQ/PQ/MSI/HPM相关 RTL
    ├── rtl_acd/            # 由ATD通过AXI4S+自定义协议配置、TLB缓存、HPM及MRIF数据通路相关 RTL
    └── lib_sram/           # SRAM 模型，当开启宏定义IOMMU_IMPLEMENTATION时可以选择sram_wrap中的3种model
```

`OpenIOMMU.mk` 是仿真构建使用的统一入口。新增、删除或调整 RTL 文件顺序时，
应同步检查 `OpenIOMMU.mk` 和 `iommu_wrap.f`，不得使用无序的 `find *.sv` 替代。

`OpenIOMMU.mk` is the canonical entry point for simulation builds. When adding,
removing, or reordering RTL sources, keep it synchronized with `iommu_wrap.f`.
Do not replace the ordered manifest with an unordered `find *.sv` command.

## 使用方法（Usage）

### XiangShan 仿真（XiangShan Simulation）

将本仓库作为 XiangShan 的 `OpenIOMMU` 子模块初始化后，通过
`WITH_IOMMU=1` 显式启用 IOMMU。默认构建不例化 IOMMU，也不编译本仓库 RTL。

Initialize this repository as the `OpenIOMMU` submodule of XiangShan and use
`WITH_IOMMU=1` to enable it explicitly. The default build neither instantiates
the IOMMU nor compiles this RTL.

```bash
git submodule update --init --recursive

# Verilator
make clean
make emu CONFIG=DefaultConfig WITH_IOMMU=1 -j$(nproc)

# VCS
make clean
make simv CONFIG=DefaultConfig WITH_IOMMU=1 -j$(nproc)

# Baseline without IOMMU
make clean
make emu CONFIG=DefaultConfig -j$(nproc)
```

切换 `WITH_IOMMU` 的值前必须清理构建目录，以免复用由另一配置生成的 RTL。

Clean the build directory before changing `WITH_IOMMU`, otherwise previously
generated RTL may be reused with the wrong configuration.

### 其他仿真工程（Other Simulation Projects）

其他 Makefile 工程可以设置 `IOMMU_ROOT`，包含 `OpenIOMMU.mk`，再将其中的
`IOMMU_VSRC` 和 `IOMMU_VFLAGS` 传给 Verilator、VCS 或其他兼容的 SystemVerilog
仿真器：

Other Makefile-based projects can set `IOMMU_ROOT`, include `OpenIOMMU.mk`, and
pass `IOMMU_VSRC` and `IOMMU_VFLAGS` to Verilator, VCS, or another compatible
SystemVerilog simulator:

```make
IOMMU_ROOT := $(abspath path/to/OpenIOMMU/bosc-iommu-v2)
include $(IOMMU_ROOT)/OpenIOMMU.mk

SIM_VSRC   += $(IOMMU_VSRC)
SIM_VFLAGS += $(IOMMU_VFLAGS)
```

集成方应根据目标配置自行添加所需的编译宏。XiangShan 当前的轻量配置使用：

The integrating project is responsible for adding configuration-specific
preprocessor definitions. The current XiangShan lite configuration uses:

```make
SIM_VFLAGS += +define+WITH_IOMMU +define+CONFIG_RISCV_IOMMU_BOSC_V2_LITE
```
采用VCS仿真的时候，可以采用默认不打开IOMMU_IMPLEMENTATION的宏定义
使用DC综合/FPGA验证/VCS仿真的时候，可以打开IOMMU_IMPLEMENTATION，然后根据RTL_RAM_SIM/FPGA_RAM_SIM/ASIC_RAM_SIM选择匹配的模型

## 许可证（License）

OpenIOMMU 使用木兰宽松许可证第 2 版（Mulan PSL v2）。详情请参阅
[`LICENSE`](LICENSE)。

OpenIOMMU is licensed under the Mulan Permissive Software License, Version 2
(Mulan PSL v2). See [`LICENSE`](LICENSE) for details.
