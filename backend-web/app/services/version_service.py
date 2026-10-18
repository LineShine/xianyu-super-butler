"""鱼太咸本地版本，不连接第三方更新服务。"""
from pathlib import Path

def get_current_version() -> str:
    return (Path(__file__).resolve().parents[3] / "version.txt").read_text().strip()
