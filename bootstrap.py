#!/usr/bin/env python3
"""Первичная настройка Debian/Ubuntu (Python 3.9+, stdlib).

См. README.md: поддержка ОС, явные привилегии и ограничения восстановления.
--dry-run не повышает права и не изменяет файловую систему.
"""

from __future__ import annotations

import argparse
import fcntl
import grp
import json
import locale
import os
import pwd
import re
import stat
import tempfile
import time
import shlex
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

try:
    import curses
except ImportError:  # нет curses: работает режим --no-tui
    curses = None  # type: ignore[assignment]

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_DOCKER_ROOT = "/opt/docker"

BASE_PACKAGES = ["sudo", "vim", "mc", "ncdu", "htop", "zsh", "git", "gh", "curl", "ca-certificates"]
EXTRA_PACKAGES = [
    "btop", "net-tools", "bash-completion", "wget", "tmux", "rsync", "unzip", "tree",
    "jq", "lsof", "dnsutils", "iotop", "gnupg", "fzf", "ripgrep",
]
DOCKER_PACKAGES = [
    "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin",
]
# Конфликтуют с docker-ce; удаляются, только если установлены.
DOCKER_CONFLICTS = ["docker.io", "docker-compose", "docker-compose-v2", "docker-buildx", "docker-doc", "podman-docker", "containerd", "runc"]

SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LOCK_PATH = Path("/run/lock/didaktik-bootstrap.lock")
OS_RELEASE = Path("/etc/os-release")
ZONEINFO = Path("/usr/share/zoneinfo")
LOCALTIME = Path("/etc/localtime")
TIMEZONE_FILE = Path("/etc/timezone")
SHELLS_FILE = Path("/etc/shells")
DAEMON_JSON = Path("/etc/docker/daemon.json")
DOCKER_KEY = Path("/etc/apt/keyrings/docker.asc")
DOCKER_SOURCES = Path("/etc/apt/sources.list.d/docker.sources")
APT_MIRROR_SOURCES = Path("/etc/apt/sources.list.d/00-didaktik-official-mirror.sources")

# Закреплённые коммиты. Обновление: поменять rev, посмотреть diff upstream.
OMZ_URL = "https://github.com/ohmyzsh/ohmyzsh.git"
OMZ_REV = "17a2b4c3a5855524ad4772a17ada0dc474e2fdf3"
CUSTOM_REPOS = [  # (url, путь относительно $ZSH/custom, коммит)
    ("https://github.com/romkatv/powerlevel10k.git", "themes/powerlevel10k",
     "35833ea15f14b71dbcebc7e54c104d8d56ca5268"),  # v1.20.0
    ("https://github.com/zsh-users/zsh-autosuggestions.git", "plugins/zsh-autosuggestions",
     "e52ee8ca55bcc56a17c828767a3f98f22a68d4eb"),  # v0.7.1
    ("https://github.com/zsh-users/zsh-history-substring-search.git",
     "plugins/zsh-history-substring-search",
     "400e58a87f72ecec14f783fbd29bc6be4ff1641c"),  # v1.1.0
    ("https://github.com/zsh-users/zsh-syntax-highlighting.git", "plugins/zsh-syntax-highlighting",
     "db085e4661f6aafd24e5acb5b2e17e4dd5dddf3e"),  # 0.8.0
]

DRY_RUN = False
ISSUES: List[str] = []
DOCKER_CMD = ["docker", "--host", "unix:///var/run/docker.sock"]
# Docker maintainer confirmation: https://github.com/docker/docs/issues/8891
DOCKER_FINGERPRINT = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"


class BootstrapError(RuntimeError):
    """Ошибка, после которой скрипт останавливается с понятным сообщением."""


