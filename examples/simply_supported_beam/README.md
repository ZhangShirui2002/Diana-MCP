# 示例：简支梁线性静力分析

二维平面应力简支梁：跨度 4 m、梁高 0.4 m、厚 0.2 m，E = 30 GPa，ν = 0.2，跨中顶面作用 10 kN 集中力。网格为 0.05 m 的 8 节点四边形单元（CQ16M），共 640 个单元、2097 个节点。

## 结果对比

| 量 | DIANA | 理论解 | 误差 |
|---|---|---|---|
| 跨中底部竖向位移 | 0.4321 mm | 0.4287 mm | +0.8 % |

理论解采用 Timoshenko 梁：PL³/(48EI) + PL/(4κGA) = 0.4167 + 0.0120 mm，κ = 5/6。
荷载作用点处的位移为 0.4361 mm，略大，是集中力作用点附近的局部压陷所致。

## 非线性变体：素混凝土开裂

把材料换成 Total Strain 开裂模型（`TSCR`，转动裂缝，Hordijk 软化，fₜ = 2.5 MPa，G_F = 100 N/m，受压线弹性），另存为 `ss_beam_nl.dpf`。然后添加 `NONLIN` 分析，按 12 × 0.25 分级加载，最大迭代 20 次。用 `diana_solve` 后台求解（约 8 秒），`diana_job_status` 返回：

| 步 | 荷载系数 | 荷载 | 迭代次数 | 状态 |
|---|---|---|---|---|
| 1–5 | 0.25–1.25 | 2.5–12.5 kN | 0 | 线弹性，收敛 |
| 6 | 1.50 | 15 kN | 1 | 开始开裂，收敛 |
| 7–8 | 1.75–2.00 | 17.5–20 kN | 4、9 | 裂缝扩展，收敛 |
| 9 | 2.25 | 22.5 kN | — | 发散，`FATAL /DIANA/AP/LB40/0053`，作业中止 |

开裂出现在 12.5 kN 与 15 kN 之间，与理论开裂荷载 P_cr = 4fₜW/L = 13.3 kN 吻合（W = th²/6）。素混凝土梁在荷载控制下越过峰值后发散，属于预期行为。

注意：作业中止时 `diana.exe` 的退出码仍然是 0。作业状态必须根据 `.out` 中的 `DIANA-JOB ABORTED` 判断，`diana_job_status` 就是这样处理的。

## 用 MCP 复现

在接入了 `diana` 服务的 AI 客户端中：

1. `diana_start` 启动 DianaIE。
2. `diana_execute` 执行 [build_model.py](build_model.py) 的内容（先把 `PROJECT_DIR` 改成本机的绝对路径）。
3. `diana_solve(analysis="Linear static")`：后台调用 `diana.exe` 求解，求解期间 GUI 不会被占用。
4. `diana_job_status` 查看进度，完成后用 `diana_read_tb` 读取跨中节点组 `Mid_Bottom` 的位移。
5. `diana_load_results` 把 `.dnb` 结果加载回 DianaIE，再用 `diana_plot_result` 画位移云图。

## 建模要点（DIANA 10.5 实测）

- `attach()` 只会吸附到**最近的已有顶点**，不会自动印刻新点。集中力、点支座、节点组所在位置必须已经是几何顶点（本例把跨中上下两点写进了 `createSheet` 的轮廓）。
- `attach()` 的返回值不可靠：本例中支座的 `attach` 返回 False，实际却已挂上。应以导出的 `.dat`（`'SUPPOR'`、`'LOADS'`、`'GROUPS'` 段）或截图为准。
- 必须先设置单元类型（`setElementClassType`），并赋好材料和几何属性，`generateMesh` 才会真正划分网格，否则返回空列表。
- 中文版 DianaIE 的查询函数返回中文名（如 `modelDimensions()` 返回 `['二维']`），但设置函数仍使用内部关键字（`"2D"`、`"STRUCT"`）。
- `saveAnalysisCommands` 导出的 `.dcf` 只包含分析块。`diana.exe` 独立运行时还需要在开头补 `*FILOS / INITIA / *INPUT`，在末尾补 `*END`，`diana_solve` 会自动处理。
- `saveProjectAs` 必须写明 `.dpf` 扩展名，否则会生成一个没有扩展名的文件。另存之后 `currentProject()` 仍返回旧路径，而 `projectName()` 和 `saveProject()` 已经指向新文件。
