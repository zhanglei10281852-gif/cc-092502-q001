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

## 测试

```bash
python -m pytest
```

测试覆盖数据库初始化、项目成员权限、会话撤销、审计脱敏、幂等写入和后台任务领取与完成。

## 田野上下文模块

`/api/projects/{project_id}/fieldwork` 提供探方、发掘单元（层位 layer、灰坑 ash_pit、古河道 channel、其他遗迹 feature）与遗迹关系的登记、封存与复核，编号（探方 code、单元 number）在项目内唯一并统一规范化为大写。

- 单元生命周期为 `open → sealed → reviewed`。记录者（recorder）只能修改 `open` 数据；封存会写入不可变快照版本及 SHA-256 内容摘要；封存后的纠错只能由 owner/researcher/reviewer 创建带原因的新版本，并回到待复核状态；复核人（owner/reviewer）不能审核自己提交的版本。所有变更以 `expected_version` 做乐观并发控制，冲突返回 409。
- 关系类型为 `cuts`（切叠）、`earlier`（早晚）、`equals`（等同）。等同按等价类收缩，切叠/早晚归一化为“晚于”有向边，插入时拒绝自环、矛盾边与可推导出的关系循环；重复提交同一关系幂等返回原记录。任一端点封存后关系冻结。
- 查询接口包括单元的直接关系（双向，`cut_by`/`later_than` 等视角）、传递关系（等同类、更早、更晚集合）、版本历史（含快照与摘要）；审计事件经 `/api/audit?project_id=&resource_type=&resource_id=` 按资源过滤，具备任一项目角色即可查询。
- 所有写接口接受 `Idempotency-Key` 头，重放返回首次响应，同键不同内容返回 409。`/import` 批量导入逐行报告错误，每行在独立保存点中执行，失败行整体回滚、不留半条关系，成功行正常落库。
- 并发版本冲突、失败回滚与重启后一致性由 `tests/test_fieldwork.py` 覆盖。

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
