"""Validate IM authentication separately from the WebSocket transport."""
from __future__ import annotations

# Both clients must register as the same supported IM protocol implementation.
IM_REGISTRATION_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36 "
    "DingTalk(2.1.5) OS(Windows/10) "
    "Browser(Chrome/146.0.0.0) DingWeb/2.1.5 "
    "IMPaaS DingWeb/2.1.5"
)


def is_im_auth_failure(response: dict) -> bool:
    if not isinstance(response, dict):
        return False
    body = response.get("body") or {}
    if not isinstance(body, dict):
        body = {}
    reason = str(body.get("reason") or "").lower()
    return (
        str(response.get("code")) == "401"
        or str(body.get("code")) == "4000001"
        or any(marker in reason for marker in (
            "token is not found", "token is invalid", "invalid token",
            "token expired", "token_expired", "session_expired",
            "session is invalid", "invalid session",
        ))
    )


class ImRegistrationError(RuntimeError):
    def __init__(self, response: dict):
        self.auth_failure = is_im_auth_failure(response)
        body = response.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        # Do not log the token, session ID or full registration response.
        super().__init__(
            f"IM注册失败: code={response.get('code')}, "
            f"body_code={body.get('code')}, reason={body.get('reason') or '未确认注册成功'}"
        )


def validate_im_registration(response: dict) -> None:
    body = response.get("body") or {}
    if str(response.get("code")) != "200" or (
        isinstance(body, dict) and body.get("reason")
    ):
        raise ImRegistrationError(response)


def get_im_sync_state(response: dict) -> dict:
    """Use the server's cursor verbatim; a wall-clock timestamp isn't a cursor."""
    validate_im_registration(response)
    state = response.get("body")
    if not isinstance(state, dict) or state.get("topic") != "sync" or "pts" not in state:
        raise RuntimeError("IM同步状态缺少服务端游标或topic，停止初始化")
    return dict(state)