def log(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    ISSUES.append(msg)
    print(f"ПРЕДУПРЕЖДЕНИЕ: {msg}", file=sys.stderr, flush=True)


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


# ---------------------------------------------------------------- выполнение команд

@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int
    home: Path

    @classmethod
    def lookup(cls, name: str) -> "Account":
        try:
            entry = pwd.getpwnam(name)
        except KeyError as exc:
            raise BootstrapError(f"нет пользователя: {name}") from exc
        return cls(entry.pw_name, entry.pw_uid, entry.pw_gid, Path(entry.pw_dir))


def run(
    args: Sequence[str],
    *,
    acc: Optional[Account] = None,
    check: bool = True,
    timeout: Optional[float] = None,
    env_extra: Optional[dict] = None,
    stream: bool = False,
    mutating: bool = True,
) -> subprocess.CompletedProcess:
    """Запускает команду, при необходимости от имени acc.

    mutating=False — команда только читает состояние: в --dry-run она выполняется.
    stream=True — вывод идёт прямо в консоль (apt-get).
    """
    cmd = [str(a) for a in args]
    if DRY_RUN and mutating:
        log(f"  [dry-run] {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    env = {**os.environ, "PATH": SYSTEM_PATH}
    if cmd and cmd[0] == "docker":
        for key in list(env):
            if key.startswith("DOCKER_"):
                env.pop(key)  # Never inspect a remote/context-selected daemon by accident.
    kwargs: dict = {}
    if acc is not None:
        env.update(HOME=str(acc.home), USER=acc.name, LOGNAME=acc.name)
        if os.geteuid() == 0:
            kwargs.update(user=acc.uid, group=acc.gid, extra_groups=os.getgrouplist(acc.name, acc.gid))
        elif acc.uid != os.geteuid():
            raise BootstrapError(f"нет прав выполнить проверку от {acc.name}")
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=not stream, text=True,
            timeout=timeout if timeout is not None else 1800, **kwargs
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(f"таймаут команды: {shlex.join(cmd)}") from exc
    except OSError as exc:
        raise BootstrapError(f"не удалось выполнить {shlex.join(cmd)}: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise BootstrapError(f"{' '.join(cmd)}: код {proc.returncode}: {detail}".rstrip(": "))
    return proc


def safe_path(path: Path, *, leaf_symlink: bool = False) -> None:
    """Reject symlinks conservatively; not a sandbox against concurrent hostile users."""
    if not path.is_absolute() or ".." in path.parts:
        raise BootstrapError(f"нужен абсолютный нормализованный путь: {path}")
    for part in [*reversed(path.parents), path]:
        if part.is_symlink() and not (part == path and leaf_symlink):
            raise BootstrapError(f"небезопасный symlink: {part}")


def acquire_lock():
    if DRY_RUN:
        return None
    safe_path(LOCK_PATH)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise BootstrapError("не удалось получить lock; возможно, скрипт уже запущен")
    return handle


def backup_file(path: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=path.name + ".bak.", dir=path.parent)
    os.close(fd)
    bak = Path(name)
    try:
        if path.is_symlink():
            # Preserve the link itself, never overwrite its target.
            bak.unlink()
            bak.symlink_to(os.readlink(path))
        else:
            shutil.copy2(path, bak)
            info = path.stat()
            os.chown(bak, info.st_uid, info.st_gid)
    except BaseException:
        bak.unlink(missing_ok=True)
        raise
    log(f"  бэкап: {bak}")
    return bak


def write_file(path: Path, content: bytes, *, mode: int = 0o644,
               uid: int = 0, gid: int = 0,
               validate: Optional[Callable[[Path], None]] = None,
               backup: bool = True, allow_leaf_symlink: bool = False) -> bool:
    tmp = None
    try:
        safe_path(path, leaf_symlink=allow_leaf_symlink)
        if path.exists() and not path.is_symlink():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise BootstrapError(f"не обычный файл: {path}")
            if (path.read_bytes() == content and stat.S_IMODE(info.st_mode) == mode
                    and (info.st_uid, info.st_gid) == (uid, gid)):
                return False
        if DRY_RUN:
            log(f"  [dry-run] записал бы {path} (валидация candidate не выполнена)")
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
        tmp = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchown(handle.fileno(), uid, gid)
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        if validate:
            validate(tmp)
        if backup and (path.exists() or path.is_symlink()):
            backup_file(path)
        safe_path(path, leaf_symlink=allow_leaf_symlink)
        os.replace(tmp, path)
        log(f"  записан {path}")
        return True
    except OSError as exc:
        raise BootstrapError(f"запись {path}: {exc}") from exc
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def user_groups(name: str) -> List[str]:
    proc = run(["id", "-nG", name], mutating=False)
    return proc.stdout.split()


# ---------------------------------------------------------------- проверки

@dataclass(frozen=True)
class SystemInfo:
    id: str
    version: str
    codename: str

    def __str__(self) -> str:
        return f"{self.id} {self.version} ({self.codename})"


def check_system() -> SystemInfo:
    if sys.platform != "linux" or not Path("/run/systemd/system").is_dir():
        raise BootstrapError("нужен Linux с работающим systemd (не macOS/WSL без systemd)")
    fields: Dict[str, str] = {}
    for line in OS_RELEASE.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value.strip().strip("\"'")
    sys_id = fields.get("ID", "")
    version = fields.get("VERSION_ID", "")
    major_text = version.split(".")[0]
    major = int(major_text) if major_text.isdigit() else 0
    if (sys_id == "debian" and major in (11, 12, 13)) or (sys_id == "ubuntu" and version in ("22.04", "24.04")):
        codename = fields.get("UBUNTU_CODENAME") or fields.get("VERSION_CODENAME", "")
        if not codename:
            raise BootstrapError("в /etc/os-release нет VERSION_CODENAME")
        return SystemInfo(sys_id, version, codename)
    raise BootstrapError(
        f"неподдерживаемая система: {sys_id} {version} (нужны Debian 11/12/13 или Ubuntu 22.04/24.04)"
    )


def check_zone(name: str) -> Path:
    if not name or name.startswith("/") or ".." in name:
        raise BootstrapError(f"некорректный часовой пояс: {name!r}")
    zone = ZONEINFO / name
    if not zone.is_file():
        raise BootstrapError(f"неизвестный часовой пояс: {name}")
    return zone


# ---------------------------------------------------------------- пакеты

APT_ENV = {"DEBIAN_FRONTEND": "noninteractive"}
APT_CONF = ["-o", "Dpkg::Options::=--force-confold", "-o", "Dpkg::Options::=--force-confdef"]


def package_installed(pkg: str) -> bool:
    proc = run(["dpkg-query", "-W", "-f=${Status}", pkg], check=False, mutating=False)
    if proc.returncode not in (0, 1):
        raise BootstrapError(f"dpkg-query {pkg}: {proc.stderr}")
    return proc.returncode == 0 and proc.stdout.strip() == "install ok installed"


def configure_official_mirror(system: SystemInfo) -> None:
    """Normalize official archive URIs, leaving security and third-party entries intact."""
    mirror = "http://deb.debian.org/debian" if system.id == "debian" else "http://archive.ubuntu.com/ubuntu"
    official_hosts = ("deb.debian.org", "ftp.debian.org") if system.id == "debian" else ("archive.ubuntu.com",)
    files = [Path("/etc/apt/sources.list")]
    sources_dir = Path("/etc/apt/sources.list.d")
    if sources_dir.is_dir():
        files.extend(sorted(sources_dir.glob("*.list")))
    for path in files:
        safe_path(path)
        if not path.is_file():
            continue
        original = path.read_text()
        changed = []
        for line in original.splitlines(keepends=True):
            if line.lstrip().startswith("#") or not re.match(r"\\s*deb(?:-src)?\\s", line):
                changed.append(line)
                continue
            replaced = line
            for host in official_hosts:
                replaced = re.sub(r"https?://" + re.escape(host) + r"(?=/|\\s)", mirror, replaced)
            changed.append(replaced)
        content = "".join(changed).encode()
        if content != original.encode():
            write_file(path, content, mode=stat.S_IMODE(path.stat().st_mode),
                       uid=path.stat().st_uid, gid=path.stat().st_gid)


def apt_update() -> None:
    run(["apt-get", "update"], env_extra=APT_ENV, stream=True)


def apt_install(packages: Sequence[str], *, recommends: bool = False) -> None:
    missing = [p for p in packages if not package_installed(p)]
    if not missing:
        log("  все нужные пакеты уже установлены")
        return
    log(f"  устанавливаю: {' '.join(missing)}")
    flags = [] if recommends else ["--no-install-recommends"]
    run(["apt-get", "install", "-y", *APT_CONF, *flags, *missing],
        env_extra=APT_ENV, stream=True)


# ---------------------------------------------------------------- шаги: система

def set_timezone(name: str) -> None:
    zone = check_zone(name)
    write_file(LOCALTIME, zone.read_bytes(), allow_leaf_symlink=True)
    write_file(TIMEZONE_FILE, (name + "\n").encode())
    run(["timedatectl", "set-timezone", name])
    run(["timedatectl", "set-ntp", "true"])


def setup_sudo(username: str) -> None:
    # root already has unrestricted privileges and must never receive a sudoers entry.
    if username == "root":
        log("  root: sudoers не настраивается")
        return
    if "sudo" not in user_groups(username):
        run(["usermod", "-aG", "sudo", username])
        log(f"  {username} добавлен в группу sudo")
    path = Path("/etc/sudoers.d") / username
    content = f"{username} ALL=(ALL) NOPASSWD: ALL\n".encode()

    def check(tmp: Path) -> None:
        run(["visudo", "-cf", str(tmp)])  # битый sudoers не попадёт на место

    if not write_file(path, content, mode=0o440, validate=check):
        log(f"  {path} уже актуален")
    elif not DRY_RUN:
        run(["visudo", "-cf", str(path)])


def set_default_editor() -> None:
    vim = Path("/usr/bin/vim.basic")
    if not vim.exists():
        if DRY_RUN:
            warn("[dry-run] vim.basic ещё не установлен; настройка editor не проверена")
            return
        raise BootstrapError("нет /usr/bin/vim.basic")
    current = Path("/etc/alternatives/editor")
    if current.exists() and current.resolve() == vim.resolve():
        log("  редактор уже vim")
        return
    run(["update-alternatives", "--set", "editor", str(vim)])


def disable_firewall() -> None:
    warn("Отключение firewall-сервисов не гарантирует отсутствие правил iptables/nftables; правила не очищаются")
    for svc in ("ufw", "firewalld", "nftables"):
        listed = run(["systemctl", "list-unit-files", f"{svc}.service", "--no-legend"],
                     check=False, mutating=False)
        if not listed.stdout.strip().startswith(svc):
            continue
        if run(["systemctl", "disable", "--now", svc], check=False).returncode != 0:
            raise BootstrapError(f"не удалось отключить {svc}")
        else:
            log(f"  отключён {svc}")
    if shutil.which("ufw"):
        run(["ufw", "--force", "disable"])


# ---------------------------------------------------------------- шаги: zsh

def template(name: str) -> bytes:
    path = TEMPLATES_DIR / name
    if not path.is_file():
        raise BootstrapError(f"нет шаблона: {path}")
    return path.read_bytes()


def ensure_repo(acc: Account, url: str, dest: Path, rev: str) -> None:
    """Делает dest git-репозиторием url на точном коммите rev."""
    safe_path(dest)
    if DRY_RUN and os.geteuid() != 0 and acc.uid != os.geteuid():
        warn(f"[dry-run] git-проверки {dest} не выполнены: нет прав {acc.name}")
        return
    new_repo = not dest.exists()
    if new_repo:
        if DRY_RUN:
            log(f"  [dry-run] git clone {url} -> {dest} @ {rev[:12]}")
            return
        run(["mkdir", "-p", str(dest)], acc=acc)
        run(["git", "-C", str(dest), "init", "-q"], acc=acc)
    if not (dest / ".git").is_dir():
        raise BootstrapError(f"не git-репозиторий: {dest}")

    remote = run(["git", "-C", str(dest), "remote", "get-url", "origin"],
                 acc=acc, check=False, mutating=False)
    if remote.returncode != 0:
        if not new_repo:
            raise BootstrapError(f"отсутствует origin в {dest}")
        run(["git", "-C", str(dest), "remote", "add", "origin", url], acc=acc)
    elif remote.stdout.strip() != url:
        raise BootstrapError(f"неожиданный origin в {dest}: {remote.stdout.strip()}")

    status = run(["git", "-C", str(dest), "status", "--porcelain", "--untracked-files=all",
                  "--", ".", *([f":(exclude)custom/{sub}/**" for _, sub, _ in CUSTOM_REPOS]
                                if url == OMZ_URL else [])], acc=acc, mutating=False)
    if status.stdout.strip():
        raise BootstrapError(f"локальные изменения в {dest}; сохраните их вручную")
    head_cmd = ["git", "-C", str(dest), "rev-parse", "-q", "--verify", "HEAD"]
    if run(head_cmd, acc=acc, check=False, mutating=False).stdout.strip() == rev:
        return
    log(f"  {dest.name}: {rev[:12]}")
    run(["git", "-C", str(dest), "fetch", "-q", "--depth=1", "origin", rev], acc=acc)
    run(["git", "-C", str(dest), "checkout", "-q", "--detach", rev], acc=acc)
    if DRY_RUN:
        return
    head = run(head_cmd, acc=acc).stdout.strip()
    if head != rev:
        raise BootstrapError(f"{dest}: HEAD={head}, ожидался {rev}")


def check_zsh_syntax(path: Path) -> None:
    if DRY_RUN and not shutil.which("zsh"):
        warn(f"[dry-run] синтаксис {path} не проверен: zsh ещё не установлен")
        return
    proc = run(["zsh", "-n", str(path)], check=False, mutating=False)
    if proc.returncode != 0:
        raise BootstrapError(
            f"синтаксическая ошибка в сгенерированном {path}: {proc.stderr.strip()}"
        )


def smoke_test(acc: Account) -> None:
    """Явно применяет ~/.zshrc в дочернем zsh и проверяет конфигурацию."""
    if DRY_RUN:
        return
    # run() supplies HOME/ZDOTDIR context and, for root, drops privileges to
    # this account.  The child shell cannot modify the caller's environment;
    # reconnect (or exec zsh) is still required to use the new login shell.
    zshrc = acc.home / ".zshrc"
    command = f"source -- {shlex.quote(str(zshrc))}"
    try:
        run(["zsh", "-i", "-c", command], acc=acc, timeout=60)
    except (BootstrapError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError(f"smoke-test zsh {acc.name}: {exc}") from exc


def setup_zsh(acc: Account) -> None:
    log(f"  zsh для {acc.name} ({acc.home})")
    if DRY_RUN and os.geteuid() != 0 and acc.uid != os.geteuid():
        warn(f"[dry-run] Zsh для {acc.name} запланирован; файлы и git не проверены без root")
        return
    safe_path(acc.home)
    for path in (acc.home / ".config" / "zsh", acc.home / ".cache" / "zsh" / "completions"):
        safe_path(path)
    omz = acc.home / ".oh-my-zsh"
    ensure_repo(acc, OMZ_URL, omz, OMZ_REV)
    run(["mkdir", "-p", str(omz / "custom" / "themes"), str(omz / "custom" / "plugins"),
         str(acc.home / ".config" / "zsh"), str(acc.home / ".cache" / "zsh" / "completions")],
        acc=acc)
    for url, sub, rev in CUSTOM_REPOS:
        ensure_repo(acc, url, omz / "custom" / sub, rev)

    def put(path: Path, name: str, **kw) -> bool:
        return write_file(path, template(name), uid=acc.uid, gid=acc.gid,
                          validate=check_zsh_syntax, **kw)

    cfg = acc.home / ".config" / "zsh"
    put(acc.home / ".zshrc", "zshrc")
    put(cfg / "provisioning-prompt.zsh", "provisioning-prompt.zsh")
    # Личные правки: создаётся один раз и больше не трогается.
    # ~/.p10k.zsh скрипт не создаёт и не меняет никогда.
    safe_path(cfg / "local.zsh")
    if not (cfg / "local.zsh").exists():
        put(cfg / "local.zsh", "local.zsh", backup=False)


def set_login_shell(acc: Account) -> None:
    zsh = shutil.which("zsh")
    if not zsh:
        if DRY_RUN:
            log("  [dry-run] chsh -s zsh")
            return
        raise BootstrapError("zsh не установлен")
    listed = [line.strip() for line in SHELLS_FILE.read_text().splitlines()]
    if zsh not in listed:
        log(f"  добавляю {zsh} в {SHELLS_FILE}")
        write_file(SHELLS_FILE, (SHELLS_FILE.read_text().rstrip("\n") + "\n" + zsh + "\n").encode())
    current = pwd.getpwnam(acc.name).pw_shell
    if current != zsh:
        log(f"  оболочка входа для {acc.name}: {current} -> {zsh}")
        run(["chsh", "-s", zsh, acc.name])


# ---------------------------------------------------------------- шаги: Docker

def docker_preflight(system: SystemInfo) -> str:
    supported = {("debian", "12"): "bookworm", ("debian", "13"): "trixie",
                 ("ubuntu", "22.04"): "jammy", ("ubuntu", "24.04"): "noble"}
    if supported.get((system.id, system.version)) != system.codename:
        raise BootstrapError(f"Docker repository не поддерживается скриптом: {system}")
    arch = run(["dpkg", "--print-architecture"], mutating=False).stdout.strip()
    allowed = {"amd64", "arm64", "armhf", "ppc64el"}
    if system.id == "ubuntu":
        allowed.add("s390x")
    if arch not in allowed:
        raise BootstrapError(f"неподдерживаемая Docker архитектура: {arch}")
    url = f"https://download.docker.com/linux/{system.id}/dists/{system.codename}/Release"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            release = response.read(1024 * 1024).decode()
    except (OSError, UnicodeError) as exc:
        raise BootstrapError(f"Docker repository недоступен: {exc}") from exc
    if f"stable/binary-{arch}/Packages" not in release:
        raise BootstrapError(f"в repository нет stable для {arch}")
    return arch


def verify_docker_key(key: bytes) -> None:
    # Isolated GnuPG home: no writes to /root/.gnupg, no implicit key import.
    with tempfile.TemporaryDirectory(prefix="bootstrap-gpg-") as directory:
        path = Path(directory) / "docker.asc"
        path.write_bytes(key)
        output = run(["gpg", "--batch", "--homedir", directory, "--with-colons",
                      "--show-keys", str(path)], mutating=False).stdout
    fingerprints = []
    primary = False
    for line in output.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            primary = True
        elif fields[0] == "sub":
            primary = False
        elif fields[0] == "fpr" and primary:
            fingerprints.append(fields[9])
            primary = False
    if fingerprints != [DOCKER_FINGERPRINT]:
        raise BootstrapError(f"неожиданный полный fingerprint ключа Docker: {fingerprints}")


def install_docker(system: SystemInfo, replace_runtime: bool = False) -> None:
    conflicts = [p for p in DOCKER_CONFLICTS if package_installed(p)]
    if conflicts and not replace_runtime:
        raise BootstrapError("конфликтующие пакеты: " + ", ".join(conflicts)
                             + "; требуется --replace-container-runtime")
    arch = docker_preflight(system)
    apt_install(["gnupg", "ca-certificates"])
    if DRY_RUN:
        warn("[dry-run] ключ Docker и новые APT candidates не проверены; удаление runtime только после проверки")
    else:
        url = f"https://download.docker.com/linux/{system.id}/gpg"
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                key = resp.read(1024 * 1024)
        except OSError as exc:
            raise BootstrapError(f"не удалось скачать ключ Docker: {exc}") from exc
        verify_docker_key(key)
        write_file(DOCKER_KEY, key)
    sources = (
        "Types: deb\n"
        f"URIs: https://download.docker.com/linux/{system.id}\n"
        f"Suites: {system.codename}\n"
        "Components: stable\n"
        f"Architectures: {arch}\n"
        f"Signed-By: {DOCKER_KEY}\n"
    ).encode()
    write_file(DOCKER_SOURCES, sources)
    # Strict update: do not proceed using stale indices after a mirror/network failure.
    run(["apt-get", "-o", "APT::Update::Error-Mode=any", "update"], env_extra=APT_ENV, stream=True)
    if not DRY_RUN:
        for package in DOCKER_PACKAGES:
            policy = run(["apt-cache", "policy", package], mutating=False,
                         env_extra={"LC_ALL": "C"}).stdout
            match = re.search(r"Candidate:\s+(\S+)", policy)
            if not match or match[1] == "(none)" or "download.docker.com" not in policy:
                raise BootstrapError(f"нет Docker repository candidate: {package}")
        # Resolve the complete replacement before removing anything.
        run(["apt-get", "--simulate", "install", *DOCKER_PACKAGES,
             *[p + "-" for p in conflicts]], mutating=False, env_extra=APT_ENV)
    if conflicts:
        run(["apt-get", "-y", "remove", *conflicts], env_extra=APT_ENV, stream=True)
    apt_install(DOCKER_PACKAGES, recommends=True)
    run(["systemctl", "enable", "--now", "docker"])
    wait_docker()


def add_docker_group(username: str) -> None:
    if username == "root":
        log("  root уже имеет права Docker; добавление в группу не требуется")
        return
    try:
        grp.getgrnam("docker")
    except KeyError as exc:
        raise BootstrapError("группа docker отсутствует: установка Docker не завершилась успешно") from exc
    if "docker" in user_groups(username):
        log(f"  {username} уже в группе docker")
        return
    run(["usermod", "-aG", "docker", username])
    log(f"  {username} добавлен в группу docker (root-equivalent)")


def daemon_config(data_root: Optional[str] = None) -> bytes:
    safe_path(DAEMON_JSON)
    cfg = {}
    if DAEMON_JSON.exists():
        try:
            cfg = json.loads(DAEMON_JSON.read_text())
        except (ValueError, UnicodeError) as exc:
            raise BootstrapError(f"{DAEMON_JSON}: невалидный JSON: {exc}") from exc
    if not isinstance(cfg, dict):
        raise BootstrapError(f"{DAEMON_JSON}: нужен JSON object")
    cfg.setdefault("log-driver", "json-file")
    if not isinstance(cfg.get("log-opts", {}), dict):
        raise BootstrapError("log-opts должен быть JSON object")
    if cfg["log-driver"] == "json-file":
        opts = cfg.setdefault("log-opts", {})
        opts.setdefault("max-size", "10m")
        opts.setdefault("max-file", "3")
    if data_root:
        cfg["data-root"] = data_root
    return (json.dumps(cfg, indent=2, ensure_ascii=False) + "\n").encode()


def validate_daemon(path: Path) -> None:
    run(["dockerd", "--validate", "--config-file", str(path)], mutating=False)


def configure_daemon_json(data_root: Optional[str]) -> bool:
    return write_file(DAEMON_JSON, daemon_config(data_root), validate=validate_daemon)


def docker_root_dir() -> str:
    value = run([*DOCKER_CMD, "info", "-f", "{{.DockerRootDir}}"],
                mutating=False, timeout=30).stdout.strip()
    if not value or not Path(value).is_absolute():
        raise BootstrapError(f"docker info вернул неверный data-root: {value!r}")
    return value


def wait_docker(attempts: int = 30) -> None:
    if DRY_RUN:
        warn("[dry-run] готовность Docker после изменений не проверена")
        return
    for attempt in range(attempts):
        try:
            docker_root_dir()
            return
        except BootstrapError:
            if attempt + 1 == attempts:
                raise
            time.sleep(1)


def snapshot_config():
    safe_path(DAEMON_JSON)
    if not DAEMON_JSON.exists():
        return None
    info = DAEMON_JSON.stat()
    return DAEMON_JSON.read_bytes(), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid


def restore_config(snapshot) -> None:
    safe_path(DAEMON_JSON)
    if snapshot is None:
        DAEMON_JSON.unlink(missing_ok=True)
    else:
        content, mode, uid, gid = snapshot
        write_file(DAEMON_JSON, content, mode=mode, uid=uid, gid=gid, backup=False)


DOCKER_SERVICES = ("docker.service", "docker.socket", "containerd.service")


def service_states() -> Dict[str, bool]:
    states = {}
    for service in DOCKER_SERVICES:
        proc = run(["systemctl", "is-active", service], check=False, mutating=False)
        value = proc.stdout.strip()
        if value not in ("active", "inactive"):
            raise BootstrapError(f"неоднозначное состояние {service}: {value} ({proc.returncode})")
        states[service] = value == "active"
    return states


def rollback_docker(snapshot, states: Dict[str, bool]) -> None:
    errors = []
    # Attempt every recovery operation, even if an earlier one failed.
    actions = [lambda: run(["systemctl", "stop", *DOCKER_SERVICES]),
               lambda: restore_config(snapshot)]
    for service in reversed(DOCKER_SERVICES):
        if states[service]:
            actions.append(lambda service=service: run(["systemctl", "start", service]))
    for action in actions:
        try:
            action()
        except BaseException as exc:
            errors.append(str(exc) or type(exc).__name__)
    try:
        if states["docker.service"]:
            wait_docker()
        if service_states() != states:
            errors.append("состояния сервисов не совпадают с исходными")
    except BaseException as exc:
        errors.append(str(exc) or type(exc).__name__)
    if errors:
        raise BootstrapError("rollback НЕ завершён: " + "; ".join(errors))
    log("  rollback: исходная конфигурация и состояния сервисов восстановлены")


def migration_paths(source: str, target: str):
    src, dst = Path(source), Path(target)
    for path in (src, dst):
        safe_path(path)
        if path == Path("/"):
            raise BootstrapError("data-root не может быть /")
    if src == dst:
        return src, dst
    if src in dst.parents or dst in src.parents:
        raise BootstrapError("source и target Docker пересекаются")
    if not src.is_dir():
        raise BootstrapError(f"исходный каталог отсутствует: {src}")
    if dst.exists() and (not dst.is_dir() or any(dst.iterdir())):
        raise BootstrapError(f"target должен отсутствовать или быть пустым каталогом: {dst}")
    return src, dst


def check_storage_layout(src: Path) -> None:
    raw = run([*DOCKER_CMD, "info", "--format", "{{json .}}"], mutating=False, timeout=30).stdout
    try:
        info = json.loads(raw)
    except ValueError as exc:
        raise BootstrapError("не удалось разобрать docker info") from exc
    if (not isinstance(info, dict) or info.get("Driver") not in ("overlay2", "vfs")
            or "io.containerd.snapshotter" in json.dumps(info.get("DriverStatus"))
            or any("rootless" in str(x) for x in info.get("SecurityOptions", []))
            or info.get("LiveRestoreEnabled") or info.get("Swarm", {}).get("LocalNodeState") != "inactive"):
        raise BootstrapError("миграция поддерживает только rootful classic overlay2/vfs, без swarm/live-restore; "
                             "containerd image store и внешний containerd storage не переносятся")
    # Refuse mounted subtrees: rsync is not a storage-driver migration tool.
    mounts = run(["findmnt", "--json", "--output", "TARGET"], mutating=False).stdout
    try:
        tree = json.loads(mounts)
        def mounted(nodes):
            for node in nodes:
                yield Path(node["target"])
                yield from mounted(node.get("children", []))
        if any(src in path.parents for path in mounted(tree["filesystems"])):
            raise BootstrapError("внутри Docker data-root есть mountpoints; перенос отклонён")
    except (ValueError, KeyError, TypeError) as exc:
        raise BootstrapError("не удалось проверить mountpoints") from exc


def move_docker(target: str) -> None:
    # Validate target even when the daemon isn't available in a non-root dry run.
    safe_path(Path(target))
    if Path(target) == Path("/"):
        raise BootstrapError("target не может быть /")
    if DRY_RUN and (not shutil.which("docker") or os.geteuid() != 0):
        warn("[dry-run] миграция не проверена: нужен установленный локальный Docker и root для чтения данных")
        return
    src, dst = migration_paths(docker_root_dir(), target)
    if src == dst:
        log(f"  Docker уже использует {dst}; перенос не требуется")
        return
    running = run([*DOCKER_CMD, "ps", "-q"], mutating=False, timeout=30).stdout.split()
    if running:
        raise BootstrapError(f"остановите контейнеры перед переносом: {len(running)}")
    check_storage_layout(src)
    if not shutil.which("rsync"):
        if not DRY_RUN:
            raise BootstrapError("для переноса нужен rsync")
        warn("[dry-run] rsync пока не установлен; копирование не проверено")
    size_text = run(["du", "-sb", str(src)], mutating=False).stdout.split()
    if not size_text or not size_text[0].isdigit():
        raise BootstrapError("не удалось оценить размер Docker data-root")
    parent = dst
    while not parent.exists():
        parent = parent.parent
    if shutil.disk_usage(parent).free < int(size_text[0]) * 1.1:
        raise BootstrapError(f"недостаточно места для переноса в {dst}")
    content = daemon_config(str(dst))
    if DRY_RUN:
        log(f"  [dry-run] stop Docker -> rsync {src}/ {dst}/ -> validate/write daemon.json -> start -> verify")
        warn("[dry-run] candidate daemon.json и фактическое переключение data-root не проверены")
        return
    # Validate before stopping, without changing daemon.json.
    safe_path(DAEMON_JSON)
    DAEMON_JSON.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".daemon.validate-", dir=DAEMON_JSON.parent) as candidate:
        candidate.write(content)
        candidate.flush()
        validate_daemon(Path(candidate.name))
    snapshot, states = snapshot_config(), service_states()
    try:
        run(["systemctl", "stop", *DOCKER_SERVICES])
        # Detect races with another client before copying any data.
        if any(service_states().values()):
            raise BootstrapError("Docker services не остановлены")
        safe_path(dst)
        dst.mkdir(parents=True, exist_ok=True)
        run(["rsync", "-aHAX", "--numeric-ids", f"{src}/", f"{dst}/"], stream=True)
        write_file(DAEMON_JSON, content, validate=validate_daemon)
        for service in reversed(DOCKER_SERVICES):
            if states[service]:
                run(["systemctl", "start", service])
        wait_docker()
        actual = docker_root_dir()
        if Path(actual) != dst:
            raise BootstrapError(f"Docker использует {actual}, ожидался {dst}")
    except BaseException as exc:
        try:
            rollback_docker(snapshot, states)
        except BaseException as recovery:
            raise BootstrapError(f"перенос прерван: {exc}; {recovery}; данные оставлены в {src} и {dst}") from exc
        raise
    log(f"  перенос проверен; старые данные оставлены в {src}. Удаляйте вручную после проверки.")


def setup_docker(opts: Dict[str, bool], username: str, system: SystemInfo, docker_root: str,
                 replace_runtime: bool = False) -> None:
    install_docker(system, replace_runtime)
    if opts["docker_group"]:
        add_docker_group(username)
    if opts["docker_move"]:
        move_docker(docker_root)
    elif DRY_RUN:
        try:
            configure_daemon_json(None)
        except PermissionError:
            warn("[dry-run] daemon.json недоступен для чтения; настройки не проверены")
        warn("[dry-run] restart Docker и daemon config не проверены")
    else:
        snapshot, states = snapshot_config(), service_states()
        try:
            if configure_daemon_json(None):
                run(["systemctl", "restart", "docker"])
            wait_docker()
        except BaseException as exc:
            try:
                rollback_docker(snapshot, states)
            except BaseException as recovery:
                raise BootstrapError(f"настройка Docker: {exc}; {recovery}") from exc
            raise


# ---------------------------------------------------------------- меню

@dataclass(frozen=True)
class Option:
    key: str
    title: str
    default: bool
    hint: str


PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]*\\Z")


def normalize_packages(values: Sequence[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        for name in value.split(","):
            name = name.strip()
            if not name:
                continue
            if not PACKAGE_RE.fullmatch(name):
                raise BootstrapError(f"недопустимое имя APT-пакета: {name}")
            if name not in result:
                result.append(name)
    return result


def build_options(username: str | Sequence[str], timezone: str, docker_root: str) -> List[Option]:
    if isinstance(username, str):
        usernames = [username] if username else []
    else:
        usernames = list(username)
    user_label = ", ".join(usernames)
    return [
        Option("official_mirror", "Официальное зеркало APT", True,
               "Debian: deb.debian.org; Ubuntu: archive.ubuntu.com. Сторонние и security-источники не меняются."),
        Option("upgrade", "Обновить систему (apt full-upgrade)", True,
               "Долгий шаг: обновляет все установленные пакеты."),
        Option("packages_extra", "Дополнительные утилиты (группа)", True,
               "Пробел переключает группу; отдельные пакеты можно отметить ниже."),
        *[Option("package_" + name, name, True, "Дополнительный пакет") for name in EXTRA_PACKAGES],
        Option("timezone", f"Часовой пояс {timezone} и NTP", True,
               "Другой пояс: флаг --timezone Region/City."),
        Option("sudo_nopasswd", f"sudo без пароля для {user_label}", False,
               "Файл /etc/sudoers.d/<user>; старый сохраняется в .bak, проверяется visudo."),
        Option("editor", "vim — редактор по умолчанию", True, "update-alternatives --set editor"),
        Option("zsh", "zsh + Oh My Zsh + Powerlevel10k (пользователь и root)", True,
               "Ваши шаблоны из templates/. ~/.p10k.zsh не трогается."),
        Option("docker", "Docker CE из официального репозитория", True,
               "Замена конфликтующих runtime требует --replace-container-runtime."),
        Option("docker_group", f"{user_label} в группе docker", False,
               "Внимание: доступ к docker равен root-доступу на хосте."),
        Option("docker_move", f"Перенести данные Docker в {docker_root}", False,
               "Останавливает Docker; работает только без запущенных контейнеров. "
               "Старый каталог остаётся на месте."),
        Option("firewall_off", "Отключить ufw / firewalld / nftables", False,
               "Отключает сервисы, но не гарантирует очистку всех правил. Опасно."),
    ]


def run_text_menu(options: List[Option], state: Dict[str, bool], notice: str) -> bool:
    """Запасное текстовое меню, если нет модуля curses. True — запускать, False — отмена."""
    while True:
        print("\nПервичная настройка сервера")
        print("Всегда ставятся: " + " ".join(BASE_PACKAGES))
        print("Свои пакеты: " + ", ".join(state.get("custom_packages", [])))
        for i, opt in enumerate(options, 1):
            print(f" {i:2}. [{'x' if state[opt.key] else ' '}] {opt.title}")
        if notice:
            print(notice)
        try:
            answer = input("Номера, c ПАКЕТЫ, Enter — запуск, q — отмена: ")
        except EOFError:
            return False
        answer = answer.strip()
        if answer.lower().startswith("c "):
            try:
                state["custom_packages"] = normalize_packages([answer[2:]])
            except BootstrapError as exc:
                print(f"ОШИБКА: {exc}")
            continue
        answer = answer.lower()
        if not answer:
            return True
        if answer == "q":
            return False
        for token in answer.split():
            if token.isdigit() and 1 <= int(token) <= len(options):
                key = options[int(token) - 1].key
                state[key] = not state[key]


def run_menu(options: List[Option], state: Dict[str, bool], notice: str = "") -> bool:
    """Меню с галочками. True — запускать, False — отмена."""
    locale.setlocale(locale.LC_ALL, "")

    def ui(stdscr) -> bool:
        curses.curs_set(0)
        pos = 0
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            def put(y: int, text: str, attr: int = 0) -> None:
                if 0 <= y < height:
                    try:
                        stdscr.addnstr(y, 0, text, max(width - 1, 1), attr)
                    except curses.error:
                        pass

            put(0, "Первичная настройка сервера", curses.A_BOLD)
            put(1, "↑/↓ — выбор   пробел — отметить   Enter — запустить   q — отмена")
            put(2, "Всегда ставятся: " + " ".join(BASE_PACKAGES))
            put(3, "Свои пакеты (c в текстовом меню): " + ", ".join(state.get("custom_packages", [])))
            for i, opt in enumerate(options):
                mark = "[x]" if state[opt.key] else "[ ]"
                put(4 + i, f" {mark} {opt.title}", curses.A_REVERSE if i == pos else 0)
            put(5 + len(options), options[pos].hint, curses.A_DIM)
            put(7 + len(options), notice, curses.A_BOLD)
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                pos = (pos - 1) % len(options)
            elif ch in (curses.KEY_DOWN, ord("j")):
                pos = (pos + 1) % len(options)
            elif ch == ord(" "):
                state[options[pos].key] = not state[options[pos].key]
            elif ch in (10, 13, curses.KEY_ENTER):
                return True
            elif ch in (ord("q"), 27):
                return False

    return curses.wrapper(ui)


def resolve_dependencies(state: Dict[str, bool]) -> None:
    if (state["docker_group"] or state["docker_move"]) and not state["docker"]:
        raise BootstrapError("docker_group/docker_move требуют docker; уберите конфликтующее отключение")


# ---------------------------------------------------------------- main

def default_user() -> str:
    if os.geteuid() != 0:
        return pwd.getpwuid(os.getuid()).pw_name
    caller = os.environ.get("SUDO_USER")
    if caller and caller != "root":
        return caller
    raise BootstrapError("при прямом запуске root укажите пользователя явно")


def ensure_users(names: Sequence[str], *, create: bool, interactive: bool) -> None:
    """Ensure selected accounts exist; new accounts have locked passwords."""
    missing = []
    for name in names:
        if name == "root":
            continue
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*[$]?", name):
            raise BootstrapError(f"имя пользователя небезопасно: {name}")
        try:
            pwd.getpwnam(name)
        except KeyError:
            missing.append(name)
    if not missing:
        return
    if not create:
        if interactive:
            answer = input("Отсутствуют пользователи " + ", ".join(missing) + ". Создать их? [y/N] ").strip().lower()
            if answer not in ("y", "yes", "д", "да"):
                raise BootstrapError("пользователи не созданы: " + ", ".join(missing))
        else:
            raise BootstrapError("нет пользователей: " + ", ".join(missing) + "; повторите с --create-users")
    for name in missing:
        run(["useradd", "--create-home", "--shell", "/bin/bash", name])
        run(["passwd", "--lock", name])
        log(f"  создан пользователь {name} (пароль заблокирован)")


def escalate(args: argparse.Namespace, state: Dict[str, bool], usernames: Sequence[str]) -> None:
    """Перезапускает скрипт от root с уже выбранными шагами. Обратно не возвращается."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--no-tui",
           *( ["--create-users"] if args.create_users else [] ),
           "--timezone", args.timezone, "--docker-root", args.docker_root]
    for key, enabled in state.items():
        cmd += ["--enable" if enabled else "--disable", key]
    if args.replace_container_runtime:
        cmd.append("--replace-container-runtime")
    cmd.extend([usernames] if isinstance(usernames, str) else usernames)

    if DRY_RUN:
        return
    sudo = shutil.which("sudo")
    if sudo:
        log("\nПовышаю права через sudo (может потребоваться ваш пароль) ...")
        argv = [sudo, "--", *cmd]
    elif shutil.which("su"):
        log("\nНужны права root. Введите пароль root (su):")
        argv = ["su", "-", "root", "-c", shlex.join(cmd)]
    else:
        raise BootstrapError("не удалось повысить права: нет ни su, ни sudo. Запустите от root.")
    sys.stdout.flush()
    os.execvp(argv[0], argv)


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    keys = [o.key for o in build_options("", "", "") if not o.key.startswith("package_")]
    p = argparse.ArgumentParser(
        description="Первичная настройка Debian/Ubuntu.",
        epilog="Ключи шагов: " + ", ".join(keys),
    )
    p.add_argument("users", nargs="*",
                   help="пользователи (от прямого root нужно указать явно)")
    p.add_argument("--timezone", default=DEFAULT_TIMEZONE, help=f"часовой пояс (по умолчанию: {DEFAULT_TIMEZONE})")
    p.add_argument("--docker-root", default=DEFAULT_DOCKER_ROOT, help=f"каталог данных Docker при переносе (по умолчанию: {DEFAULT_DOCKER_ROOT})")
    p.add_argument("--no-tui", action="store_true", help="без меню: применить значения по умолчанию и флаги")
    p.add_argument("--packages", action="append", default=[], metavar="ПАКЕТЫ",
                   help="добавить APT-пакеты (через запятую; имена [a-z0-9][a-z0-9+.-]*)")
    p.add_argument("--package", action="append", dest="packages", metavar="ПАКЕТ",
                   help="добавить один APT-пакет (можно повторять)")
    p.add_argument("--enable", action="append", default=[], choices=keys, metavar="ШАГ", help="включить шаг (можно несколько раз)")
    p.add_argument("--disable", action="append", default=[], choices=keys, metavar="ШАГ", help="выключить шаг (можно несколько раз)")
    p.add_argument("--dry-run", action="store_true", help="показать команды, ничего не меняя")
    p.add_argument("--create-users", action="store_true", help="создать отсутствующих пользователей (пароль заблокирован)")
    p.add_argument("--replace-container-runtime", action="store_true",
                   help="разрешить удаление конфликтующих container runtime пакетов")
    args = p.parse_args(argv)
    if set(args.enable) & set(args.disable):
        p.error("один шаг нельзя одновременно включить и отключить")
    return args


def main(argv: Optional[Sequence[str]] = None) -> None:
    global DRY_RUN
    args = parse_args(argv)
    DRY_RUN = args.dry_run

    needs_escalation = os.geteuid() != 0 and not DRY_RUN
    ISSUES.clear()
    lock = None

    system = check_system()
    log(f"система: {system}")
    selected_names = args.users or [default_user()]
    if not selected_names:
        raise BootstrapError("укажите хотя бы одного пользователя")
    selected_names = list(dict.fromkeys(selected_names))
    interactive = not args.no_tui and sys.stdin.isatty() and sys.stdout.isatty()
    if os.geteuid() == 0 and not DRY_RUN:
        ensure_users(selected_names, create=args.create_users, interactive=interactive)
    users = [Account.lookup(name) for name in selected_names]
    root = Account.lookup("root")
    options = build_options([account.name for account in users], args.timezone, args.docker_root)
    state = {o.key: o.default for o in options}
    state["custom_packages"] = normalize_packages(args.packages)
    for key in args.enable:
        state[key] = True
    for key in args.disable:
        state[key] = False
    interactive = not args.no_tui and sys.stdin.isatty() and sys.stdout.isatty()
    if interactive:
        notice = ("Запущено без root: после Enter sudo запросит пароль пользователя, либо su — root."
                  if needs_escalation else "")
        menu = run_menu if curses is not None else run_text_menu
        if not menu(options, state, notice):
            log("Отменено.")
            return
    resolve_dependencies(state)
    if state["timezone"]:
        check_zone(args.timezone)
    for user in users:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*[$]?", user.name):
            raise BootstrapError("имя пользователя небезопасно для sudoers")
        safe_path(user.home)
        if not user.home.is_dir():
            raise BootstrapError(f"нет домашнего каталога: {user.home}")
    if state["zsh"]:
        for name in ("zshrc", "provisioning-prompt.zsh", "local.zsh"):
            template(name)
    if state["docker"]:
        docker_preflight(system)
        conflicts = [p for p in DOCKER_CONFLICTS if package_installed(p)]
        if conflicts and not args.replace_container_runtime:
            raise BootstrapError("конфликтующие runtime: " + ", ".join(conflicts)
                                 + "; требуется --replace-container-runtime")
    if state["docker_move"]:
        safe_path(Path(args.docker_root))
        if Path(args.docker_root) == Path("/"):
            raise BootstrapError("Docker target не может быть /")
    if needs_escalation:
        escalate(args, state, [account.name for account in users])
    if DRY_RUN:
        log("режим --dry-run: изменения не вносятся")
    log("шаги: " + ", ".join(k for k, v in state.items() if v))

    completed: List[str] = []
    try:
        lock = acquire_lock()
        step("Пакеты")
        packages = list(BASE_PACKAGES)
        if state["packages_extra"]:
            packages += [name for name in EXTRA_PACKAGES if state.get("package_" + name, False)]
        packages += state.get("custom_packages", [])
        packages = normalize_packages(packages)
        if state["docker_move"] and "rsync" not in packages:
            packages.append("rsync")
        if state["official_mirror"]:
            step("Официальное зеркало APT")
            configure_official_mirror(system)
        apt_update()
        if state["upgrade"]:
            run(["apt-get", "-y", *APT_CONF, "full-upgrade"], env_extra=APT_ENV, stream=True)
        apt_install(packages, recommends=True)
        completed.append("packages")
    
        if state["timezone"]:
            step("Часовой пояс")
            set_timezone(args.timezone)
            completed.append("timezone")
        if state["sudo_nopasswd"]:
            step("sudo без пароля")
            for user in users:
                setup_sudo(user.name)
            completed.append("sudo_nopasswd")
        if state["editor"]:
            step("Редактор по умолчанию")
            set_default_editor()
            completed.append("editor")
        if state["zsh"]:
            step("zsh")
            zsh_accounts = list(users)
            if all(acc.name != "root" for acc in zsh_accounts):
                zsh_accounts.append(root)
            for acc in zsh_accounts:
                setup_zsh(acc)
                set_login_shell(acc)
                smoke_test(acc)
            completed.append("zsh")
        if state["docker"]:
            step("Docker")
            setup_docker({**state, "docker_group": False}, users[0].name, system, args.docker_root, args.replace_container_runtime)
            if state["docker_group"]:
                for user in users:
                    add_docker_group(user.name)
            completed.append("docker")
        if state["firewall_off"]:
            step("Фаервол")
            disable_firewall()
    
            completed.append("firewall_off")
    except BaseException as exc:
        warn(f"выполнение остановлено: {exc or type(exc).__name__}")
        raise
    finally:
        if lock:
            lock.close()
        label = "Запланированы (не выполнены)" if DRY_RUN else "Завершены"
        log(f"\n{label}: " + (", ".join(completed) or "нет"))
        log("Отключены: " + (", ".join(k for k, v in state.items() if not v) or "нет"))
        pending = [k for k, v in state.items() if v and k not in completed
                   and k not in ("upgrade", "packages_extra", "docker_group", "docker_move")]
        if pending:
            log("Не завершены: " + ", ".join(pending))
        for key, parent in (("upgrade", "packages"), ("packages_extra", "packages"),
                            ("docker_group", "docker"), ("docker_move", "docker")):
            if state[key]:
                status = ("запланирован" if DRY_RUN else "завершён") if parent in completed else "не завершён"
                log(f"  {key}: {status}")
        log(f"Предупреждений / непроверенных действий: {len(ISSUES)}")
    if not DRY_RUN:
        log("Выбранные шаги завершены. Переподключитесь для применения групп и shell.")


if __name__ == "__main__":
    try:
        main()
    except (BootstrapError, OSError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nПрервано.", file=sys.stderr)
        sys.exit(130)
