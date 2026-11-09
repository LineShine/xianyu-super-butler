# Windows 源码本地运行验收

验收日期：2026-11-08。环境：Windows 11 x64，版本 10.0.22000；Windows PowerShell 5.1。这里只记录实际执行的源码运行验收，不代表 EXE 安装包验收。

## 运行组件

| 组件 | 固定版本 |
| --- | --- |
| Python | CPython 3.12.12，Astral 便携构建 |
| Node.js | 22.21.1 |
| MySQL | 8.4.13 |
| Redis | 7.4.8，redis-windows Cygwin 构建 |
| Playwright / Patchright | 1.64.0 / 1.64.1 |
| Chromium / Headless Shell | 156.0.8078.4，revision 1248 |
| ffmpeg / winldd | revision 1013 / 1007 |

组件来自 dependencies.json 中的固定 HTTPS 地址；全部下载归档已通过 SHA256 校验。三个 Python 环境分别安装锁定依赖，并通过 pip check。前端 npm ci、npm run build 已通过。

## 回归与集成结果

| 命令或检查 | 结果 |
| --- | --- |
| 便携 Python -m unittest discover -s tests -p test_windows_runtime.py -v | 13 项通过 |
| backend-web Python -m unittest discover -s tests -p test_credential_vault.py -v | 6 项通过 |
| 便携 Python scripts/test_services.py backend-web | 124 项通过 |
| 便携 Python scripts/test_services.py websocket | 140 项通过 |
| 便携 Python scripts/test_services.py scheduler | 12 项通过 |
| 便携 Python scripts/verify_windows_runtime.py | 11 项集成验收通过 |
| Yutaixian.ps1 -Action Prepare | 成功；五组 Playwright/Patchright 环境均实际启动完整 Chromium 和默认无头浏览器 |
| 当前目录 Start / Status / Restart / Stop / 再次 Start | 全部成功；三个服务健康检查通过，Restart 保留基础服务进程，Stop 清空进程，随后从已验证缓存恢复运行 |
| 实际登录页面 | HTTP 200，浏览器渲染及输入框检查通过 |
| 实际 Chromium 的 Job Object 回收 | 通过；关闭 Job 后 Python、Node、Chromium 共 8 个进程均退出 |

集成验收覆盖：首次启动与健康检查、重复启动不创建新进程、前端 API 代理及管理员登录、重启应用保留 MySQL/Redis 进程与 TTL、不同运行代次的数据库锁互斥、系统服务重启请求、管理器异常退出后的 Job 进程树清理与恢复、密钥丢失拒绝启动且不生成替代密钥、端口冲突不影响外部监听程序、完整停止再启动后的订单/缓存持久化及凭据解密、中文与空格源码和数据目录。

测试实例使用独立配置、端口、数据库、主密钥及合成账号，直接复用已准备好的三个服务环境和便携组件。仅在合成实例中关闭外部登录验证码，以验证本地密码认证；没有修改用户实例的验证码配置。可执行文件不使用硬链接跨实例复用，避免 Windows 映像文件占用。

详细集成结果写入 build_output/windows-runtime-validation.json。失败实例保留在 yutaixian_data/windows-validation 供诊断；成功实例默认清理。所有测试都没有连接真实闲鱼账号、发送消息或创建交易。

## 范围

已验证源码启动、浏览器启动和本机服务生命周期。真实闲鱼登录、人工验证码交互、真实交易业务及 Windows 10 的完整实机矩阵未覆盖。EXE 打包、安装/升级/卸载仍需独立实施；本次没有生成安装器的 windows-validation.json 放行标记。
