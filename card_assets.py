"""Resolve V3 primary icons without changing the author's resource declarations."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import ipaddress
import socket
import warnings
from dataclasses import dataclass

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError
from yarl import URL

from .cards import MAX_UPLOAD_BYTES, PNG_SIGNATURE, _chunk, _png_text, png_chunks
from .domain import StudioError, digest

IMAGE_TYPES = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
}


def invalid(message):
    return StudioError("invalid_asset", message)


def main_icon(document: dict) -> dict | None:
    assets = document["data"].get("assets")
    if assets is None:
        return {"type": "icon", "name": "main", "uri": "ccdefault:", "ext": "png"}
    icons = [asset for asset in assets if asset.get("type") == "icon"]
    mains = [asset for asset in icons if asset.get("name") == "main"]
    if len(mains) > 1 or (len(icons) > 1 and not mains):
        raise invalid("多个 icon 资源必须且只能声明一个 name=main 主头像。")
    icon = mains[0] if mains else icons[0] if icons else None
    if icon is not None and (not isinstance(icon.get("uri"), str) or not icon["uri"]):
        raise invalid("主头像资源缺少有效的 uri。")
    return icon


@dataclass(frozen=True)
class CardImage:
    raw: bytes
    media_type: str

    @property
    def data_url(self):
        return f"data:{self.media_type};base64," + base64.b64encode(self.raw).decode(
            "ascii"
        )


def image_info(raw: bytes) -> tuple[str, str]:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise invalid("头像文件最大为 20 MiB。")
    if raw.startswith(PNG_SIGNATURE):
        # Card and embedded-asset text has its own bounds. Do not let Pillow's
        # unrelated text limits reject otherwise valid portrait pixels.
        raw = PNG_SIGNATURE + b"".join(
            _chunk(kind, payload)
            for kind, payload in png_chunks(raw)
            if kind not in (b"tEXt", b"zTXt", b"iTXt")
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in IMAGE_TYPES:
                    raise invalid("头像仅支持 PNG、JPEG 或 WebP 图片。")
                width, height = image.size
                if (
                    min(width, height) < 1
                    or max(width, height) > 8192
                    or width * height > 32_000_000
                ):
                    raise invalid("头像尺寸不能超过 8192 边长或 3200 万像素。")
                kind = IMAGE_TYPES[image.format]
                image.verify()
            # verify() alone does not decode the image data (notably for JPEG).
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
            return kind
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise invalid("头像图片损坏或格式无效。") from exc


def image_png(image: CardImage) -> bytes:
    if image.media_type == "image/png":
        return image.raw
    with Image.open(io.BytesIO(image.raw)) as source:
        output = io.BytesIO()
        ImageOps.exif_transpose(source).convert("RGBA").save(output, format="PNG")
        return output.getvalue()


def prepare_image(raw: bytes, *, avatar: bool = False) -> bytes:
    """Validate pixels, fix orientation, and strip uploaded metadata."""
    image_info(raw)
    if raw.startswith(PNG_SIGNATURE):
        raw = PNG_SIGNATURE + b"".join(
            _chunk(kind, payload)
            for kind, payload in png_chunks(raw)
            if kind not in (b"tEXt", b"zTXt", b"iTXt")
        )
    with Image.open(io.BytesIO(raw)) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
        if avatar:
            image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        image.info.clear()
        output = io.BytesIO()
        image.save(output, format="PNG")
        result = output.getvalue()
    if len(result) > MAX_UPLOAD_BYTES:
        raise invalid("处理后的图片超过 20 MiB，请先缩小图片。")
    return result


def set_main_icon(document: dict, uri: str):
    """Explicit image editing can repair duplicate/missing main declarations."""
    assets = document["data"].get("assets", [])
    icons = [a for a in assets if a.get("type") == "icon"]
    mains = [a for a in icons if a.get("name") == "main"]
    replaced = mains[0] if mains else icons[0] if len(icons) == 1 else None
    main = {
        **(replaced or {}),
        "type": "icon",
        "name": "main",
        "uri": uri,
        "ext": "png",
    }
    document["data"]["assets"] = [
        a
        for a in assets
        if a is not replaced
        and not (a.get("type") == "icon" and a.get("name") == "main")
    ] + [main]


def data_image(uri: str) -> bytes:
    header, separator, encoded = uri.partition(",")
    if not separator or header.lower() not in {
        "data:image/png;base64",
        "data:image/jpeg;base64",
        "data:image/webp;base64",
    }:
        raise invalid("头像 Data URL 必须为 PNG、JPEG 或 WebP 的 Base64 数据。")
    if len(encoded) > (MAX_UPLOAD_BYTES + 2) // 3 * 4:
        raise invalid("头像文件最大为 20 MiB。")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise invalid("头像 Data URL 的 Base64 数据无效。") from exc
    # Never trust ext, Content-Type or the declared Data URL MIME for display.
    return raw


def public_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address):
            if address.ipv4_mapped:
                return public_ip(str(address.ipv4_mapped))
            if address in ipaddress.ip_network("::/96"):
                return False
            if address in ipaddress.ip_network("64:ff9b::/96"):
                return public_ip(str(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)))
            if address.sixtofour and not public_ip(str(address.sixtofour)):
                return False
            if address.teredo and not all(
                public_ip(str(part)) for part in address.teredo
            ):
                return False
        return address.is_global and not address.is_multicast
    except ValueError:
        return False


def remote_url(uri: str) -> URL:
    try:
        url = URL(uri)
        host = url.host
        if (
            url.scheme != "https"
            or not host
            or url.user is not None
            or url.password is not None
        ):
            raise ValueError("unsupported URL")
        if url.port != 443:
            raise ValueError("unsupported port")
        # aiohttp bypasses DNS resolvers for numeric hosts; check them here.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if ":" in host or host.replace(".", "").isdigit():
                raise ValueError("non-canonical address") from None
        else:
            if not public_ip(host):
                raise ValueError("non-public address")
        if "%" in host:
            raise ValueError("scoped address")
        return url
    except (TypeError, ValueError) as exc:
        raise invalid("远程头像仅支持无凭据、443 端口的公网 HTTPS 地址。") from exc


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate the exact DNS results passed to the connection, on every redirect."""

    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self.resolver.resolve(host, port, family)
        if not records or any(not public_ip(record["host"]) for record in records):
            raise invalid("远程头像地址解析到了非公网 IP，已拒绝读取。")
        return records

    async def close(self):
        await self.resolver.close()


