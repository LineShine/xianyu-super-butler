# 部署与凭据加密验收记录

验收日期：2026-11-05。环境：当前 Apple Silicon Mac；容器测试使用本机 Docker Desktop 的 Linux 容器环境。Windows 仅交付框架。

## 当前 Mac 实例

- 旧的应用与数据库合并管理器已拆分；首次升级进行了一次受控基础服务重启。
- 原 MySQL 实例 UUID 和账号数量保留；现有 2 个账号、4 条 IM Token 记录完成加密迁移，并通过逐项解密校验。
- 已实际完成原生 → Docker → 原生切换。正常切换前后 MySQL UUID、拆分后的 Redis run ID 保持一致；临时验证键的值与自然剩余 TTL 保持连续，验证键已清理。
- Docker 共用模式只运行四个应用容器，连接现有本机数据库，没有为当前实例建立第二套 MySQL / Redis。
- 最后恢复原生模式并关闭 Docker Desktop；前端和三个后端均返回健康响应，2 个账号的消息连接均处于 connected 状态。
- 当前配置为 `database_provider=native`、`application_mode=native`。实例与密钥保持原样；主密钥存放在源码目录之外，权限为 600。

当前实例的非敏感检查结果保存在本机 `yutaixian_data/local_dev/upgrade-verification.json`。该运行目录不提交到版本库。

## 自动化与隔离集成测试

| 检查 | 结果 |
| --- | --- |
| 后端业务回归 | backend-web 124 项、websocket 137 项、scheduler 12 项全部通过 |
| 根目录单元测试 | 共 23 项；普通运行通过 21 项，另 2 项依赖本机数据库的测试在显式启用后通过 |
| 凭据测试 | 随机加密、密文篡改、实例/账号/字段绑定、账号归属变更、缺失密钥、权限检查、日志脱敏通过 |
| 部署失败保护 | Docker 不可用、容器连接预检查失败、密钥缺失、实例提供方式冲突、独立生命周期检查通过 |
| 原生 → Docker → 原生集成 | 独立临时 MySQL / Redis 上创建并修改测试订单，两次切换均可见最新数据；MySQL UUID、Redis run ID、键值及自然 TTL、同一密钥解密、共享文件访问均通过 |
| API 集成 | 两种运行模式均通过密码/Cookie/代理密码不回显、保留/替换/清除、内部登录鉴权与会话归属检查 |
| 重复运行保护 | 不同运行代次的服务竞争同一实例所有权锁时被拒绝 |
| 全新 Linux 容器组合 | MySQL + Redis + 四个应用启动通过；应用重启后数据库身份、测试订单、Redis 值及自然 TTL 保留 |
| 构建与静态检查 | 前端生产构建、Python 编译、Shell 语法、git diff 空白检查通过 |

集成测试使用合成账号凭据，不向真实买家发送消息，也不创建真实交易。当前账号连接恢复已实测；自动登录、续期、口令生成、自助领取与发货的分支行为由业务回归覆盖，未通过真实交易逐项触发。Linux 组合在 Docker Desktop 内实测，未宣称完成独立 Linux 主机或 Windows 实机验收。

## 复验入口

```bash
backend-web/.venv/bin/python -m unittest discover -s tests
RUN_NATIVE_DB_TESTS=1 PYTHONPATH=scripts:. backend-web/.venv/bin/python -m unittest discover -s tests -p 'test_native_runtime.py'
yutaixian_data/local_dev/launcher.venv/bin/python scripts/test_services.py
backend-web/.venv/bin/python scripts/verify_shared_deployment.py
backend-web/.venv/bin/python scripts/verify_shared_deployment.py --full
backend-web/.venv/bin/python scripts/verify_credentials.py
bash dev.sh status
```

Docker 集成复验前需要启动 Docker Desktop。隔离测试成功后会移除自建测试容器及临时资源；失败时停止测试服务并保留诊断目录。

使用与恢复见 [共用本机数据的部署说明](SHARED-DEPLOYMENT.md)。Windows 后续工作见 [AI 交接说明](../launcher/windows/AI-HANDOFF.md)。历史日志、备份和旧浏览器缓存的保护边界亦见部署说明；本次未擅自删除历史资料。
