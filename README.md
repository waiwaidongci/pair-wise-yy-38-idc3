# 水库防汛调度与操作确认

根据库位、入库流量、下游警戒和施工限制生成复核授权的泄洪指令。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、回执一致性和关闭不变量。
- `src/policy.py`：授权冻结与库位变化后的每孔开度目标重算。
- `src/repository.py`：SQLite建表、事务、版本控制、并发唯一约束和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败、闸门批次和HTTP测试。

## 指令状态与可续作闸门批次

状态链为 `draft → checked → authorized → executing → executed → closed`。
“点一次执行即整条完成”被禁止：总工授权时冻结每孔开度目标并开出**唯一执行批次**，
调度员派工，值班员逐孔登记实际开度，全部孔到位且回执一致后批次才完成，总工最后关闭。

- 授权时按闸门清单冻结目标（`gate_code` + `target_opening`），并记录依据与容差。
- 两个值班员同时提交同一孔：同派工轮次内由数据库唯一索引保证先到结果生效，后到返回409。
- 现场回传失败（`feedback-lost`）后按**同一批次**续办；同一回执携带 `client_token`
  重试不会重复追加。
- 某孔拒动（`refused`）时指令留在 `executing`；恢复后续办只针对未到位孔开新一轮，
  已到位孔不重做、不重复登记。
- 库位变化后总工可按新依据重算：未到位孔重算目标并重置，已到位孔保留记录不动。
- 全部孔都有一致到位回执、且没有未关闭事项（含open记录）时，总工才能关闭责任链。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8315
```

默认端口为`8315`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`（只支持 checked/authorized 常规复核）
- `GET /api/items/{id}/batch`：查看批次、逐孔状态、派工轮次与回执
- `POST /api/items/{id}/batch/authorize`：总工授权冻结目标，指令进入 executing
- `POST /api/items/{id}/batch/dispatch`：调度员首次派工
- `POST /api/items/{id}/batch/continue`：恢复后续办剩余孔（已到位不重做）
- `POST /api/items/{id}/batch/receipts`：逐孔回执 `{gate_code,outcome,actual_opening,client_token}`
- `POST /api/items/{id}/batch/feedback-lost`：登记回传失败
- `POST /api/items/{id}/batch/recompute`：库位变化后按新依据重算未到位孔
- `POST /api/items/{id}/batch/settle`：全部一致到位后批次完成
- `POST /api/items/{id}/batch/close`：总工关闭（必须提交 `expected_version`）
- `GET /api/audit`

允许角色：duty_officer, chief_engineer, dispatcher, viewer。库位超过汛限或入库流量上升时提升紧迫度；授权前必须有复核记录，执行后仍要闭环现场反馈。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
