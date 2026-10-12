# 首批 RTL IOMMU 门禁

所有用例通过本次构建的 API/UT/DPI 库运行真实 OpenIOMMU RTL。
stub/refmodel smoke 和全 SoC RTL-system 不属于这些用例。

| ID | 成功断言 |
| --- | --- |
| picker-runtime | API open、复位、步进、capability `0x1f8c7e68610`、close，关闭后输出带 nonce 的 marker |
| rtl-apb | capability/reset 值；RO 写准确返回 PSLVERR 且不改变值；MSI data 读回；DDTP 高低 32 位读回 |
| rtl-dma-bypass | 显式开启硬件 bypass；非对齐/跨 32-byte beat/跨页数据；实际 GPA、数据、guard bytes/strobe 和读取内容 |
| rtl-dma-translate | 硬件 bypass 关闭；extended DDT、DID 1/2、PV=0/PDTV=0、S-stage Sv39、G-stage Bare；跨页写入正确 GPA；两页 A/D 和 ACE `awatop=0x13`；同 IOVA 的 DID 2 读到不同 GPA |
| rtl-dma-fault | 非法 DID/DC、未映射写、只读页写分别失败；FQ cause `258/15/15`，DID、TTYP=3、IOVA 准确；payload 未改变；无 FQ overflow/memory fault |
| rtl-invalidate | 缓存旧映射后修改 PTE；IOTINVAL.VMA + IOFENCE.C(PR/PW/AV)，CQH 达到提交值且无 CQ 错误；completion 写回；读写使用新 GPA，旧 GPA 未改 |
| qemu-linux-nvme-ext4 | 外置 RTL IOMMU + cmodel io-system；真实 shell 算术回应；1 CPU；导出 live DTB；NVMe block device 和 iommu_group；ext4 mount/ls/write/sync/umount/remount/内容比较/umount；QMP quit 后 QEMU 正常退出；无 fatal |

定向 memory callbacks 实现 32-byte lane 对齐和 byte strobe，translation/downstream
访问最终物理内存，记录实际地址。callback 不重新进入阻塞 API。
故障用例不会把超时当正常 fault：必须读取到对应 FQ record。
APB 用例要求只读 capability 写返回失败、寄存器值不变，并准确记录一次
`addr=0x00000000 data=0xffffffff` PSLVERR；其他 APB 错误仍阻塞门禁。

每个用例在新进程中创建 VCS 实例，避免不支持的重复初始化；不以 `$finish`
提前结束来冒充通过。运行器要求退出码 0 和独立的本次 nonce 成功行同时成立。
Linux guest 关闭 echo 后读取命令响应；目标文件在卸载/重新挂载后再次比较。
`qemu_to_iosystem` 没有 guest poweroff device；全部断言和最终卸载完成后通过
QMP `quit` 收尾，要求退出码 0。提前退出或必须强杀的进程不能通过。

Linux 启动前通过 QEMU generic loader 向 `0x311f1000` 写 `0x80000000`，显式
关闭 RTL 复位默认开启的 hardware bypass；mount 前和 I/O 后通过 QMP 读回。
TCG 使用 `-icount shift=3,sleep=off`，避免 VCS 阻塞宿主时间导致 guest RCU/timer
误报；实际 deadlock 仍由外层 wall-clock deadline 阻塞，RCU stall 不豁免。
当前固定 kernel 使用 writeq 写 32 位 BOSC sync pulse，产生最多两条
`APB write PSLVERR: addr=0x000002b4 data=0x00000000` 上半字诊断。
CI 单独记录这两条 boot compatibility diagnostics；其他地址、非零数据、
额外次数或 guest command 阶段的 APB 错误均失败，不豁免实际 DMA/fault。

初版覆盖不声称验证 ATS/PRI、S2、MRIF、全部 DDT/PDT 模式、CQ/FQ 环回压力或
失效筛选隔离。这些应在首批基线稳定后增加独立用例。
