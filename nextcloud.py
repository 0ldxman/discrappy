"""
Заливка файла на Nextcloud через WebDAV + создание публичной ссылки через OCS.

Аутентификация — логин + app-password (Настройки → Безопасность → App password),
а НЕ основной пароль аккаунта.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote, unquote

import httpx


@dataclass
class NextcloudConfig:
    base_url: str          # https://cloud.example.com  (без завершающего /)
    username: str
    app_password: str
    remote_dir: str        # папка назначения, напр. "discord-scrapes"

    @classmethod
    def from_env(cls) -> "NextcloudConfig":
        return cls._build(
            os.getenv("NEXTCLOUD_URL", ""),
            os.getenv("NEXTCLOUD_USER", ""),
            os.getenv("NEXTCLOUD_APP_PASSWORD", ""),
            os.getenv("NEXTCLOUD_DIR", "discord-scrapes"),
        )

    @classmethod
    def from_mapping(cls, cfg: dict) -> "NextcloudConfig":
        """Из словаря настроек (ключи nextcloud_*) — используется веб-приложением."""
        return cls._build(
            cfg.get("nextcloud_url", ""),
            cfg.get("nextcloud_user", ""),
            cfg.get("nextcloud_app_password", ""),
            cfg.get("nextcloud_dir", "discord-scrapes"),
        )

    @classmethod
    def _build(cls, base: str, user: str, pwd: str, remote_dir: str) -> "NextcloudConfig":
        base = (base or "").strip().rstrip("/")
        user = (user or "").strip()
        pwd = (pwd or "").strip()
        remote_dir = (remote_dir or "discord-scrapes").strip().strip("/")
        missing = [n for n, v in [
            ("NEXTCLOUD_URL", base),
            ("NEXTCLOUD_USER", user),
            ("NEXTCLOUD_APP_PASSWORD", pwd),
        ] if not v]
        if missing:
            raise ValueError("Не заданы настройки Nextcloud: " + ", ".join(missing))
        return cls(base_url=base, username=user, app_password=pwd, remote_dir=remote_dir)

    @property
    def auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.username, self.app_password)

    def _dav_url(self, remote_path: str) -> str:
        # remote_path — путь относительно корня файлов пользователя
        encoded = quote(remote_path)
        return f"{self.base_url}/remote.php/dav/files/{quote(self.username)}/{encoded}"


async def _ensure_dir(client: httpx.AsyncClient, cfg: NextcloudConfig, remote_dir: str) -> None:
    """Создаёт папку назначения (по сегментам). 405 = уже существует — это ок."""
    if not remote_dir:
        return
    path = ""
    for segment in remote_dir.split("/"):
        path = f"{path}/{segment}" if path else segment
        resp = await client.request("MKCOL", cfg._dav_url(path), auth=cfg.auth)
        if resp.status_code not in (201, 405):  # 201 создано, 405 уже есть
            resp.raise_for_status()


async def upload_file(
    cfg: NextcloudConfig, local_path: str, remote_name: str, remote_dir: str | None = None
) -> str:
    """Заливает файл, возвращает remote_path. remote_dir переопределяет папку из конфига."""
    remote_dir = (remote_dir if remote_dir is not None else cfg.remote_dir).strip("/")
    remote_path = f"{remote_dir}/{remote_name}" if remote_dir else remote_name
    with open(local_path, "rb") as f:
        data = f.read()
    async with httpx.AsyncClient(timeout=60) as client:
        await _ensure_dir(client, cfg, remote_dir)
        resp = await client.put(cfg._dav_url(remote_path), content=data, auth=cfg.auth)
        if resp.status_code not in (201, 204):  # 201 создан, 204 перезаписан
            resp.raise_for_status()
    return remote_path


async def list_folders(cfg: NextcloudConfig, path: str = "") -> list[str]:
    """Список подпапок каталога `path` (относительно корня файлов) через PROPFIND."""
    import xml.etree.ElementTree as ET

    path = path.strip("/")
    body = (
        '<?xml version="1.0"?>'
        '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            "PROPFIND", cfg._dav_url(path), auth=cfg.auth,
            headers={"Depth": "1", "Content-Type": "application/xml"}, content=body,
        )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()

    ns = {"d": "DAV:"}
    root = ET.fromstring(resp.text)
    base_prefix = f"/remote.php/dav/files/{cfg.username}/".rstrip("/")
    folders: list[str] = []
    for response in root.findall("d:response", ns):
        href = response.findtext("d:href", default="", namespaces=ns)
        is_collection = response.find(".//d:resourcetype/d:collection", ns) is not None
        if not is_collection:
            continue
        rel = unquote(href).rstrip("/")
        if rel.startswith(base_prefix):
            rel = rel[len(base_prefix):].strip("/")
        # пропускаем сам запрошенный каталог, оставляем только его прямых детей
        if rel and rel != path:
            folders.append(rel.rsplit("/", 1)[-1])
    return sorted(folders)


async def create_public_share(cfg: NextcloudConfig, remote_path: str) -> str:
    """Создаёт публичную ссылку (read-only) на файл, возвращает URL."""
    url = f"{cfg.base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url,
            auth=cfg.auth,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
            data={
                "path": f"/{remote_path}",
                "shareType": 3,   # 3 = public link
                "permissions": 1,  # read-only
            },
        )
    resp.raise_for_status()
    payload = resp.json()
    try:
        return payload["ocs"]["data"]["url"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Не удалось разобрать ответ OCS: {payload}") from exc


async def upload_and_share(
    cfg: NextcloudConfig,
    local_path: str,
    remote_name: str,
    *,
    remote_dir: str | None = None,
    share: bool = True,
) -> tuple[str, str | None]:
    """Заливает файл и (опц.) создаёт публичную ссылку. -> (remote_path, url|None)."""
    remote_path = await upload_file(cfg, local_path, remote_name, remote_dir=remote_dir)
    link = await create_public_share(cfg, remote_path) if share else None
    return remote_path, link
