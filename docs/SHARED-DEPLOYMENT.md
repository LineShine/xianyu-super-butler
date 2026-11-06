# 共用本机数据的部署

## Mac 日常操作

```bash
bash dev.sh switch native         # 原生应用，共用现有本机 MySQL / Redis
bash dev.sh switch docker         # Docker 应用，仍连接同一套本机 MySQL / Redis
bash dev.sh restart               # 重启当前模式的应用，数据库保持运行
bash dev.sh switch native --restart
bash dev.sh status
bash dev.sh logs websocket
bash dev.sh stop                  # 停止当前应用和本机基础服务；保留数据和密钥
```

双击 `启动鱼太咸.command` 会启动或重启原生应用；`停止鱼太咸.command` 完整停止。首次从旧启动器升级允许一次基础服务重启，以后切换只重启应用。单独关闭 Docker Desktop 后，只要确认数据库中的应用锁已经释放，原生模式可以独立启动。

Docker 模式双击 `启动鱼太咸Docker.command` 即可。脚本会自动打开 Docker Desktop 并等待引擎就绪（最长 180 秒），随后启动本机 MySQL、Redis、检查连接、停止旧应用并生成本次任务启用标记；无需再去 Docker App 点击 Start。需要已安装支持 `docker desktop start` 的 Docker Desktop。命令行仍可在引擎运行时执行 `bash dev.sh switch docker`。

双击 `停止鱼太咸Docker.command` 会等待当前实例任务退出，再停止本机数据库，保留数据、密钥和镜像。它复用完整停止入口，不退出 Docker Desktop，不操作其他 Docker 项目；若当前已切回原生，则完整停止同一实例的原生应用和数据库。

**Docker App 的项目 Start 按钮只启动现有容器，不能代替模式切换入口。** 当前 Mac 项目没有 MySQL、Redis 容器；电脑重启或完整停止后，直接点 Start 会因宿主机数据库未运行而出现 `Can't connect to MySQL server on 'host.docker.internal'`，随后显示 `backend-web exited (3)` / `dependency failed to start`。此外，切回原生后保存的旧容器也不能直接作为新的 Docker 运行实例使用。遇到这些情况执行统一启动入口即可，不要创建替代数据库或删除数据目录。

`bash docker.sh prepare|build|start|stop|restart|status|logs|update` 提供 Docker 操作入口。Mac 的 `start` 等价于 `dev.sh switch docker`，`update` 重新构建当前工作区源码并重启 Docker 应用；不会执行 git pull 或修改源码。

## 共享内容与边界

| 内容 | 原生与 Mac Docker 的关系 |
| --- | --- |
| MySQL | 同一个本机 mysqld、库名、账号及物理目录；Docker 不挂载 MySQL 目录 |
| Redis | 同一个本机进程、密码、DB 编号及代码中的键前缀；不复制、不清空、不重设 TTL |
| 业务文件 | 共用 `yutaixian_data/static`、`backups`、`private` |
| 主密钥 | 同一个实例密钥文件；容器只读挂载 |
| 浏览器 | `browser_runtime/darwin` 与 `browser_runtime/linux` 分开；通过加密的 Cookie 和浏览器 Cookie 快照恢复 |
| 账号任务 | 相同数据库中的服务所有权锁，加上统一启动器的模式互斥和健康检查开关 |

`yutaixian_data/instance.json` 记录非密钥的实例标识、数据库提供方式、应用模式、库名、Redis DB 及密钥路径。根 `.env` 仍保留现有数据库连接凭证；不要删除或替换。运行状态位于 `yutaixian_data/local_dev`，其中 `infrastructure.json` 与 `state.json` 分别记录数据库和原生应用。

实例描述可通过 `docker_project_name` 指定 Docker 项目名，例如 `yutaixian`。Docker Compose 项目名仅支持小写英文字母、数字、下划线和连字符，不能使用中文；未指定时仍按实例 ID 生成独立名称。更名应经统一启动器重启应用；启动前会检查目标名称的容器归属，拒绝接管其他实例。项目名不会改变实例 ID、密钥或数据库。

