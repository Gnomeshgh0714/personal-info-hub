# personal-info-hub

A personal information hub that collects facts from your own files — resume LaTeX, project READMEs, award records, skill packs — into an append-only event log, with every number mapped back to its source file and line number. Pure Python standard library; editor hooks fail open.

个人信息中台：把散落在各处的个人经历事实（简历、项目 README、获奖材料、技能包）按白名单采集进一份追加式事件日志，每个数字都能回溯到源文件行号。纯 Python 标准库实现，hook 恒不阻断主流程。

## 解决什么问题

经历材料散落在多个目录：简历 tex 一份、项目 README 一份、获奖扫描件一份。写简历或申请材料时临时翻找，数字凭记忆填写，填完无法回溯出处。本工具把这些材料按白名单定点采集，产出结构化条目与分类视图；每个量化值登记数字级行号定位，定位不到的标「无源待确认」，等人核验。

它不做的事：不生成简历话术，不自动判断事实真伪，不整盘遍历。

## 三条铁律

1. **append-only**：条目只追加进 `中台数据/personal_info.jsonl`，同一 id 的更新以新行落盘、版本递增，下游按「同 id 取最后一行」聚合，旧行不删不改；内容哈希不变则重跑不追加（幂等）。
2. **证据映射**：每条事实带源文件路径与行区间，每个量化值带数字级行号；定位不到的数字写进 `无源量化值` 并标「无源待确认」，永不自动转正。「已核验」只能由人置。
3. **fail-open**：两个 hook 对任何输入（坏 JSON、非对象、缺字段、不存在的 transcript）都 exit 0；采集异常只记日志，不阻断 Agent 主流程。配套边界：只写本仓、绝不改源文件。

## 架构

```
collector/sources.json（白名单源根，人工配置）
        │
        ▼
collector/collect.py ── 四个提取器
  resume_tex（简历 tex 拆条） / readme_oneliner（项目一句话）
  doc_auto（md/tex/html/xlsx/docx） / skill_registry（技能包登记）
        │
        ▼
中台数据/personal_info.jsonl（append-only 事件日志）
        │  同 id 取最后一行聚合
        ▼
中台数据/六分类视图.md + 待核验队列.md + 采集日志.md

collector/state/（游标、id 映射、hook 事件日志；gitignore）
        ▲
hook_session_start.py（SessionStart → --incremental 增量采集）
hook_stop.py（Stop → transcript 线索登记）
```

## 快速开始（demo）

要求：Python 3.10+，纯标准库即可运行；只有解析 xlsx/docx 时才需要可选的 openpyxl / python-docx。

```bash
git clone https://github.com/Gnomeshgh0714/personal-info-hub.git
cd personal-info-hub
python3 collector/collect.py --full --dry-run   # 先看计数与源根明细，不落盘
python3 collector/collect.py --full             # 落盘
python3 collector/tests/regression_guard.py     # 回归护栏，exit 0 为全过
```

demo 配置指向 `examples/demo-sources/` 下的合成示例（人物、机构与数字全部虚构）：两份简历 tex、一份项目 README、一份获奖记录、一个技能包。跑完得到 10 条条目、六份分类视图、待核验队列与采集日志；再跑一遍 `--full` 新增为 0（内容哈希幂等）。保研版简历的 GPA/排名条目会自动带上场景警示标签，求职版的同类条目不带——这是「多源按场景并存 + 配置化警示」的演示。

## 配置自己的源

编辑 `collector/sources.json`，把 demo 源换成自己的目录：

- `根`：源根目录，绝对路径或相对仓库根的路径。采集器只访问白名单内的源根，绝不整盘遍历。
- `扩展名` / `仅文件名` / `排除目录` / `最大深度` / `子路径`：控制采集范围。
- `提取器`：`resume_tex` / `readme_oneliner` / `doc_auto` / `skill_registry` 四选一。
- `场景警示规则`（可选）：`场景含` 匹配场景标签、`文本匹配` 正则命中条目文本时，把 `警示` 文案写进条目。不配规则则不打任何警示。
- 涉密源可配 `仅元信息: true`：xlsx 只登记工作表名与行列规模，不抄单元格值。
- 二进制与扫描件（pdf/图片等）配进 `登记扩展名`：只登记路径与元信息，状态标「需OCR人工录入」。

改完先 `--full --dry-run` 看计数与源根明细，确认范围符合预期再落盘。字段全集与口径见 `schema/条目模板.md` 与 `docs/architecture.md`。

## Qoder hook 接线（可选）

在仓库根的 `.qoder/settings.local.json`（已被 gitignore）里注册：

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "python3 /path/to/personal-info-hub/collector/hook_session_start.py", "timeout": 120}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "python3 /path/to/personal-info-hub/collector/hook_stop.py", "timeout": 45}]}
    ]
  }
}
```

SessionStart 跑增量采集（脚本内部超时 100s，小于外层 120s，超时会写进事件日志供排查）；Stop 登记 transcript 口述线索计数（「线索 → 候选条目」的抽取尚未实现，不会产生条目）。临时停用：把脚本顶部的 `ENABLE_INCREMENTAL` / `ENABLE_SESSION_EXTRACT` 改回 False，仍会写事件日志、仍 exit 0。触发证据看 `collector/state/hook_events.jsonl`。其他支持 stdin 事件 JSON 的 Agent 工具可按同样方式接线。

## 隐私纪律

- **采集产物默认不入库**：`.gitignore` 已排除 `中台数据/`、`待核验队列.md`、`采集日志.md`、`collector/state/` 与 `.qoder/settings.local.json`。跑起来之后这些文件里是你自己的真实经历，默认不会被 git 追踪；本仓库只含引擎、配置模板、合成示例与文档。
- **白名单之外不采集**：采集器只访问 `sources.json` 列出的源根；源文件一律只读，不修改、不移动、不删除。
- **核验是人的动作**：采集器产出的条目一律「待核验」；只有人工在 `待核验队列.md` 底部裁决区留下结论后，才允许把状态改为「已核验」。
- **中台不复制源本体**：只存结构化文本条目与路径引用，二进制与扫描件只登记路径与元信息。

## 目录结构

```
collector/
  collect.py               采集器（扫描、抽取、落盘、视图生成）
  sources.json             源清单白名单（demo 配置，兼作配置模板）
  hook_session_start.py    SessionStart hook（fail-open）
  hook_stop.py             Stop hook（fail-open）
  tests/regression_guard.py 回归护栏（改抽取规则前后必跑）
  state/                   运行时状态（gitignore，跑后生成）
schema/条目模板.md          人写条目与机读字段的对照模板
examples/demo-sources/     合成示例源（全部虚构）
docs/architecture.md       设计口径详解
AGENTS.md                  AI 编码工具知识索引
中台数据/                   采集产物（gitignore，跑后生成）
```

## License

[MIT](LICENSE)