# 跨境教育技术集成试点管理服务

面向广西—东盟合作院校的智能教学组件试点治理：统一登记组件制品，逐校建立本地化适配方案，
按阶段门禁收集证据推进试点，出现严重问题时冻结组合并跟踪回撤。

## 核心规则

- **制品登记不可变**：`artifact_id + revision` 唯一登记制品摘要、能力声明（课程数据格式 /
  语言包 / 运行能力）与依赖契约；同版本重复登记内容一致则复用，不一致则拒绝。
- **适配方案受声明约束**：各校适配方案（数据格式、语言包、运行时画像）不得超出制品能力声明；
  方案版本化并保留历史，试点记录钉住所用的方案版本，可随时追溯"用了哪份配置"。
- **试点幂等**：同一制品版本 + 学校的重复开设请求复用原记录（确定性 `pilot_id`）。
- **阶段门禁**：兼容检查 → 小规模验证 → 观察期 → 推广评审 → 已推广。每阶段要求关键指标
  齐全且达标、证据在有效期内（各阶段有效期 30/60/90/45 天），否则禁止晋级；
  验证失败可回退到前一阶段，当前阶段证据作废。
- **限时豁免**：缺失/不达标指标可由限时豁免覆盖，不同类别限定审批角色——
  数据格式 → `data_governance_officer`，语言 → `academic_director`，
  运行时 → `technology_director`，流程类 → `steering_committee_chair`；到期自动失效。
- **冻结与回撤**：上报严重问题即冻结该制品版本的全部组合、列出所有采用方并启动回撤；
  冻结组合禁止晋级与提交证据，回撤期间禁止新开试点（已有记录仍复用）；
  各采用方确认回撤后组合进入 `RECALLED` 终态，全部确认后回撤单完结。
- **重启续跑**：全部状态（含审计事件）原子落盘 JSON，进程重启后原试点可继续。

## 查询端点

| 端点 | 说明 |
| --- | --- |
| `GET /artifacts/{id}/release-board?revision=N` | 放行/阻断看板：逐校阶段、证据与阻断原因 |
| `GET /artifacts/{id}/adaptation-diff?revision=N` | 学校间适配差异（标记存在分歧的字段） |
| `GET /artifacts/{id}/recall-scope?revision=N` | 当前回撤范围：采用方清单与确认进度 |
| `GET /pilots/{pilot_id}` | 试点详情：门禁评估、钉住的方案版本、近期事件 |

操作端点：`POST /artifacts`、`POST /adaptations`、`POST /pilots`、
`POST /pilots/{id}/evidence|advance|rollback|sync-plan`、`POST /exemptions`、
`POST /recalls`、`POST /recalls/{id}/withdrawals`。

## 运行

启动服务（默认 `127.0.0.1:8080`，状态存于 `data/pilot.json`）：

    PYTHONPATH=src python -m integration_pilot.server --store data/pilot.json --port 8080

运行测试：

    python -m unittest discover -s tests -v

编译检查：

    python -m compileall -q src tests run_cli.py

命令行冒烟（含模拟重启续跑与回撤全流程）：

    python run_cli.py

## 代码结构

    src/integration_pilot/
      contracts.py   基础领域契约（制品/站点记录）
      models.py      阶段规则、关键指标校验器、可序列化领域模型
      errors.py      领域错误类型（稳定错误码）
      store.py       JSON 原子持久化
      service.py     试点管理服务（全部业务规则）
      server.py      标准库 HTTP 端点
