from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

import httpx
from astrbot.api import logger

try:
    from .config import (
        DEFAULT_ALLOWED_MEDIA_DOMAINS,
        load_allowed_media_domains,
        load_media_max_bytes,
    )
    from .serializer import cq_param_exists, read_image_dimensions
except ImportError:
    from config import (
        DEFAULT_ALLOWED_MEDIA_DOMAINS,
        load_allowed_media_domains,
        load_media_max_bytes,
    )
    from serializer import cq_param_exists, read_image_dimensions


class ArchiveMediaCache:
    _URL_EXTENSIONS = {
        ".png",
        ".gif",
        ".webp",
        ".jpg",
        ".jpeg",
        ".mp4",
        ".webm",
        ".avi",
        ".mkv",
    }
    _CONTENT_TYPE_EXTENSIONS = {
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }

    def __init__(self, *, config, cache_dir: Path):
        self.conf = config
        self.cache_dir = cache_dir
        self._download_locks: dict[str, asyncio.Lock] = {}
        self._download_lock_refs: dict[str, int] = {}
        self._download_locks_guard = asyncio.Lock()

    def get_allowed_media_domains(self) -> set[str]:
        """Return configured media-cache domain allowlist."""
        if os.environ.get("ARCHIVE_ALLOWED_MEDIA_DOMAINS", "").strip():
            return set(load_allowed_media_domains())
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        configured = basic_conf.get("allowed_media_domains", [])
        domains = configured or list(DEFAULT_ALLOWED_MEDIA_DOMAINS)
        return {str(d).strip().lower().rstrip(".") for d in domains if str(d).strip()}

    def get_max_media_bytes(self) -> int:
        if os.environ.get("ARCHIVE_MEDIA_MAX_MB", "").strip():
            return load_media_max_bytes()
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        try:
            mb = int(basic_conf.get("media_max_mb", 50))
        except (TypeError, ValueError):
            mb = 50
        mb = max(1, min(mb, 200))
        return mb * 1024 * 1024

    @staticmethod
    def hostname_matches_allowlist(hostname: str, domains: set[str]) -> bool:
        host = (hostname or "").lower().rstrip(".")
        return any(host == d or host.endswith("." + d) for d in domains)

    @staticmethod
    def ip_is_public(ip: ipaddress._BaseAddress, allow_fake_ip: bool = False) -> bool:
        if allow_fake_ip and isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.IPv4Network("198.18.0.0/15"):
            return True
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    def is_allow_fake_ip(self) -> bool:
        env_val = os.environ.get("ARCHIVE_ALLOW_FAKE_IP", "").strip().lower()
        if env_val:
            return env_val in ("1", "true", "yes", "on")
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        return bool(basic_conf.get("allow_fake_ip", True))

    async def hostname_resolves_to_public_ips(self, hostname: str) -> bool:
        """Block localhost/private/link-local destinations before media caching."""
        allow_fake_ip = self.is_allow_fake_ip()
        try:
            try:
                ip = ipaddress.ip_address(hostname)
                return self.ip_is_public(ip, allow_fake_ip=allow_fake_ip)
            except ValueError:
                pass

            infos = await asyncio.to_thread(
                socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM
            )
            if not infos:
                return False
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if not self.ip_is_public(ip, allow_fake_ip=allow_fake_ip):
                    logger.warning(f"Chat Archive: 拒绝缓存解析到非公网地址的媒体域名 {hostname} -> {ip}")
                    return False
            return True
        except Exception as e:
            logger.warning(f"Chat Archive: 媒体域名解析校验失败 {hostname}: {e}")
            return False

    def _guess_extension(self, url: str, *, is_video_hint: bool = False) -> str:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix == ".jpeg":
            return ".jpg"
        if suffix in self._URL_EXTENSIONS:
            return suffix
        return ".mp4" if is_video_hint else ".jpg"

    async def download_media_to_cache(self, url: str) -> str:
        """Download media into the local web cache and return its /static/cache path."""
        if not url:
            return ""

        if url.startswith("/static/cache/"):
            return url

        parsed_url = urlparse(url)
        if parsed_url.scheme not in ("http", "https"):
            logger.warning(f"Chat Archive: 拒绝下载危险的 URL 协议 {url}")
            return url
        hostname = parsed_url.hostname
        if not hostname:
            logger.warning(f"Chat Archive: 拒绝下载无效媒体 URL {url}")
            return url
        allowed_domains = self.get_allowed_media_domains()
        if not self.hostname_matches_allowlist(hostname, allowed_domains):
            logger.warning(f"Chat Archive: 拒绝缓存非白名单媒体域名 {hostname}")
            return url
        if not await self.hostname_resolves_to_public_ips(hostname):
            return url
        max_media_bytes = self.get_max_media_bytes()

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Chat Archive: 创建缓存目录失败: {e}")
            return url

        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]

        async with self._download_locks_guard:
            download_lock = self._download_locks.get(url_hash)
            if download_lock is None:
                download_lock = asyncio.Lock()
                self._download_locks[url_hash] = download_lock
            self._download_lock_refs[url_hash] = self._download_lock_refs.get(url_hash, 0) + 1

        try:
            async with download_lock:
                ext = self._guess_extension(url, is_video_hint="video" in url.lower())
                filename = f"{url_hash}{ext}"
                dest_path = self.cache_dir / filename
                relative_url = f"/static/cache/{filename}"

                if dest_path.exists():
                    return relative_url

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
                    "Referer": "https://q.qq.com/",
                }

                try:
                    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                        async with client.stream("GET", url, headers=headers) as response:
                            if response.status_code != 200:
                                logger.warning(f"Chat Archive: 下载媒体失败 {url}, 状态码 {response.status_code}")
                                return url

                            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                            if not (content_type.startswith("image/") or content_type.startswith("video/")):
                                logger.warning(f"Chat Archive: 拒绝缓存非图片/视频响应 {url}, content-type={content_type or 'unknown'}")
                                return url

                            content_length = response.headers.get("content-length")
                            if content_length:
                                try:
                                    if int(content_length) > max_media_bytes:
                                        logger.warning(f"Chat Archive: 拒绝缓存超大媒体 {url}, size={content_length}")
                                        return url
                                except ValueError:
                                    pass

                            new_ext = self._CONTENT_TYPE_EXTENSIONS.get(content_type, "")
                            if new_ext:
                                filename = f"{url_hash}{new_ext}"
                                dest_path = self.cache_dir / filename
                                relative_url = f"/static/cache/{filename}"
                                if dest_path.exists():
                                    return relative_url

                            temp_path = dest_path.with_suffix(".tmp")
                            downloaded = 0
                            try:
                                with open(temp_path, "wb") as f:
                                    async for chunk in response.aiter_bytes():
                                        downloaded += len(chunk)
                                        if downloaded > max_media_bytes:
                                            raise ValueError(f"media too large: {downloaded} bytes")
                                        f.write(chunk)
                                temp_path.rename(dest_path)
                            except Exception:
                                try:
                                    temp_path.unlink(missing_ok=True)
                                except Exception:
                                    pass
                                raise
                            logger.info(f"Chat Archive: 成功缓存媒体到 {dest_path}")
                            return relative_url
                except Exception as e:
                    logger.error(f"Chat Archive: 下载媒体异常 {url}: {e}")

                return url
        finally:
            async with self._download_locks_guard:
                refs = self._download_lock_refs.get(url_hash, 0) - 1
                if refs <= 0:
                    self._download_lock_refs.pop(url_hash, None)
                    self._download_locks.pop(url_hash, None)
                else:
                    self._download_lock_refs[url_hash] = refs

    async def replace_cq_media_url(self, match) -> str:
        cq_type = match.group(1)
        inner = match.group(2)
        url_match = re.search(r"url=(https?://[^,\]]+)", inner)
        if url_match:
            original_url = url_match.group(1)
            url = (
                original_url.replace("&amp;", "&")
                .replace("&#44;", ",")
                .replace("&#91;", "[")
                .replace("&#93;", "]")
            )
            cached_url = await self.download_media_to_cache(url)
            new_inner = inner.replace(original_url, cached_url)
            if (
                cq_type == "image"
                and not cq_param_exists(new_inner, "width")
                and not cq_param_exists(new_inner, "height")
                and cached_url.startswith("/static/cache/")
            ):
                cached_path = self.cache_dir / cached_url.rsplit("/", 1)[-1]
                dims = read_image_dimensions(str(cached_path))
                if dims:
                    new_inner += f",width={dims[0]},height={dims[1]}"
            return f"[CQ:{cq_type},{new_inner}]"
        return match.group(0)

    async def process_and_cache_media_in_string(self, text: str) -> str:
        """Replace CQ image/video URLs in text with local cache URLs when possible."""
        if not text:
            return text

        pattern = r"\[CQ:(image|video),([^\]]+)\]"
        matches = list(re.finditer(pattern, text))
        if not matches:
            return text

        for match in reversed(matches):
            replaced = await self.replace_cq_media_url(match)
            start, end = match.span()
            text = text[:start] + replaced + text[end:]

        return text
