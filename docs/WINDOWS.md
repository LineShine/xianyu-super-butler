# Windows 本地运行

支持 Windows 10 1809+ / Windows 11 x64，使用系统自带的 64 位 Windows PowerShell 5.1 和 tar.exe。源码目录需要当前用户可写。MySQL 需要 Microsoft Visual C++ 2015–2022 x64 运行库；缺少时，安装日志会在组件检查阶段报错。

## 日常操作

双击根目录的 **启动鱼太咸.bat**。首次运行会下载并校验便携组件，创建三个独立 Python 环境，安装浏览器并构建前端。所有应用就绪后打开 <http://127.0.0.1:9999>。双击 **停止鱼太咸.bat** 完整停止本实例。

也可以在项目目录执行：

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\launcher\windows\Yutaixian.ps1 -Action Prepare
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\launcher\windows\Yutaixian.ps1 -Action Start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\launcher\windows\Yutaixian.ps1 -Action Status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\launcher\windows\Yutaixian.ps1 -Action Restart
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\launcher\windows\Yutaixian.ps1 -Action Stop
~~~

- **Prepare**：准备依赖、浏览器和前端产物，并重新实测浏览器启动。更新依赖或前端源码后，先 Stop，再 Prepare、Start。
- **Start**：自动准备并启动；已经运行时校验状态，不创建重复进程。
- **Restart**：重启应用，保持 MySQL、Redis 的进程和数据。运行中的依赖不会被替换。
- **Stop**：先关闭应用，再正常关闭 MySQL、Redis；保留配置、数据库和密钥。
- **Status**：查询管理进程归属、阶段和最近错误。不会启动服务或下载组件。
- **-NoBrowser**：与 Start / Restart 一起使用，完成启动后不自动打开网页。

全新数据库管理员为 admin / admin123，首次登录后修改密码。默认端口和 .env 配置项与 [项目说明](../README.md#数据与端口) 相同，所有服务仅绑定 127.0.0.1。

## 数据与组件

| 位置 | 内容 |
| --- | --- |
| .env | 本实例随机生成的连接密码与内部令牌 |
| yutaixian_data/native | 独立 MySQL / Redis 数据、初始化记录和私有配置 |
| yutaixian_data/instance.json | 实例身份、数据库名称及密钥文件位置 |
| yutaixian_data/local_dev | 应用与基础服务的独立管理状态、任务启用标记和日志 |
| yutaixian_data/windows | 下载缓存、版本化便携组件和 Playwright/Patchright 浏览器 |
| 各服务的 .venv | 三个相互独立的 Python 环境 |
| %LOCALAPPDATA%\Yutaixian\secrets\<instance-id>\keyring.json | AES-256-GCM 实例主密钥 |

依赖版本、来源与 SHA256 记录在 [dependencies.json](../launcher/windows/dependencies.json)。Python 使用 Astral 发布的 CPython 便携构建，Node.js 和 MySQL 使用上游发行包，Redis 使用 redis-windows 项目的 Cygwin 社区构建。浏览器归档同样固定 SHA256，并核对每个 Playwright/Patchright 环境所需的 revision；Python 下载校验后，由父 PowerShell 调用独立 Node 进程解压，再执行完整 Chromium 和默认无头浏览器启动检查。启动器不注册 Windows 服务，不修改系统 PATH，也不接管机器上已有的 MySQL / Redis。

密钥目录和文件关闭 ACL 继承，只允许当前运行身份和 SYSTEM 访问。读取时同样校验权限；不使用 DPAPI，也不需要人工解锁。请始终以同一个 Windows 用户运行。

恢复时需同时保留原 .env、实例描述、数据库数据和原密钥；主密钥应单独备份。删除密钥、管理记录或已有数据库不会触发自动生成替代实例。账号解密失败仍按原业务规则暂停处理，不被当作未配置账密而禁用账号。

## 运行和异常恢复

基础服务与应用各有一个后台管理进程。命名互斥量防止并行启动；状态记录包含进程创建时间、程序路径和本次启动标识。停止通过具名事件通知对应管理器，不按端口或过期 PID 强杀。

服务进程在恢复执行前加入 Windows Job Object。管理器异常退出时，Windows 回收它管理的进程树，包括浏览器子进程。正常停止优先通过应用退出事件、MySQL SHUTDOWN、Redis SHUTDOWN SAVE 完成；超过退出期限才回收本实例 Job 中的剩余进程。

启动顺序为：检查配置和密钥 → 启动基础服务并校验身份 → 检查服务所有权锁 → 初始化或迁移凭据格式 → 启动四个应用并检查健康 → 写入当前运行代次的 activation 文件。自动任务只在最后一步通过后启用。

重试时直接使用 Start。已有完整组件与依赖会被复用；未完成的下载和解压不会视为成功。进程归属无法确认、端口占用、迁移未完成或密钥缺失时，先修复错误再重试。

## 排查

~~~powershell
Get-Content .\yutaixian_data\local_dev\logs\setup.log -Tail 60
Get-Content .\yutaixian_data\local_dev\logs\infra-supervisor.log -Tail 60
Get-Content .\yutaixian_data\local_dev\logs\apps-supervisor.log -Tail 60
~~~

组件分别记录 mysql、redis、backend-web、websocket、scheduler、frontend 日志。浏览器下载或解压失败时保留准备失败状态，修复网络或文件访问问题后重新 Prepare。中断后可复用已完成的解压目录；目录短暂被占用时会有限重试，不会强行结束占用它的外部程序。

端口被占用或被 Windows 保留时，在 .env 调整对应端口；六个端口必须不同。启动器不会停止占用端口的其他程序。

使用 Windows 的北京时间设置和锁定的 tzdata，避免 IANA TZ 字符串被 Windows C 运行库当作 UTC。

支持中文和空格路径。路径过深且系统未启用 LongPathsEnabled 时，会提示移至较短目录或由系统管理员启用 Windows 长路径策略；启动器不会自动修改系统注册表。

## 开发与验收

~~~powershell
$python = '.\yutaixian_data\windows\packages\python-3.12.12\python\python.exe'
& $python -X utf8 -m unittest discover -s tests -p test_windows_runtime.py -v
& $python -X utf8 scripts\verify_windows_runtime.py
& $python -X utf8 scripts\test_services.py
~~~

集成验收使用独立的中文/空格目录、端口、数据库和合成凭据，复用已准备好的依赖；不连接真实闲鱼账号，不发送消息或创建交易。失败保留诊断目录；成功默认清理合成实例，使用 --keep 可保留。报告位于 build_output/windows-runtime-validation.json。

实机测试命令、覆盖范围和结果见 [Windows 验收记录](WINDOWS-VALIDATION.md)。

本次交付为源码本地运行。EXE 安装包仍需单独完成白名单打包及安装/升级/卸载验收；集成测试不会生成用于绕过 Build 门禁的 windows-validation.json。
