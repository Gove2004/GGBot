"""Agnes AI 生图 / 生视频 Tool：按文字（可带参考图）生成图片或短视频并发送。

配置/数据：tool/media/generate/config.json（与代码同分类目录）
运行产物：temp/tool/media/generate/*.{png,mp4}（随 temp TTL 清理）

参考图只收本地路径（temp/ 与 tool/media/emoji/ 素材），转成 base64 data URL 发出去：
群友发的图本来就已落地在 temp/，不必再让工具去外抓 URL，少一份 SSRF 面；接口的参考模式还要求素材公网可达，本地图直连不了。
生图是同步接口（一次 POST 拿结果），生视频是异步接口（提交任务 → 轮询状态 → 下载成品），
两者的等待都发生在 asyncio.to_thread 里，不阻塞其他会话。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..base import Tool
from ggbot.util.media import human_size
from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools.gen")

RATIOS = ("1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9")
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
# 参考图整份进请求体，过大的图先拒掉（QQ 群图常在 1MB 量级）
MAX_REF_BYTES = 8 * 1024 * 1024
# 视频接口只认 4–12 秒的整数秒；档位与画幅也是固定枚举
VIDEO_SECONDS_RANGE = (4, 12)
VIDEO_SIZES = ("720P", "1080P", "1K", "2K")
# 视频走首尾帧，比图片严：3 张以上属于参考模式，接口要求素材公网可达，这里不做
MAX_VIDEO_REFS = 2


def _json_of(req: urllib.request.Request, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:200]
        raise RuntimeError(f"HTTP {e.code}: {body}") from e


def _post(url: str, api_key: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    return _json_of(req, timeout)


def _post_with_retry(url: str, api_key: str, payload: dict, timeout: int, retries: int) -> dict:
    """队列满、限流都是暂时的（实测见过 503 video_queue_full），退避后重试；留下最后一次的错。"""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return _post(url, api_key, payload, timeout)
        except Exception as e:  # noqa: BLE001
            last_error = e
        if attempt < retries:
            time.sleep(min(5 * attempt, 15))
    raise last_error or RuntimeError("请求失败")


def _get(url: str, api_key: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    return _json_of(req, timeout)


def _download(url: str, timeout: int) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("接口返回的地址不是 http(s)")
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()


def _image_b64(result: dict) -> str:
    """接口正常给 data[0].b64_json；个别形态只给 URL，取回内容再转 base64。"""
    rows = result.get("data") or []
    item = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
    b64 = str(item.get("b64_json") or "")
    if b64:
        return b64
    for value in item.values():
        if isinstance(value, str) and value.startswith("http"):
            return base64.b64encode(_download(value, 30)).decode()
    raise RuntimeError("接口没返回图片")


def _looks_video(url: str) -> bool:
    return any(ext in url.lower() for ext in (".mp4", ".mov", ".video"))


def _find_video_url(obj) -> str:
    """在状态响应里找可下载的视频地址（不同返回形态的兼容写法）。"""
    if isinstance(obj, dict):
        for value in obj.values():
            if isinstance(value, str) and value.startswith(("http://", "https://")) \
                    and _looks_video(value):
                return value
            found = _find_video_url(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_video_url(value)
            if found:
                return found
    return ""


def _ref_data_url(rt, raw) -> tuple[str, str]:
    """参考图路径 → data URL；返回 (data_url, 错误说明)，二者只有一个非空。"""
    s = str(raw or "").strip().strip("\"'")
    if not s:
        return "", "refs 里有空路径"
    p = Path(s).expanduser()
    if not p.is_absolute():
        p = Path(rt.cfg.temp_dir) / p
    p = p.resolve()
    temp_root = Path(rt.cfg.temp_dir).resolve()
    emoji_root = (Path(rt.tool_root) / "media" / "emoji").resolve()
    under_temp = p.is_relative_to(temp_root) and p.relative_to(temp_root).parts[:1] != ("logs",)
    if not (under_temp or p.is_relative_to(emoji_root)):
        return "", f"参考图只允许 temp/ 与 tool/media/emoji/ 下的文件（{p.name} 不在其中）"
    if p.name == "config.json" or p.suffix.lower() in {".key", ".pem"}:
        return "", "参考图不能是配置/密钥文件"
    if not p.is_file():
        return "", f"参考图文件不存在: {p.name}"
    size = p.stat().st_size
    if size > MAX_REF_BYTES:
        return "", f"参考图过大（{human_size(size)}），请换 8MB 以内的图"
    mime = _MIME.get(p.suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}", ""


class _GenTool(Tool):
    """生图/生视频共用配置目录与参数校验。"""

    data_name = "media/generate"
    default_config = {
        "enabled": True,
        "api_key": "",
        "base_url": "https://api.agnes-ai.cn/v1",
        "image_model": "agnes-image-2.1-flash",
        "video_model": "agnes-video-2.5-flash",
        "image_size": "2K",
        "image_ratio": "1:1",
        "video_size": "720P",
        "video_ratio": "16:9",
        "video_seconds": 5,
        "video_max_seconds": 8,
        "image_timeout": 120,
        "video_timeout": 180,
        "request_timeout": 60,
        "poll_interval": 5,
        "max_retries": 2,
        "max_refs": 3,
        "max_prompt_chars": 800,
    }

    @property
    def _base(self) -> str:
        return str(self.config.get("base_url") or "https://api.agnes-ai.cn/v1").rstrip("/")

    def is_enabled(self) -> bool:
        if not super().is_enabled():
            return False
        return bool(self.config.get("api_key"))

    def _check(self, args: dict, ratio_key: str, refs_limit: int | None = None) -> tuple[dict, str]:
        """公共参数校验，返回 {prompt, refs, ratio}；第二个返回值是错误说明。"""
        cfg = self.config
        if not cfg.get("api_key"):
            return {}, "未配置 Agnes api_key"
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return {}, "prompt 为空"
        limit = max(20, int(cfg.get("max_prompt_chars") or 800))
        if len(prompt) > limit:
            return {}, f"描述过长（{len(prompt)} 字），请控制在 {limit} 字以内"
        ratio = str(args.get("ratio") or cfg.get(ratio_key) or "1:1").strip()
        if ratio not in RATIOS:
            return {}, f"ratio 只支持 {'/'.join(RATIOS)}"
        raw = args.get("refs") or []
        if isinstance(raw, str):
            raw = [raw]  # 只给一张参考图时模型常写成字符串
        if not isinstance(raw, list):
            return {}, "refs 需要本地图片路径的数组"
        max_refs = refs_limit if refs_limit is not None else max(1, int(cfg.get("max_refs") or 3))
        if len(raw) > max_refs:
            return {}, f"参考图最多 {max_refs} 张"
        refs = []
        for item in raw:
            data_url, err = _ref_data_url(self.rt, item)
            if err:
                return {}, err
            refs.append(data_url)
        return {"prompt": prompt, "ratio": ratio, "refs": refs}, ""


class GenerateImage(_GenTool):
    name = "generate_image"
    min_level = 2
    description = (
        "按文字描述生成一张图片并发送，对方想看某个画面、或想把群里已有的图改一版时用。"
        "想改图把它的本地路径放进 refs——上下文里 [图片 … local=…] 的 local 就是本地路径。"
        "描述写清主体、风格、光线；一次一张，生成要几十秒，别连着刷屏"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "画面描述：主体 + 风格/色调 + 光线，一段口语化的话",
            },
            "refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "参考图的本地路径（temp/ 或 tool/media/emoji/ 下），用于改图或保持风格；纯文生图不填",
            },
            "ratio": {
                "type": "string",
                "enum": list(RATIOS),
                "description": "画幅比例，横图 16:9、竖图 9:16、头像 1:1；不填用配置默认",
            },
        },
        "required": ["prompt"],
    }

    def _synth(self, prompt: str, refs: list[str], ratio: str, size: str) -> Path:
        cfg = self.config
        # 空数组会被接口拒（"image ... must not be empty"），纯文生图就不带 image 字段
        extra_body: dict = {"response_format": "b64_json"}
        if refs:
            extra_body["image"] = refs
        payload = {
            "model": cfg.get("image_model") or "agnes-image-2.1-flash",
            "prompt": prompt,
            "size": size,
            "ratio": ratio,
            "extra_body": extra_body,
        }
        result = _post_with_retry(
            f"{self._base}/images/generations", str(cfg.get("api_key") or ""), payload,
            max(10, int(cfg.get("image_timeout") or 120)),
            max(1, int(cfg.get("max_retries") or 2)),
        )
        out = self.rt.temp_dir(self.key) / f"gen_{int(time.time() * 1000)}.png"
        out.write_bytes(base64.b64decode(_image_b64(result)))
        if out.stat().st_size == 0:
            raise RuntimeError("接口返回的图片为空")
        return out

    async def run(self, args: dict, ctx: dict) -> dict:
        checked, err = self._check(args, "image_ratio")
        if err:
            return {"ok": False, "error": err}
        size = str(self.config.get("image_size") or "2K")
        extra = {"session": session_label(ctx.get("session_id"))}
        started = time.monotonic()
        try:
            path = await asyncio.to_thread(
                self._synth, checked["prompt"], checked["refs"], checked["ratio"], size
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("生图失败: %s", e, extra=extra)
            return {"ok": False, "error": f"生图失败: {e}"}
        logger.info("生图 %s/%s %s（%.1fs）", size, checked["ratio"],
                    human_size(path.stat().st_size), time.monotonic() - started, extra=extra)
        return await self.rt.send_media(ctx, 1, str(path), "图片")


class GenerateVideo(_GenTool):
    name = "generate_video"
    min_level = 3
    config_owner = False
    toggleable = False
    description = (
        "生成一段短视频并发送，只在对方明确想看动态画面时用（比生图慢得多、也更贵）。"
        "不填 refs 是文生视频；填一张是拿它当首帧让画面动起来；"
        "填两张是首帧到尾帧的过渡。seconds 控制时长（4–12 秒），一次一段，别连着刷屏"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "画面与动作描述：主体 + 要发生的运动/镜头变化，一段话",
            },
            "refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "本地图片路径，最多两张：一张=首帧（让这张图动起来），两张=首帧+尾帧（做过渡）；不填则纯文生视频",
            },
            "ratio": {
                "type": "string",
                "enum": list(RATIOS),
                "description": "画幅比例，横屏 16:9、竖屏 9:16；不填用配置默认",
            },
            "seconds": {
                "type": "number",
                "description": "时长（秒），就近取整到 4–12 秒；不填用配置默认",
            },
        },
        "required": ["prompt"],
    }

    def _size(self) -> str:
        cfg = self.config
        size = str(cfg.get("video_size") or "720P").strip().upper()
        if size not in VIDEO_SIZES:
            size = "720P"
        # flash 档只出 720P，配置里写更高的档位会被接口拒
        return "720P" if "flash" in str(cfg.get("video_model") or "") else size

    def _seconds(self, args) -> int:
        """秒数 → 4–12 之间的整数，配置上限也在这段里收。"""
        lo, hi = VIDEO_SECONDS_RANGE
        cfg = self.config
        ceiling = min(hi, max(lo, int(cfg.get("video_max_seconds") or hi)))
        default = min(ceiling, max(lo, int(round(float(cfg.get("video_seconds") or 5)))))
        try:
            value = int(round(float(args.get("seconds"))))
        except (TypeError, ValueError):
            return default
        return min(max(lo, value), ceiling)

    def _synth(self, prompt: str, refs: list[str], ratio: str, seconds: int, size: str) -> Path:
        cfg = self.config
        api_key = str(cfg.get("api_key") or "")
        base = self._base
        model = str(cfg.get("video_model") or "agnes-video-2.5-flash")
        timeout = max(10, int(cfg.get("request_timeout") or 60))
        budget = max(30, int(cfg.get("video_timeout") or 180))
        payload = {
            "model": model,
            "prompt": prompt,
            "mode": "text" if not refs else "keyframe",
            "seconds": str(seconds),
            "size": size,
            "aspect_ratio": ratio,
        }
        if refs:
            payload["first_frame"] = refs[0]
            if len(refs) > 1:
                payload["last_frame"] = refs[1]

        created = _post_with_retry(f"{base}/videos", api_key, payload, timeout,
                                   max(1, int(cfg.get("max_retries") or 2)))
        video_id = str(created.get("video_id") or "")
        task_id = str(created.get("task_id") or created.get("id") or "")
        if not video_id:
            raise RuntimeError("接口没返回视频 ID")
        poll = f"{base}/agnesapi?video_id={video_id}&model_name={model}"
        interval = max(1, int(cfg.get("poll_interval") or 5))
        deadline = time.monotonic() + budget
        status = created
        while str(status.get("status") or "") != "completed":
            if time.monotonic() > deadline:
                raise RuntimeError(f"视频生成超过 {budget} 秒未完成")
            time.sleep(interval)
            status = _get(poll, api_key, timeout)
            st = str(status.get("status") or "")
            if st == "failed":
                detail = json.dumps(status.get("error") or {}, ensure_ascii=False)[:200]
                raise RuntimeError(f"接口报失败: {detail}")

        url = str(status.get("url") or "") or _find_video_url(status)
        if not url and task_id:
            fallback = _get(f"{base}/videos/{task_id}", api_key, timeout)
            url = str(fallback.get("url") or "") or _find_video_url(fallback)
        if not url:
            raise RuntimeError("接口没返回视频地址")
        out = self.rt.temp_dir(self.key) / f"gen_{int(time.time() * 1000)}.mp4"
        out.write_bytes(_download(url, timeout))
        if out.stat().st_size == 0:
            raise RuntimeError("下载到的视频为空")
        return out

    async def run(self, args: dict, ctx: dict) -> dict:
        checked, err = self._check(args, "video_ratio", refs_limit=MAX_VIDEO_REFS)
        if err:
            return {"ok": False, "error": err}
        seconds = self._seconds(args)
        size = self._size()
        ratio = checked["ratio"]
        extra = {"session": session_label(ctx.get("session_id"))}
        started = time.monotonic()
        try:
            path = await asyncio.to_thread(
                self._synth, checked["prompt"], checked["refs"], ratio, seconds, size
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("生视频失败: %s", e, extra=extra)
            return {"ok": False, "error": f"生视频失败: {e}"}
        logger.info("生视频 %s/%s %ds %s（%.1fs 耗时）", size, ratio, seconds,
                    human_size(path.stat().st_size), time.monotonic() - started, extra=extra)
        return await self.rt.send_media(ctx, 2, str(path), "视频")


def get_tools(rt):
    return [GenerateImage(rt), GenerateVideo(rt)]
