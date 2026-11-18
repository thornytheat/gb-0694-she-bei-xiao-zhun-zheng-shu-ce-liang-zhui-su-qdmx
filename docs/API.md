# 接口

`POST /api/run`：JSON中的csv文本，省略使用样例；或multipart字段file。最大5MB。

`POST /api/m1`至`POST /api/m7`：独立运行到指定模块，JSON结构同运行接口。

`GET /api/history`：历史批次。`POST /api/review`接收id、revision和decision（通过或退回）。`GET /api/evidence.csv`提供单一证据导出。

结构错误422，版本变化409，文件过大413；错误返回code与message。所有页面可从左侧模块入口访问。

`GET /api/runs/<id>`读取指定批次；页面与证据CSV通过run_id查询参数绑定批次，模块导航保留该参数。

JSON请求体必须是对象，数组、标量、null、损坏JSON及错误上传字段返回400，且不运行默认样例。

## M8 证书纠错补录（对象级纠错）

纠错是挂在原回执下的独立对象，不改写原批次身份、裁决与复核状态；仅对纠错 CSV 涉及证书及其追溯链下游的测量重判，未变测量沿用原结论，受影响测量先停用、确认后更新。全程本地离线复算，不接外部源。

- `POST /api/m8/corrections`：JSON `{run_id, csv}` 或 multipart（`file` + 表单字段 `run_id`），提交纠错 CSV（同主输入 14 列）。幂等：身份为原回执 id + 规则版本 + 纠错原文摘要，重复提交返回同一纠错且 `reused=true`，不生成第二份。
- `GET /api/m8/corrections?run_id=<id>`：列出某原回执下的纠错；`GET /api/m8/corrections/<correction_id>`：纠错详情。
- `POST /api/m8/corrections/<id>/confirm`：`{revision, decision}`，decision 为 `通过`（停用测量按修正证书更新）或 `退回`（维持停用）。乐观锁，陈旧 revision 返回 409。
- `POST /api/m8/corrections/<id>/rollback`：`{revision}`，回退纠错并恢复旧版复核状态，原批次 revision/decision 不变。
- `GET /api/m8/effective?correction_id=<id>`：有效裁决视图，合并原回执与纠错状态，标注停用/更新/回退去向。
- `GET /api/m8/evidence.csv?correction_id=<id>`：纠错证据导出，逐对象给出原回执 ID、纠错 ID、纠错范围、原/修正结论与停用去向。

纠错状态机：`停用待确认 → 已确认更新 / 退回停用 → 已回退`。每步落库并记录 checkpoint，中断后凭已持久化状态续跑，不重复施加副作用。合并采用按 `cert` 替换同编号原行的对象级合并，避免同证书重复定义触发冲突；纠错新增隔离以 `C1..Cn` 来源行标记。页面入口 `/work/8?run_id=<id>&correction_id=<id>`。
