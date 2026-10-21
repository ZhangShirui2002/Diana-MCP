# DIANA 10.5 用户手册对照复查（更新至 diana-mcp 0.9.0）

复查依据是本机安装的 DIANA 10.5 完整手册（`share\Man\HTML\diana-10.5.qch`，共 1323 页），逐部分对照 MCP 的功能，并用真实求解器验证了未在手册中写明的格式。

## 结论

- **所有功能都有调用途径。** 三条通路互为补充：
  - `diana_execute` 可以在 DianaIE 中执行任意脚本命令，包括手册附录 B 的 552 条，以及手册未收录的另外约 311 条（报告、动画、CSV 导出等）。
  - `diana_run_solver` 能运行手写或导出的 `.dat`/`.dcf`，覆盖附录 C–I 的全部批处理语法；从 0.9.0 起只用于已认证基准模型或已批准参数变体，不能绕过建模阶段的 GUI 校审。
  - 手册第 V 部分的 25 类分析都能通过 `diana_solve` 在后台求解。前提是共用同一 `.ff` 数据库的多个分析块属于同一个 DianaIE 分析，或者用 `restart_ff` 在已有 `.ff` 上续算。
- **缺口集中在便利层和健壮性**：结果格式识别、批处理文件的边界情况、AI 可用的关键字知识。本轮已按优先级修复了大部分，剩余项列在最后。

## 一、`.tb` 取消分页

手册附录 I.1.1.2 “Options for Tabular Output”（`Theory/Theoryse738.html`）：

```
BEGIN OUTPUT
  TABULA
  BEGIN LAYOUT
    LINPAG 0            ← 每页行数；0 或负数 = 不分页（默认 66 行一页，每页重复页眉和列头）
    COLLIN 1000         ← 每行字符数；表宽超过它（默认 130）时每行会折成多行
    DIGITS RESULT 8     ← 结果有效位数 1–10（默认 4 位，如 2.385E+01）
    COMBIN              ← 可选：多个输出项合并成一张表
  END LAYOUT
  ...
END OUTPUT
```

- **DianaIE 中的写法**（经实测确认；手册只给出关键字，没有写明命令树路径）：
  - `addAnalysisCommandDetail(a, c, "OUTPUT(n)/LAYOUT")`
  - `setAnalysisCommandDetail(a, c, "OUTPUT(n)/LAYOUT/LINPAG", 0)`
  - `addAnalysisCommandDetail` + `setAnalysisCommandDetail(a, c, "OUTPUT(n)/LAYOUT/DIGITS/RESULT", 8)`
- **只设一个选项时的写法差异**：DianaIE 会把它写成单行的 `LAYOUT LINPAG 0`。
- **后台求解的默认行为**：`diana_solve` / `diana_run_solver` 默认在作业用的 `.dcf` 副本中写入 `LINPAG 0` 和 `COLLIN 1000`；`tb_digits` 参数控制有效位数。
- **不改的情况**：原文件和项目本身都不会被修改；`*DESIGN` 的输出块也不改，因为 DIANA 不允许为设计校核指定版面（附录 I.2）。

## 二、按手册各部分的覆盖情况

| 手册部分 | MCP 的调用方式 | 说明 |
|---|---|---|
| I 概述；第 4 章 DianaIE；第 5 章单位 | `diana_execute`；`diana_model_summary` 列出当前单位 | DIANA 没有固定单位制。说明中已要求 AI 先看 `unit(q)`，并保持一致的单位制 |
| II 建模：几何、荷载、连接、网格 | `diana_execute`，配合 `diana_keywords` / `diana_parameters` | 只有批处理语法的能力须纳入 GUI 校审过的基准，或在人审参数方案中明确授权后，才可交给 `diana_run_solver` |
| III 单元（附录 A、F） | `setElementClassType` + 网格划分器 + 单元阶次 | 直接指定单元类型只能在 `.dat` 中完成 |
| IV 材料（附录 G） | `addMaterial(类别, 模型)` + `setParameter` | 类别、模型和参数树的关键字手册中没有列出，已从安装目录的 `lib\SCHEMA` 建立索引（`diana_keywords`） |
| V 分析（附录 H，25 类） | `diana_solve` / `diana_run_solver` | 续算（`RESTOR`）以及单独运行 `*REDUCT`/`*ELIQUE`/`*ECREEP` 时，用 `restart_ff` 在已有 `.ff` 上进行 |
| VI 结果（附录 I） | `diana_read_tb`、`diana_results_table`、`diana_load_results`、`diana_plot_result` | `.tb` 的各种版式已用真实求解器逐一验证，见第四节 |
| 附录 B 脚本命令 | `diana_api_search` / `diana_api_doc` | 被兼容层包装、自身没有文档的命令（如 `addDirection`）已补上原函数的文档 |
| 附录 C 批处理运行 | 后台作业 | `-n` 只控制单元循环的线程数；求解器线程数由 `.dcf` 中 `SOLVE PARDIS … MAXTHR` 设置 |
| 附录 J 用户子程序 | 部分支持 | `*FORTRAN USE "x.dll"` 引用的 DLL 会被检查并复制到作业目录；编译 DLL 需要 Intel Fortran，MCP 不提供 |
| 附录 K Communicator | 作业中关闭 | 它只做本地存储，不能远程提交作业。作业设置 `COMMUNICATOR_CONNECTION=server=NONE`，避免误杀 DianaIE 正在使用的进程 |
| 附录 L/M FX+、NASTRAN 转换；CAD/BIM 导入 | `diana_execute` 调用 `importModel` / `importGeometry` | 这些命令会丢弃未保存的修改，说明中已提醒 AI |
| 验证手册、官方示例 | `diana_manual_search` | 已索引 127 个教程脚本和 62 个验证算例（`.dcf`/`.py`） |

