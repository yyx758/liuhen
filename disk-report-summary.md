# 磁盘扫描摘要

扫描时间：2026-11-21 18:43–18:56（原始报告：`C:\Users\29146\Desktop\disk-report.txt`，约 483 MiB）。这份报告记录的是**磁盘空间**，不是运行内存占用。以下数字为当时快照，单位 GB，目录与其子目录不能相加。

## 容量

| 分区 | 已用 | 剩余 | 关注点 |
| --- | ---: | ---: | --- |
| C: | 187 | 13 | 空间紧张，优先清理 |
| D: | 314.6 | 437.1 | 空间充足 |

## C 盘主要占用

- `C:\Users` 110.46 GB，其中当前用户的 `AppData` 90.54 GB。
- `C:\Windows` 36.29 GB；`C:\Program Files` 22.09 GB；`C:\Program Files (x86)` 7.58 GB。这些属于系统或安装内容，不直接手工删除。
- `C:\Users\29146\AppData\Local\Claude-3p` 12.83 GB，其中 `rootfs.vhdx` 8.48 GB、`sessiondata.vhdx` 2.63 GB。它们可能承载应用环境或会话数据，处理前确认用途。
- `C:\Users\29146\AppData\Roaming\LarkShell` 12.17 GB，可能含飞书应用数据；先在应用内检查缓存和下载内容。
- Chrome 用户数据 10.13 GB；其中可能有浏览记录、配置与扩展数据，优先用浏览器自带的清理功能。
- `C:\Users\29146\AppData\Local\npm-cache` 6.71 GB；属于较明确的开发缓存候选项，可用 npm 的缓存管理命令处理。
- C 盘回收站 5.4 GB；确认没有需要恢复的文件后可清空。
- `C:\Users\29146\.cache` 2.24 GB、JetBrains Local 3.38 GB、`.gradle` 0.99 GB、`.m2` 0.84 GB、pip 缓存 0.43 GB、uv 缓存 0.30 GB：需按各工具的缓存机制清理，避免误删项目或依赖配置。
- 临时目录 `AppData\Local\Temp` 0.43 GB。原始报告本身约 0.47 GB，也位于 C 盘桌面。

## D 盘值得核对的大文件

- Docker 当前虚拟磁盘 `D:\DockerData\DockerDesktopWSL\disk\docker_data.vhdx` 45.08 GB；不要直接删除。
- Docker 迁移前备份 `D:\DockerMigrationBackup\docker_data_pre_migration_20260816.vhdx` 38.63 GB；确认已不需要回滚后，是 D 盘的明显清理候选。
- `D:\pagefile.sys` 22 GB，属于系统分页文件，不手工删除。
- `D:\develop\Models\blobs` 中有多个约 3.83–5.56 GB 的模型文件；按实际使用情况管理。

## 建议顺序

1. 先确认 C 盘回收站 5.4 GB 中没有待恢复文件，再清空。
2. 清理 npm 等明确的缓存，以及浏览器和应用内提供的缓存清理项。
3. 检查 Claude-3p 与 LarkShell 的实际数据用途，再决定是否移除旧环境或历史数据。
4. 保留本摘要后，可删除桌面的原始扫描日志，约回收 0.47 GB。

原始报告的“虚拟磁盘”命令实际输出了约 168 万条普通路径，导致文件膨胀到约 483 MiB；上述虚拟磁盘条目是按 `.vhd` / `.vhdx` 后缀重新筛出的。扫描数字未经重新测量，清理前应以当前占用为准。
