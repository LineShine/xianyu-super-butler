"""Send restart requests to this instance's owning native supervisor."""
from __future__ import annotations
import json
import os
from pathlib import Path
import secrets
import time

SERVICE_META = {
    key: {'port': int(os.getenv(variable, default)), 'dir': key, 'label': label}
    for key, variable, default, label in (
        ('backend-web', 'BACKEND_WEB_PORT', '8189', '后端服务'),
        ('websocket', 'WEBSOCKET_PORT', '8190', '消息服务'),
        ('scheduler', 'SCHEDULER_PORT', '8191', '定时任务服务'),
    )
}

def get_project_root():
    return Path(__file__).resolve().parents[2]

def detect_runtime():
    return 'native'

def restart_service(service_key: str) -> dict:
    if service_key not in SERVICE_META:
        return {'success': False, 'mode': 'native', 'message': '未知服务'}
    runtime = get_project_root() / 'yutaixian_data/local_dev'
    try:
        state = json.loads((runtime / 'state.json').read_text())
        if state.get('phase') != 'running' or os.environ.get('LOCAL_STACK_MANAGED') != '1':
            raise ValueError('请使用统一启动入口管理服务')
        request = {'service': service_key, 'token': state['token'], 'requested_at': time.time()}
        temporary = runtime / ('restart-' + secrets.token_hex(8) + '.tmp')
        temporary.write_text(json.dumps(request)); temporary.chmod(0o600)
        temporary.replace(runtime / 'restart-request.json')
        return {'success': True, 'mode': 'native', 'message': '已提交重启请求，请稍后刷新服务状态'}
    except (OSError, ValueError, KeyError):
        return {'success': False, 'mode': 'native', 'message': '请使用 bash dev.sh restart 重启服务'}
