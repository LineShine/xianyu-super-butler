# 部署与凭据加密验收记录

首次验收日期：2026-11-05；最近复验：2026-11-06。环境：当前 Apple Silicon Mac；容器测试使用本机 Docker Desktop 的 Linux 容器环境。Windows 仅交付框架。

## 2026-11-06：商品同步与完整编辑恢复

- 根因：容器仅保留当前服务的 `/app/service`，未提供共享加载器依赖的 `/app/backend-web/app`。定时同步吞掉能力检测的模块缺失异常，降级使用个人版卡片数据覆盖卖家详情，导致库存、状态、上架时间和完整编辑标记丢失。
- 三个后端镜像均补齐共享源码，并为消息、调度服务补齐共享发布入口的媒体依赖。构建期加载全部四个共享入口，检查当前服务包与配置未被替换；该检查禁止网络连接。
- 能力检测失败或不可靠时停止同步；弱快照不能未经可靠账号判定覆盖已有卖家详情。稀疏卖家快照保留未提供字段，零库存等明确值仍可更新。
- 已保存修复前私有快照，重新拉取两条真实商品的卖家列表数据；API 确认库存、在卖状态、上架时间及卖家编辑标记恢复。
- 实际浏览器验证两个完整编辑入口可用，编辑弹窗回填图片，拖拽后图片顺序正确变化；随后取消，未向闲鱼提交编辑。浏览器测试跳过外部 Google 字体请求，使用系统字体，不替换商品 API。
- 新增商品同步回归 8 项通过；后端业务测试 132 项、消息服务 140 项、调度服务 12 项通过。部署测试 33 项通过（其中 4 个条件性探针跳过：2 个本机数据库夹具、2 个尚未安装新媒体依赖的原生环境探针）；真实 Docker 镜像均通过完整导入检查并启动健康。
- 应用已在 Docker 模式重启，四个容器健康、两个账号 connected；两个账号和四条 IM Token 记录的解密校验通过。MySQL、Redis 继续共用本机实例。

## 2026-11-06：Docker 启动恢复及更名复验

- 已定位 Docker App 直接 Start 失败：宿主机 MySQL、Redis 未运行，backend-web 连接 `host.docker.internal` 被拒绝，退出码 3；其他应用因依赖后端健康检查而无法启动。
- 经统一入口恢复，项目名按用户确认改为 `yutaixian`，实例 ID、密钥和本机数据库保持不变。旧 `ytx-<实例前12位>` 项目的四个已停止应用容器及空网络已清理，未删除数据、密钥或镜像，未操作其他 Docker 项目。
- 新增 `启动鱼太咸Docker.command` 与 `停止鱼太咸Docker.command`。启动入口自动调用 Docker Desktop 启动命令并等待引擎，随后启动本机基础服务与容器；停止入口完整停止当前实例，保留 Docker Desktop 和其他项目。
- 实测两个脚本完成停止 → 启动：四个应用健康、两个账号 connected；原 MySQL UUID、账号数量保留，Redis 在完整停止后正常重启，专用测试键的值和自然剩余 TTL 保留。该测试键已移除。
- 两个账号、四条 IM Token 记录逐项解密校验通过；三个服务持有各自的独占运行锁，并属于同一次启动。
- 排除 Finder/Explorer 元数据对源码指纹与构建上下文的影响。部署专项 13 项测试通过；根目录测试共 30 项，28 项通过、2 项依赖临时本机数据库的测试本轮未启用。Shell 语法与 git diff 检查通过。
- 自动启动命令在 Docker Desktop 已运行时实测通过；此次未为测试自动打开功能而再次关闭整个 Docker 引擎。最终保留 Docker 应用模式运行。

本次运行结果保存在本机 `yutaixian_data/local_dev/docker-recovery-check.json`，不包含凭据或主密钥。

## 2026-11-05：首次实施验收

- 旧的应用与数据库合并管理器已拆分；首次升级进行了一次受控基础服务重启。
- 原 MySQL 实例 UUID 和账号数量保留；现有 2 个账号、4 条 IM Token 记录完成加密迁移，并通过逐项解密校验。
- 已实际完成原生 → Docker → 原生切换。正常切换前后 MySQL UUID、拆分后的 Redis run ID 保持一致；临时验证键的值与自然剩余 TTL 保持连续，验证键已清理。
- Docker 共用模式只运行四个应用容器，连接现有本机数据库，没有为当前实例建立第二套 MySQL / Redis。
- 最后恢复原生模式并关闭 Docker Desktop；前端和三个后端均返回健康响应，2 个账号的消息连接均处于 connected 状态。
- 当次验收结束配置为 `database_provider=native`、`application_mode=native`。实例与密钥保持原样；主密钥存放在源码目录之外，权限为 600。

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
