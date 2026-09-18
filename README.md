# OpenIOMMU

OpenIOMMU 是北京开源芯片研究院（BOSC）开发的开源 RISC-V IOMMU
SystemVerilog 实现。仓库根目录就是 RTL 工程根目录，顶层模块为
`iommu_wrap`。

OpenIOMMU is an open-source RISC-V IOMMU SystemVerilog implementation from the
Beijing Institute of Open Source Chip (BOSC). The repository root is the RTL
project root, and the integration top-level module is `iommu_wrap`.

## 功能概览（Overview）

当前 RTL 包括：

* `iommu_wrap.sv`：集成顶层模块；
* `rtl_atd/`：地址转换、页表遍历、设备侧请求及软件管理通路；
* `rtl_acd/`：配置、TLB、事务处理及相关数据通路；
* `axi4_traffic.sv` 和 `iommu_axi_mon.sv`：AXI/ACE-Lite 相关接口逻辑；
* `lib_sram/`：RTL、FPGA 和 ASIC SRAM 模型；
* `iommu_wrap.f`：按依赖顺序排列的完整仿真 filelist；
* `Kbuild`：内核风格构建系统使用的源文件聚合入口。

The RTL is organized as follows:

* `iommu_wrap.sv`: integration top level;
* `rtl_atd/`: address translation, page-table walk, device-request, and
  software-management logic;
* `rtl_acd/`: configuration, TLB, transaction, and related data-path logic;
* `axi4_traffic.sv` and `iommu_axi_mon.sv`: AXI/ACE-Lite interface logic;
* `lib_sram/`: RTL, FPGA, and ASIC SRAM models;
* `iommu_wrap.f`: ordered simulation filelist;
* `Kbuild`: source aggregation entry point for Kbuild-style systems.

## 目录结构（Directory Structure）

```text
OpenIOMMU/
├── iommu_wrap.sv       # 顶层模块 / integration top level
├── iommu_wrap.f        # 有序 RTL filelist / ordered RTL filelist
├── axi4_traffic.sv     # 自测试激励产生模块，AXI4接口
├── iommu_axi_mon.sv    # 监控数据通路slv/mst功能模块
├── Kbuild              # 保留，当前环境不使用
├── rtl_atd/            # CDW/PTW地址转换、APB/CQ/FQ/PQ/MSI/HPM相关功能 / address translation datapath
├── rtl_acd/            # 由ATD通过AXI4S+自定义协议配置、TLB缓存、HPM、MRIF和数据通路相关功能 / configuration and transaction datapath
└── lib_sram/           # SRAM 模型，，当开启宏定义IOMMU_IMPLEMENTATION时可以选择sram_wrap中的3种model / SRAM models
```

`iommu_wrap.f` 中的路径均相对于 OpenIOMMU 仓库根目录。新增、删除或调整
RTL 文件时，应同步检查该 filelist 的顺序；不要使用无序的
`find *.sv` 替代它。RTL 文件直接位于本仓库根目录及其功能子目录中。

All paths in `iommu_wrap.f` are relative to the OpenIOMMU repository root.
Keep the filelist order when adding, removing, or reordering RTL sources; do not
replace it with an unordered `find *.sv` command. RTL sources live directly in
the repository root and its functional subdirectories.

## XiangShan 集成（XiangShan Integration）

XiangShan 将本仓库作为 `OpenIOMMU` git submodule 使用。先在 XiangShan
仓库根目录初始化子模块：

XiangShan consumes this repository as the `OpenIOMMU` git submodule. From the
XiangShan repository root, initialize the submodule first:

```bash
git submodule update --init --recursive
```

通过 `WITH_IOMMU=1` 显式启用 IOMMU。XiangShan 的 Makefile 会将
`OpenIOMMU/iommu_wrap.f` 直接传给 Verilator 或 VCS；未设置该变量时，默认
构建不会编译或例化 OpenIOMMU。

Enable the IOMMU explicitly with `WITH_IOMMU=1`. XiangShan's Makefile passes
`OpenIOMMU/iommu_wrap.f` directly to Verilator or VCS. Without this variable,
the default build does not compile or instantiate OpenIOMMU.

```bash
```make
SIM_VFLAGS += +define+WITH_IOMMU +define+CONFIG_RISCV_IOMMU_BOSC_V2_LITE
```

# Run from the XiangShan repository root.
make clean
make emu CONFIG=DefaultConfig WITH_IOMMU=1 -j"$(nproc)"

make clean
make simv CONFIG=DefaultConfig WITH_IOMMU=1 -j"$(nproc)"

# Baseline build without IOMMU.
make clean
make emu CONFIG=DefaultConfig -j"$(nproc)"
```

切换 `WITH_IOMMU` 前请清理 `build/`，避免复用另一种配置生成的 RTL。

Clean `build/` before changing `WITH_IOMMU` so that generated RTL from another
configuration is not reused.

## 其他仿真工程（Other Simulation Projects）

其他工程应将根目录的 `iommu_wrap.f` 作为仿真器的 filelist，并将其路径
解析为绝对路径或相对于 filelist 的路径。例如：

Other simulation projects should pass the root-level `iommu_wrap.f` to the
simulator. Use an absolute path, or preserve the filelist's repository-root
relative paths:

```make
OPENIOMMU_ROOT := $(abspath path/to/OpenIOMMU)
IOMMU_FILELIST := $(OPENIOMMU_ROOT)/iommu_wrap.f

# Add IOMMU_FILELIST to the simulator command line.
# Verilator: verilator ... -F $(IOMMU_FILELIST)
# VCS and other tools: use their filelist option with $(IOMMU_FILELIST).
```

集成方负责提供与目标平台匹配的宏定义。常用配置包括：
采用VCS仿真的时候，可以采用默认不打开IOMMU_IMPLEMENTATION的宏定义
使用DC综合/FPGA验证/VCS仿真的时候，可以打开IOMMU_IMPLEMENTATION

* `CONFIG_RISCV_IOMMU_BOSC_V2_LITE`：启用 XiangShan 使用的轻量配置；
* `IOMMU_IMPLEMENTATION`：启用 SRAM 实现选择逻辑；
* `RTL_RAM_SIM`、`FPGA_RAM_SIM`、`ASIC_RAM_SIM`：选择对应的 SRAM 模型。

The integrating project is responsible for target-specific preprocessor
definitions. Common definitions are:

* `CONFIG_RISCV_IOMMU_BOSC_V2_LITE`: XiangShan lightweight configuration;
* `IOMMU_IMPLEMENTATION`: enable SRAM implementation selection logic;
* `RTL_RAM_SIM`, `FPGA_RAM_SIM`, `ASIC_RAM_SIM`: select the corresponding SRAM
  model.

## 许可证（License）

OpenIOMMU 使用木兰宽松许可证第 2 版（Mulan PSL v2）。详情请参阅
[`LICENSE`](LICENSE)。

OpenIOMMU is licensed under the Mulan Permissive Software License, Version 2
(Mulan PSL v2). See [`LICENSE`](LICENSE) for details.
