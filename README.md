# 工业实验数据账本

本项目是可在本地运行的 FastAPI 服务，用于管理实验规格、采集批次、质量门禁、组间对照分析、评审和不可变发布结果。仓库中的实验数据均为合成样本。

## 启动

在运行服务之前安装 requirements.txt 中锁定的依赖。设置 ATELIER_DB 为可写的 SQLite 路径，然后从项目根目录运行 uvicorn atelier.api:app --host 127.0.0.1 --port 8000。交互式接口文档位于 /docs。

实验规格定义分组、对照组、指标单位、数值边界、精度和质量阈值。批次使用稳定标识；相同内容重复提交会返回原结果，不同内容使用同一批次标识会被拒绝。无效行保留拒收原因。修正和作废样本时需提交当前内容摘要。有效数据变化会推进活动版本，旧分析仍可查阅，但不能作为新的评审或发布依据。

分析结果冻结来源版本与内容摘要。发布需两个不同评审人同意，由第三人执行；任何否决都会阻止发布。证据导出和对账接口可核对批次、样本、分析和发布内容。

## 配方版本与可比性

组名只描述实验角色，不能单独决定两次测量能否合并。配方通过 `POST /campaigns/{campaign_id}/formulations/{formulation_id}` 按版本登记，版本携带名称、参数和可比性依据（basis），并可声明父版本。样本提交时必须同时给出 `formulation` 版本引用（如 `{"formulation_id": "coat", "version": 2}`）；一旦活动谱系中出现版本，缺少引用或引用不可用版本的样本行会以 `missing_formulation`、`unknown_formulation` 被拒收并保留原因。

缺失父版本、循环继承和同号不同定义会在写入前被拒绝（`missing_parent`、`lineage_cycle`、`definition_conflict`），因此不会进入可用谱系。父版本可以通过 `PUT /campaigns/{campaign_id}/formulations/{formulation_id}/{version}/lineage` 调整，调整带 `expected_lineage_revision` 乐观锁、循环检查和审计事件。`GET .../formulations` 列出各版本及其适用的可比规则，`GET .../formulations/{id}/{version}/ancestors` 返回冻结前的祖先快照。

可比规则通过 `PUT /campaigns/{campaign_id}/comparability-rules/{rule_id}` 声明并带修订号：`basis` 相同的版本可比；`include_lineage` 可再按谱系距离放宽。规则修订历史可通过 `GET .../comparability-rules/{rule_id}/history` 复核。

`POST /campaigns/{campaign_id}/cohorts/{analysis_id}` 按所选规则解释配方关系并生成队列分析。每个版本簇单独报告分组统计和对照；没有共同可比依据的版本不会被拼成一个均值，结果中的 `combinable` 为 `false`。每个窗口内样本都有 `membership` 入选/排除理由（`voided`、`unbound_formulation`、`unknown_formulation`），缺少有效对照的层会以 `no_valid_control` 门禁列出。分析冻结规则修订、谱系快照和成员内容摘要（`cohort_sha`）。仍按组名解释的活动分析若窗口内出现多个配方版本，会以 `multiple_formulations` 门禁阻止评审，必须改用队列分析。

`GET /campaigns/{campaign_id}/cohorts/impact` 在调整继承关系或可比声明后重新找出受影响的队列、缺少有效对照的层、待发布结论以及需要重新发布的已发布结果；`GET .../cohorts/{analysis_id}/status` 给出单个队列的当前性。评审和发布绑定到分析时的 `cohort_sha`，旧审核不能批准更换了队列的结果。已发布结果保持原样，`GET .../releases/{release_id}/lineage` 可复核历史报告当时依据的谱系和规则修订。独立作废的测量不会因配方重新归类而恢复。

## 离线演示与检查

python -m atelier.cli --fixture fixtures/synthetic_experiment.json 会在临时数据库导入合成批次，计算对照指标，取得两份评审并输出发布摘要；demo 不访问网络。`python -m atelier.cli --fixture fixtures/formulation_lineage.json` 演示配方版本谱系、可比规则和分簇队列分析（两个版本没有共同依据时分别报告，不合并均值）。业务测试命令是 python -m unittest discover -s tests -v；语法检查命令是 python -m compileall -q atelier。

测量值使用十进制文本，时间戳必须包含明确的 UTC 偏移，分析窗口采用左闭右开区间。SQLite 事务串行化写入操作，并保留审计事件。