Mac 容器通过 `host.docker.internal` 访问本机端口，默认 MySQL 24306、Redis 27379。启动器会在实际目标容器中验证连接、MySQL UUID、Redis run ID、实例、密钥和凭据格式；失败时保留旧应用，不扩大数据库监听，不新建替代数据库。参见 [Docker Desktop 宿主机网络说明](https://docs.docker.com/desktop/features/networking/networking-how-tos/#connect-a-container-to-a-service-on-the-host)。

镜像由本项目源码构建，支持 Linux Chromium、反向代理和 WebSocket 长连接。仅当四个应用都健康时才写入本次运行的任务启用标记。切换前等待旧进程退出；拒绝与其他运行代次混用账号任务。目标启动失败时停止目标应用，数据库继续保留，可修复后重新执行切换。后台进程归属不明或仍有任务持锁时，不按端口强杀。

## 全新 Linux 完整容器模式

先安装 Docker Engine、Compose v2、Python 3.11+ 与 venv，然后在全新工作目录运行：

```bash
bash docker.sh prepare
bash docker.sh start
bash docker.sh status
bash docker.sh logs websocket
bash docker.sh update
bash docker.sh stop
```

此模式明确使用容器 MySQL 8.4 和 Redis 7.4，持久化到 `yutaixian_data/docker/mysql`、`redis`。数据库不发布公网端口；应用端口只绑定宿主机 loopback，可由管理员配置 HTTPS 反向代理。发现现有原生实例时会拒绝完整容器模式。Mac 数据与不同电脑的数据不会自动同步。

生成的 Compose JSON 包含本地数据库连接设置，仅保存在权限 600 的运行目录，不应提交或转发。不要手工执行 `down -v`、删除物理数据目录，或让第二个数据库进程打开相同目录。

## 凭据与恢复

账号密码、Cookie、代理密码、IM Token 和可移植浏览器 Cookie 快照使用 AES-256-GCM。每次写入生成随机数据密钥，用实例主密钥封装；关联数据绑定实例、账号凭据 ID、所属用户和字段。登录用户名仍作为可编辑的账号标识显示。主密钥不写入源码、数据库或普通备份。

Mac 主密钥位置：`~/Library/Application Support/Yutaixian/secrets/<instance-id>/keyring.json`；Linux：`~/.config/yutaixian/secrets/<instance-id>/keyring.json`。文件权限 600，父目录 700。应用不会自动生成替代密钥；只有首次启动器初始化会创建密钥。没有 Keychain、DPAPI 或人工解锁步骤。

账号 API 不回显密码或 Cookie；密码更新使用 `password_action=preserve|replace|clear`，Cookie 更新使用 `cookie_action`。普通 Excel 导出只含配置，不含登录凭据；导入到已有账号会保留缺省的凭据，跨实例的新账号需先重新登录。人脸验证截图存放在私有目录，经归属校验后提供短时访问链接。远程凭据接口要求 HTTPS 并禁止自动跟随重定向。

密钥缺失、权限异常、密文篡改或版本不匹配时，停止相关取密/启动操作；不会清空旧密文，也不会当作“未配置账密”禁用账号。真正没有账密且登录过期、续期失败的原禁用规则保持。

恢复时必须保留原 `.env`、实例描述、同一 MySQL / Redis 数据及原密钥。主密钥请单独保存到受控备份介质；普通数据库备份不足以恢复加密账号。仅持有源码或数据库不能解密；能够控制运行用户、读取密钥和进程内存的人仍能取得凭据，这不属于纯软件自动解密可以隔离的威胁范围。

升级后的日志不记录凭据值，数据库备份保存密文。首次迁移前的账号和 Token 凭据快照另行加密保存为 `backups/credential-upgrade-<instance-id>.ytxenc`，不包含主密钥，解密关联数据为 `("credential-upgrade-backup",)`。迁移前已有的历史 SQL 备份、旧日志及旧浏览器缓存不因数据库列加密而自动消除，仍应按敏感资料保管；磁盘中的历史数据库页也不等价于已安全擦除。此次不会删除这些历史资料。

## 验证与 Windows 交接

已执行的检查及实测范围见 [部署与凭据加密验收记录](DEPLOYMENT-VALIDATION.md)。

```bash
backend-web/.venv/bin/python -m unittest discover -s tests -p 'test_credential_vault.py'
yutaixian_data/local_dev/launcher.venv/bin/python scripts/test_services.py
bash dev.sh build
backend-web/.venv/bin/python scripts/verify_shared_deployment.py
backend-web/.venv/bin/python scripts/verify_shared_deployment.py --full
```

不带 `--full` 的命令创建临时本机数据库、Redis、密钥及应用，执行原生→Docker→原生的订单、凭据、文件及 TTL 往返验证；`--full` 则在 Docker 的 Linux 环境中验证全新六容器部署及应用重启保留数据库。成功后清理测试资源，不连接真实闲鱼账号。测试失败时保留临时诊断目录并停止测试服务。

Windows 此次仅交付两个 BAT、PowerShell 入口/适配接口、依赖清单、安装模板和构建入口。后续按 [Windows AI 交接说明](../launcher/windows/AI-HANDOFF.md) 在 Windows 完成原生适配及验收；目前没有可用 EXE，也没有新增 GitHub Actions。
