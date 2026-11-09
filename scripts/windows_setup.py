"""Verified portable dependencies and separate Windows service environments."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

SERVICES = ("backend-web", "websocket", "scheduler")


class BrowserInstallRequired(RuntimeError):
    """Hand verified browser archives to the parent Windows launcher."""



def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # Antivirus/indexers can briefly hold a Windows file without delete sharing.
        for attempt in range(20):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.05)
    finally:
        temporary.unlink(missing_ok=True)


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_entry(entry):
    import re
    if not re.fullmatch(r"[a-fA-F0-9]{64}", entry.get("sha256") or ""):
        raise RuntimeError("组件缺少 SHA256，禁止下载执行")
    if urllib.parse.urlparse(entry["source"]).scheme != "https":
        raise RuntimeError("组件下载地址必须为 HTTPS")
    if not re.fullmatch(r"[a-z-]+", entry["name"]) or not re.fullmatch(r"[0-9.]+", entry["version"]):
        raise RuntimeError("无效组件名称或版本")
    member = entry["executable"].replace("\\", "/")
    if member.startswith("/") or ":" in member or ".." in member.split("/"):
        raise RuntimeError("组件路径越界")


def extract_zip(archive, stage):
    stage = Path(stage).resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            relative = member.filename.replace("\\", "/")
            target = (stage / relative).resolve()
            if (not target.is_relative_to(stage) or relative.startswith("/") or ":" in relative
                    or (member.external_attr >> 16) & 0o170000 == 0o120000):
                raise RuntimeError("组件压缩包包含不安全路径")
        bundle.extractall(stage)


class Setup:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.runtime = self.root / "yutaixian_data/local_dev"
        self.packages = self.root / "yutaixian_data/windows/packages"
        self.logs = self.runtime / "logs"
        self.browsers = self.root / "yutaixian_data/windows/browsers"
        self.tools_file = self.runtime / "windows-tools.json"

    def command(self, args, label, *, cwd=None, env=None):
        print(label, flush=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        with (self.logs / "setup.log").open("a", encoding="utf-8") as output:
            output.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {label}\n")
            output.flush()
            result = subprocess.run([str(x) for x in args], cwd=cwd or self.root,
                                    env=env, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=output, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(f"{label}失败，请查看 {self.logs / 'setup.log'}")

    def download(self, entry):
        validate_entry(entry)
        cache = self.root / "yutaixian_data/windows/downloads"
        cache.mkdir(parents=True, exist_ok=True)
        archive = cache / f"{entry['name']}-{entry['version']}.zip"
        if not archive.is_file() or file_hash(archive) != entry["sha256"]:
            print(f"下载 {entry['name']} {entry['version']} …", flush=True)
            partial = archive.with_suffix(".partial")
            with urllib.request.urlopen(entry["source"], timeout=90) as response:
                if urllib.parse.urlparse(response.url).scheme != "https":
                    raise RuntimeError("下载重定向降级为非 HTTPS")
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output)
            if file_hash(partial) != entry["sha256"]:
                raise RuntimeError(f"{entry['name']} SHA256 不符，未解压或执行")
            partial.replace(archive)
        return archive

    def install(self, entry):
        validate_entry(entry)
        destination = self.packages / (entry["name"] + "-" + entry["version"])
        marker = read_json(destination / ".installed.json")
        executable = destination / marker.get("executable", entry["executable"])
        if (marker.get("sha256") == entry["sha256"] and executable.resolve().is_relative_to(destination.resolve())
                and executable.is_file()):
            return str(executable)
        archive = self.download(entry)
        self.packages.mkdir(parents=True, exist_ok=True)
        stage = destination.with_name(destination.name + ".extract-" + uuid.uuid4().hex)
        stage.mkdir()
        extract_zip(archive, stage)
        relative = Path(entry["executable"])
        if not (stage / relative).is_file() and entry["name"] == "redis":
            matches = list(stage.glob("*/redis-server.exe"))
            if len(matches) == 1:
                relative = matches[0].relative_to(stage)
        if not (stage / relative).is_file():
            raise RuntimeError("组件布局与依赖清单不一致")
        if destination.exists():
            # Retain interrupted packages. Never delete data, caches or prior tools.
            assert destination.resolve().is_relative_to(self.packages.resolve())
            destination.rename(destination.with_name(destination.name + ".incomplete-" + uuid.uuid4().hex))
        stage.rename(destination)
        write_json(destination / ".installed.json", {"sha256": entry["sha256"], "executable": str(relative)})
        return str(destination / relative)

    def fingerprint(self, paths):
        digest = hashlib.sha256(str(self.root).encode("utf-8"))
        digest.update(sys.version.encode())
        for path in paths:
            digest.update(str(path).encode("utf-8"))
            digest.update(Path(path).read_bytes())
        return digest.hexdigest()

    def prepare(self, *, probe_browsers=False):
        manifest = read_json(self.root / "launcher/windows/dependencies.json")
        tools = {entry["name"]: self.install(entry) for entry in manifest["dependencies"] if entry["name"] != "python"}
        tools["python"] = sys.executable
        for name in ("node", "mysql", "redis"):
            self.command([tools[name], "--version"], f"检查 {name}（MySQL 需要 VC++ x64 运行库）")
        write_json(self.tools_file, tools)
        environment = os.environ.copy()
        # Desktop/CI parents may omit shell variables used by npm postinstall.
        environment.setdefault("COMSPEC", str(Path(os.environ["SYSTEMROOT"]) / "System32/cmd.exe"))
        extensions = [value.upper() for value in environment.get("PATHEXT", "").split(";") if value]
        environment["PATHEXT"] = ";".join(dict.fromkeys([*extensions, ".COM", ".EXE", ".BAT", ".CMD"]))
        environment.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", PLAYWRIGHT_BROWSERS_PATH=str(self.browsers))
        environment["PATH"] = str(Path(tools["node"]).parent) + os.pathsep + environment.get("PATH", "")
        marker_path = self.runtime / "windows-dependencies.json"
        marker = read_json(marker_path)
        launcher_lock = self.root / "scripts/requirements-launcher.txt"
        fingerprint = self.fingerprint([launcher_lock])
        if marker.get("launcher") != fingerprint:
            self.command([sys.executable, "-m", "pip", "install", "--only-binary=:all:", "-r", launcher_lock],
                         "安装启动器依赖", env=environment)
            marker["launcher"] = fingerprint
            write_json(marker_path, marker)
        extras = self.root / "launcher/windows/requirements-extra.lock"
        for name in SERVICES:
            directory = self.root / name
            python = directory / ".venv/Scripts/python.exe"
            fingerprint = self.fingerprint([directory / "pyproject.toml", directory / "requirements.lock", extras])
            created = not python.is_file()
            if created:
                self.command([sys.executable, "-m", "venv", directory / ".venv"], f"创建 {name} 独立环境", env=environment)
            if created or marker.get(name) != fingerprint:
                # Constraints retain the existing resolution; uvloop is conditional.
                # Native extensions must have Windows wheels; pure Python extras may build wheels.
                binary = "asyncmy,cryptography,cffi,greenlet,pydantic-core,numpy,pandas,pillow,lxml,opencv-python-headless,httptools,watchfiles"
                self.command([python, "-m", "pip", "install", "--prefer-binary", "--only-binary=" + binary,
                              "-c", directory / "requirements.lock", "-c", extras, "-e", directory, "tzdata"],
                             f"安装 {name} 锁定依赖及 Windows wheel", env=environment)
                self.command([python, "-m", "pip", "check"], f"检查 {name} 依赖兼容性", env=environment)
            marker[name] = fingerprint
            write_json(marker_path, marker)
        tools["service_python"] = {name: str(self.root / name / ".venv/Scripts/python.exe") for name in SERVICES}
        write_json(self.tools_file, tools)
        frontend = self.root / "frontend"
        npm = Path(tools["node"]).parent / "node_modules/npm/bin/npm-cli.js"
        lock_fingerprint = self.fingerprint([frontend / "package.json", frontend / "package-lock.json"])
        if marker.get("npm") != lock_fingerprint or not (frontend / "node_modules/vite/bin/vite.js").is_file():
            self.command([tools["node"], npm, "ci", "--include=dev", "--no-audit", "--no-fund"], "安装前端依赖", cwd=frontend, env=environment)
            marker["npm"] = lock_fingerprint
        sources = []
        for directory, children, files in os.walk(frontend):
            children[:] = sorted(x for x in children if x not in ("node_modules", "dist", ".git"))
            sources.extend(Path(directory) / name for name in sorted(files))
        fingerprint = self.fingerprint(sources)
        if marker.get("frontend") != fingerprint or not (frontend / "dist/index.html").is_file():
            self.command([tools["node"], npm, "run", "build"], "构建本地前端", cwd=frontend, env=environment)
            marker["frontend"] = fingerprint
        write_json(marker_path, marker)
        pinned_browsers = {(entry["name"], entry["version"]): entry for entry in manifest["browsers"]}
        required_browsers = set()
        for name in SERVICES:
            for browser in (("playwright",) if name == "scheduler" else ("playwright", "patchright")):
                registry = read_json(self.root / name / ".venv/Lib/site-packages" / browser / "driver/package/browsers.json")
                for entry in registry["browsers"]:
                    if entry["name"] in ("chromium", "chromium-headless-shell", "ffmpeg", "winldd"):
                        revision = str(entry.get("revisionOverrides", {}).get("win64", entry["revision"]))
                        key = (entry["name"], revision)
                        if key not in pinned_browsers:
                            raise RuntimeError(f"{browser} 所需的 {key} 未锁定 SHA256，请更新 Windows 浏览器清单")
                        required_browsers.add(key)
        pending = []
        for key in sorted(required_browsers):
            entry = pinned_browsers[key]
            validate_entry(entry)
            directory = self.browsers / (entry["name"].replace("-", "_") + "-" + entry["version"])
            browser_marker = read_json(directory / ".installed.json")
            if browser_marker.get("sha256") != entry["sha256"] or not (directory / entry["executable"]).is_file():
                self.download(entry)
                pending.append(list(key))
        if pending:
            write_json(self.runtime / "browser-install-request.json", {"root": str(self.root), "browsers": pending})
            raise BrowserInstallRequired("浏览器归档已完成校验，正在交由 Windows 启动器解压。")
        browser_fingerprint = hashlib.sha256(json.dumps({
            "environments": {name: marker[name] for name in SERVICES},
            "archives": [(key, pinned_browsers[key]["sha256"]) for key in sorted(required_browsers)],
        }, sort_keys=True).encode()).hexdigest()
        if not probe_browsers and marker.get("browser_probes") == browser_fingerprint:
            print("Windows 运行依赖及浏览器已就绪（复用已验证缓存）。", flush=True)
            return
        marker.pop("browser_probes", None)
        write_json(marker_path, marker)
        for name in SERVICES:
            python = self.root / name / ".venv/Scripts/python.exe"
            for browser in (("playwright",) if name == "scheduler" else ("playwright", "patchright")):
                # Check the full browser used by manual login as well as the
                # default headless executable. A partial install is not ready.
                probe = (f"from {browser}.sync_api import sync_playwright; "
                         "p=sync_playwright().start(); "
                         "b=p.chromium.launch(headless=True, executable_path=p.chromium.executable_path); b.close(); "
                         "b=p.chromium.launch(headless=True); b.close(); p.stop()")
                self.command([python, "-c", probe], f"实测 {name} {browser} 浏览器", env=environment)
        marker["browser_probes"] = browser_fingerprint
        write_json(marker_path, marker)
        print("Windows 运行依赖及浏览器已就绪。", flush=True)