## 三、本轮修复清单

### 结果输出（`.tb`）

- **按列位置对齐**，不再按空格计数。以下格式因此都能正确解析：
  - COMBIN 合并表中留空的单元格；
  - COOR 追加的坐标列、AXES 输出的主方向余弦；
  - 极值表中每个数值后的工况注记，以及块头里的 `Extreme values` 汇总行；
  - 超过 `COLLIN` 后折成多行的表；
  - 首列不是已知编号列的表：这类表以前会被整块静默丢弃。
- 新增块头：`Mode nr.`、`Eigen frequency`、`Element type`、`Extreme results`；可以按模态筛选，也可以按任意编号列筛选（`ids`，如 `{"Reinnr": [3]}`）。
- 索引阶段只数行、不做逐列对齐：78 MB 的文件建索引约 1.3 秒；读取时按字节偏移直接跳到对应的块。
- 新增 `diana_results_table`：从 DianaIE 中已加载的结果取数，精度为完整双精度，并支持钢筋、桩、分层结果。`diana_plot_result` 增加了 type、location、layer 参数。

### 作业与批处理

- **表头插入位置**：`*FILOS / INITIA / *INPUT` 插在 `:` 注释行和控制命令（`LOG`/`NUMTHR`/`ERRORS`）之后；已有 `*INPUT` 时不再重复添加。
- **续算**：`restart_ff` 参数用已有 `.ff` 续算，此时 `.dcf` 按原样使用，`.ff` 保留不删。
- **外部文件引用**：识别用户 DLL、`*INPUT READ FILE=`、`IMPORT` 引用的文件，找得到就复制进作业目录，找不到则在提交前报错。
- **输出文件清单**：按本次运行的时间列出全部输出文件，包括 `FILE=` 指定的文件（DIANA 会把这些文件名转成大写）。
- **communicator 隔离**：作业进程中关闭 communicator。
- **模型校验与人工门禁**：`diana_solve` 无授权时只打开 DianaIE 审阅窗口，不求解；人工批准后再运行 `validateModel('ALL')` 自检并提交一次性许可。基准候选还须通过验证求解的正常结束、错误和收敛检查。

### 求解日志（`.out`）

- **误判防护**：阶段计时行的数字溢出（如 `99.-FA`）不再导致作业被误判为失败。
- **新增识别内容**：
  - `.dcf` 语法错误（`Unexpected input` / `Last line was` / `SCAN ERROR`）；
  - 多行错误消息，其中包含 DIANA 给出的处理建议；
  - 特征值和频率列表（含 D 指数）、7 张模态表（参与系数、有效质量等）；
  - 强度折减的安全系数。
- **步统计**：步重试（自动减小步长后重算同一步）按最后一次尝试计数；步号重新从 1 开始时判定为进入下一阶段。
- **进度**：根据回显的 `STEPS EXPLIC SIZES` 计算计划步数，显示为“已完成步数/计划步数”。

### 建模知识

| 工具 | 作用 |
|---|---|
| `diana_keywords` | 检索 SCHEMA 中的关键字：12,800 个条目，含标题、量纲、取值范围、默认值和枚举 |
| `diana_parameters` | 调用 `availableParameters` 列出对象的全部参数路径和当前值，并附 SCHEMA 说明 |
| `diana_analysis_commands` | 把某个分析导出为 `.dcf` 文本，用来核对命令树路径的设置效果 |
| `diana_command_log` | 读取 DianaIE 的命令历史；用户在 GUI 中做一次操作，AI 就能看到对应的准确调用 |
| `diana_model_summary` | 增加当前单位和 `validateModel` 的结果 |

## 四、用真实求解器验证过的格式

以简支梁模型为基础，逐一实跑并存为测试夹具（`tests/data/formats`）：
- 线性静力、非线性（开裂直至发散）、特征值分析；
- `COMBIN`、`COOR`、`AXES`、`LOADS … MIN` 极值表、`DIGITS 10`；
- 默认 `COLLIN` 下的折行表与 `COLLIN 250/1000` 的对比；
- 裂缝状态文本表（`STATUS CRACK`）；
- 关闭 communicator 后的求解。

## 五、尚未覆盖或已知的限制

- **用户子程序的编译**：需要 Intel Fortran 和 DIANA 命令行环境，MCP 只负责检查并复制编译好的 DLL。
- **没有专门的 `.dat` 表格编辑工具**：Claude Code 可以直接编辑文件；在没有文件工具的客户端里，可以通过 `diana_execute` 读写文件。
- **NDIANA ASCII（`.dna`）不解析**：它的格式没有公开文档，可以改用 `diana_results_table` 读取 `.dnb`。
- **APPEND 追加的多次运行**：写入同一个 `.tb` 时，索引会合并重复的步号。
- **`.tb` 块头精度**：块头里的荷载系数固定为 4 位有效数字，精确值应以 `.out` 为准。
- **进度百分比**：只有 `STEPS EXPLIC SIZES` 这种写法能算出计划步数；弧长法、自动步长等只能显示已完成的步数。
- **求解器线程数**：`MAXTHR` 需要写在 `.dcf` 中；`-m` 监控和 `.ff` 存放位置两个选项没有开放。
- **DIANA 自带的测试算例**：`share\Test` 中的 3,558 个 `.dcf` 数量过多、重复度高，没有纳入检索。
- **GUI 专属交互**：鼠标拾取、模态对话框等无法通过脚本完成。可以用 `selectedShapes()` 读取用户在 GUI 中的当前选择。
  - 反方向在 0.4.0 中已经打通：用户在 GUI 里做的操作，可以通过 `diana_changes`、`diana_command_log` 和跟随模式看到，包括撤销了什么、参数的新旧值，详见 README。
