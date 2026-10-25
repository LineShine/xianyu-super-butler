# 商品口令生成

商品管理在「AI提示词」之后提供「口令生成」开关，所有商品默认关闭，按账号和商品独立保存。

开启后，卖家在鱼太咸 Web 聊天或闲鱼 App 的商品单聊中发送完整的 `生成口令` 或 `口令生成`。买家先收到卖家的四字指令，再收到服务器生成的六位口令。空格、换行、标点、引用、买家消息和自动消息回传均不触发。

仅处理本次开启之后、最近五分钟内且具有有效原始时间的指令。商品或买家无法唯一确认时停止，原因记录在「回复日志」的「口令生成」规则中。功能不依赖 AI 开关，人工发出消息仍保留原来的 AI 暂停行为。

服务器使用服务自身的 Python 3 解释器执行 `common/custom_func/generate_code.py` 的绝对路径，脚本算法保持不变。执行不经过 shell，不接收聊天文本参数，最多运行五秒；只接受成功退出且输出六位大小写字母或数字的结果。脚本按原规则可能在同一秒产生相同口令。

同一会话串行处理，以账号、会话、源消息 ID（缺失时包含原始时间的指纹）在现有回复日志中持久化占用。重复推送和重启后的重复消息不会再次执行；卖家发送新的指令可以再次生成。

发送前重新检查商品开关、开启时间和账号启用状态。闲鱼明确确认才记录「发送成功」；拦截记录失败；无回执或处理中断记录结果未知，不自动重试或补发。遇到未知状态请先检查聊天记录，再决定是否发送新指令。脚本报错不会向买家发送错误文本。

配置接口：`PUT /api/v1/items/{cookie_id}/{item_id}/code-generation`，请求 `{"enabled": true}` 或 `{"enabled": false}`，校验账号和商品归属。配置存入商品 metadata 的 `code_generation_enabled` 和 `code_generation_enabled_at`（Unix 毫秒），商品同步保留它们。幂等开启不刷新时间，关闭后重新开启会生成新时间。

本地回归（模拟消息和发送接口，不向真实买家发消息）：

```sh
PYTHONPATH=backend-web:. backend-web/.venv/bin/python -m unittest discover -s backend-web/tests -p 'test_item_code_generation.py'
PYTHONPATH=websocket:. websocket/.venv/bin/python -m unittest discover -s websocket/tests -p 'test_code_generation.py'
PYTHONPATH=. backend-web/.venv/bin/python frontend/tests/code-generation.browser.py
```

浏览器测试需要本地 Vite 服务，所有 API 和聊天 WebSocket 均被模拟。持久化去重依赖回复日志；不要在其五分钟有效窗口内删除相关日志或手动清理占用记录。
