# 鱼太咸

面向闲鱼卖家的本机工作台。账号、聊天、订单、自动发货、卡券、商品、采集监控和定时任务集中管理。界面沿用黄白配色与圆角卡片。

## 启动

macOS 双击 **启动鱼太咸.command**，启动完成后会打开 <http://127.0.0.1:9100>。停止时双击 **停止鱼太咸.command**。

首次运行需要联网，脚本会准备 Homebrew、Python、Node.js、MySQL、Redis 和浏览器组件。支持 macOS 与 Homebrew Linux，不需要 Docker。Homebrew 安装可能要求输入电脑的系统密码；下载中断后重新运行即可。

也可以在项目目录使用终端：

```bash
bash dev.sh start
bash dev.sh status
bash dev.sh restart
bash dev.sh stop
bash dev.sh setup
bash dev.sh logs websocket
```

`setup` 用于停止服务后安装或更新依赖；日常直接运行 `start`。日志名称还支持 `backend-web`、`scheduler`、`frontend`、`mysql`、`redis`、`supervisor`、`setup`、`migration`。按 Ctrl+C 退出日志查看，不会停止服务。

迁移用户用原项目的账号密码重新登录。全新空库初始管理员为 `admin` / `admin123`，登录后在个人设置修改密码。

## 数据与端口

运行文件全部保存在 `yutaixian_data/`；根目录 `.env` 保存本实例随机生成的连接密码。停止、重新启动、修改源码均保留这些数据。请同时备份这两个位置；不要将它们提交 Git。

| 服务 | 本机端口 | 配置项 |
| --- | --- | --- |
| Web | 9100 | `FRONTEND_PORT` |
| Web API | 8189 | `BACKEND_WEB_PORT` |
| 消息连接 | 8190 | `WEBSOCKET_PORT` |
| 定时任务 | 8191 | `SCHEDULER_PORT` |
| MySQL | 24306 | `LOCAL_MYSQL_PORT` |
| Redis | 27379 | `LOCAL_REDIS_PORT` |

在首次生成的 `.env` 中添加对应配置项可覆盖端口，默认仅监听 `127.0.0.1`。数据库为 `yutaixian_data`，业务表前缀为 `ytx_`，应用缓存和浏览器存储使用 `ytx:` 命名空间。此实例与其他项目的数据相互独立。

端口被占用、依赖下载失败或服务异常时，启动器会报错并停止本次启动的进程，保留数据供重试。它不会接管其他程序，也不会恢复旧应用。运行中的单个服务可从系统管理页面请求重启；统一启动器负责管理其进程。

## 导入现有数据

先阅读 [迁移说明](docs/MIGRATION.md)。预检查不会停服务或改数据：

```bash
bash dev.sh migrate --from /绝对路径/原项目 --dry-run
```

确认清单后执行：

```bash
bash dev.sh migrate --from /绝对路径/原项目
bash dev.sh start
```

正式迁移会停止来源的原生方式五实例，备份其数据，在备份副本上完成导出，保留原库。新实例校验完成前不会启动账号连接和任务。迁移失败时使用同一命令续跑；已经投入使用的目标数据库不允许再次覆盖。

## 功能与许可

完整功能对应表见 [功能清单](docs/FEATURES.md)，本次验证情况见 [验收报告](docs/VERIFICATION.md)。

本版本不包含公告、广告、推广、软件售卖、激活码、充值返佣、商品分销、对接库存或结算。发货使用自有卡券。版本由本地 `version.txt` 维护。

保留并遵循 [AGPL-3.0 许可证](LICENSE)。必要来源署名见 [NOTICE](NOTICE)。