async def _download(uri: str) -> bytes:
    resolver = PublicResolver()
    try:
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=12, connect=5),
            trust_env=False,
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={
                "Accept": "image/png,image/jpeg,image/webp",
                "Accept-Encoding": "identity",
            },
        ) as session:
            url = remote_url(uri)
            for _ in range(4):
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise invalid("远程头像重定向缺少目标地址。")
                        url = remote_url(str(url.join(URL(location))))
                        continue
                    if response.status != 200:
                        raise invalid("远程头像下载失败，请检查资源地址。")
                    if (
                        response.content_length is not None
                        and response.content_length > MAX_UPLOAD_BYTES
                    ):
                        raise invalid("远程头像文件超过 20 MiB。")
                    raw = bytearray()
                    async for block in response.content.iter_chunked(65536):
                        raw.extend(block)
                        if len(raw) > MAX_UPLOAD_BYTES:
                            raise invalid("远程头像文件超过 20 MiB。")
                    return bytes(raw)
            raise invalid("远程头像重定向次数过多。")
    finally:
        await resolver.close()


async def download_image(uri: str) -> bytes:
    remote_url(uri)
    try:
        return await asyncio.wait_for(_download(uri), timeout=15)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
        raise invalid("远程头像暂时无法读取，请稍后刷新重试。") from exc


class CardAssets:
    def __init__(self, storage):
        self.storage = storage
        self.lock = asyncio.Lock()

    async def original(self, asset_id):
        if not asset_id:
            return None
        try:

            def read():
                with self.storage.asset_path(asset_id).open("rb") as stream:
                    raw = stream.read(MAX_UPLOAD_BYTES + 1)
                if len(raw) > MAX_UPLOAD_BYTES:
                    raise invalid("图片资源超过 20 MiB。")
                if hashlib.sha256(raw).hexdigest() != asset_id.split(".", 1)[0]:
                    raise invalid("图片内容校验失败，请恢复原始资源文件。")
                return raw

            return await asyncio.to_thread(read)
        except OSError as exc:
            raise invalid("图片资源缺失或无法读取。") from exc

    async def resolve(self, document, asset_id=None) -> CardImage | None:
        icon = main_icon(document)
        if icon is None:
            return None
        uri = icon["uri"]
        scheme = uri.split(":", 1)[0].lower()
        if uri.lower() == "ccdefault:":
            raw = await self.original(asset_id)
            if raw is None:
                return None
            media_type, _ = await asyncio.to_thread(image_info, raw)
            return CardImage(raw, media_type)
        key = digest(
            {"uri": uri, "container": asset_id if uri.startswith("__asset:") else None}
        )
        async with self.lock:
            cached = self.storage.cached_asset(key)
            if cached:
                raw = await self.original(cached["asset_id"])
                media_type, _ = await asyncio.to_thread(image_info, raw)
                return CardImage(raw, media_type)
            if scheme == "data":
                raw = data_image(uri)
            elif scheme == "https":
                raw = await download_image(uri)
            elif uri.startswith("__asset:"):
                original = await self.original(asset_id)
                if original is None:
                    raise invalid("内嵌头像需要随卡片导入的原始 PNG。")
                raw = await asyncio.to_thread(
                    self.embedded, original, uri[len("__asset:") :]
                )
            elif uri.startswith(("embeded://", "embedded://")):
                raise invalid(
                    "该头像引用 CHARX 包内资源；当前仅导入 JSON/PNG，请改用 Data URL、HTTPS 或 PNG 内嵌资源。"
                )
            else:
                raise invalid(
                    "头像 URI 暂不支持；请使用 ccdefault:、Data URL、HTTPS 或 PNG 的 __asset: 引用。"
                )
            media_type, ext = await asyncio.to_thread(image_info, raw)
            cached_id = await asyncio.to_thread(self.storage.put_asset, raw, ext)
            self.storage.cache_asset(key, cached_id, media_type)
            cached = self.storage.cached_asset(key)
            if cached["asset_id"] != cached_id:
                raw = await self.original(cached["asset_id"])
                media_type, _ = await asyncio.to_thread(image_info, raw)
            return CardImage(raw, media_type)

    @staticmethod
    def embedded(original: bytes, path: str) -> bytes:
        try:
            key = b"chara-ext-asset_:" + path.encode("utf-8")
            matches = [
                _png_text(kind, payload, decode_asset=True)[1]
                for kind, payload in png_chunks(original)
                if kind in (b"tEXt", b"zTXt", b"iTXt")
                and b"\0" in payload
                and payload.split(b"\0", 1)[0] == key
            ]
            if len(matches) != 1:
                raise invalid("PNG 内嵌头像缺失或存在重复资源名称。")
            return base64.b64decode(matches[0], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise invalid("PNG 内嵌头像的 Base64 数据无效。") from exc
