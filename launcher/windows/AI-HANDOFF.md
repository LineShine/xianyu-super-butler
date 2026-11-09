# Windows AI 实现交接

> 本文保留最初交接要求。Windows 源码运行实现及使用方式见 [Windows 本地运行](../../docs/WINDOWS.md)。EXE 打包仍是独立的后续工作。

本目录是框架，不是已完成的 Windows 安装器，也未生成 EXE。Mac 与 Docker 的已实现逻辑位于 `scripts/deployment.py`、`scripts/docker_deploy.py`、`common/security/runtime.py`。不要直接在 Windows 导入依赖 fcntl/ps/killpg 的 Mac 启动器。

在 Windows 实机完成以下工作：

1. 补齐 `Platform.psm1` 六个适配函数。分别维护基础服务与应用进程；重启应用时保持 MySQL/Redis 进程。用 Windows Job Object、命名互斥量及进程创建时间校验归属，不按端口强杀、不按过期 PID 停止进程。
2. 核实并固定依赖清单中的具体版本、官方 HTTPS 下载地址和 SHA256。Redis 为需实测的社区 Windows 构建；未填写 SHA256 的组件禁止下载执行。用 ZIP/便携组件放入实例专属运行目录，不改动已有系统 MySQL/Redis。
3. 用现有 Python lockfile 创建三个独立环境，校验 Windows wheel 可用性，安装并实测 Playwright/Patchright。处理中文、空格路径和长路径。前端使用现有源码构建。
4. 密钥放 `%LOCALAPPDATA%\Yutaixian\secrets\<instance-id>\keyring.json`。创建时用 ACL 限制当前运行身份和 SYSTEM，关闭继承；不使用 DPAPI、不人工解锁。密钥不能放进安装包、普通备份或日志。采用现有 AES-256-GCM 格式和实例校验，缺失密钥不得新建替代密钥。
5. 复用 MySQL 每角色所有权锁、同一运行代次校验、任务 activation 文件、凭据迁移和失败停止机制。首次新库必须先初始化凭据版本，再启动后端建表；所有服务健康后才启用任务。
6. 生成安装 payload 必须用明确文件白名单，排除真实 `.env`、数据库、Cookie、浏览器缓存、截图、备份、主密钥和开发工具缓存。Inno Setup 安装模板不得覆盖已有配置、数据或密钥；卸载默认保留用户数据。
7. 测试准备/启动/重复启动/完整停止/应用重启/异常中断恢复/端口冲突/密钥缺失/路径含中文与空格/登录自动续期。原有登录过期无账密禁用规则保持；解密失败必须暂停而非清空或禁用。
8. 全部通过后在构建 payload 中写入真实的 `windows-validation.json` 验收结果，再运行 `Yutaixian.ps1 -Action Build`。不能仅创建 passed 标记绕过验收。记录操作系统、依赖版本、测试命令和结果。此次无需 GitHub Actions。
