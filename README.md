# 考古研究协作基础服务

这是一个供考古项目扩展业务模块的纯后端基础服务，提供研究项目登记、成员与角色、会话认证、审计事件、幂等请求和可恢复后台任务。服务使用 FastAPI 与 SQLite，不依赖另行部署的数据库、缓存或队列。

## 环境与安装

运行环境为 Python 3.11。安装开发依赖：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

基础接口包括 `/api/system/health`、`/api/projects`、`/api/users`、`/api/sessions`、`/api/audit` 和 `/api/jobs`。首次启动后可用命令行创建管理员，也可以通过测试夹具构造隔离数据库。

## 田野上下文模块

路径前缀 `/api/projects/{project_id}/field`，覆盖探方（trench）、发掘单元（excavation_unit）、层位（layer）、遗迹（feature）与古河道堆积（paleochannel）的登记、封存、纠错与关系复核。

- 单元编号在项目内唯一；`owner/researcher/recorder` 可登记与修改，`reviewer/viewer` 只读。
- 未封存（draft）单元可就地修改草稿；封存（`POST /units/{code}/seal`）生成带 SHA-256 摘要与哈希链（`prev_digest`）的不可变快照。封存后的纠错只能 `POST /units/{code}/corrections`（必须带 `change_reason` 与 `base_version`）追加新版本，乐观锁冲突返回 `version_conflict`。
- 关系类型：`earlier`（早于）、`cuts`（切叠/打破，切叠者年代更晚）、`equivalent`（等同）。关系先以 pending 提交，复核人不能审核自己的提交；拒绝自环、与现有年代序矛盾的边、可推导出的冗余边以及任何年代序循环。pending 边不参与推导，驳回后可修正证据重新提交。
- 查询：`GET /units/{code}/relations`（直接关系，入边/出边双向）、`GET /units/{code}/transitive`（按已批准边推导的传递闭包）、`GET /units/{code}/versions`（版本历史与快照摘要）、`GET /units/{code}/audit`（审计事件）。
- `POST /import` 整批原子导入单元与关系：逐行返回错误（`error.details.lines`），任一行失败整体回滚，不留半条关系。
- 写接口支持 `Idempotency-Key` 请求头：相同键相同请求体返回首次结果（响应中标注 `idempotent_replay`），相同键不同请求体返回 409。

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内检查根路径、健康接口、数据库外键和 WAL 配置。

## 扩展约定

新研究模块应通过独立路由、服务和仓储接入，跨表写入放在即时事务中。外部标识、幂等键和审计载荷应保存原始值及规范化值；后台任务使用 SQLite 租约，不允许依赖外部队列。用户口令和会话令牌只保存摘要，审计事件会过滤密码、令牌等敏感字段。
