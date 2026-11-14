#!/usr/bin/env python3
"""角色抠图网页界面的后端。启动：python character_web.py serve

页面在 05_character.html，默认地址 http://127.0.0.1:8001 。

为什么单独一个端口：8000 已经被视频界面（04_video.py serve）占用了，
两个界面各自独立，互不影响。

和 04_video.py 一样，密钥只留在服务端，不下发给浏览器。
"""
import argparse
import base64
import json
import os
import re
import socket
import sys
import time
import urllib.request
import http.server
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "05_character.html"
CORE_FILE = ROOT / "05_character.py"
WEB_HOST = "127.0.0.1"
WEB_PORT = 8001          # 刻意避开视频界面的 8000，两个界面可以同时开着
SESSION_DIR = "web_sessions"
MAX_UPLOAD = 20 * 1024 * 1024


def load_core():
    """把 05_character.py 当模块加载。

    文件名以数字开头，没法用普通的 import 语句，所以用 importlib 按路径加载。
    """
    spec = importlib.util.spec_from_file_location("character_core", CORE_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CORE = load_core()


def parse_multipart(body, content_type):
    """极简 multipart/form-data 解析，够用即可。

    只处理一个文件字段：找出 boundary，按 boundary 切段，从每段里取出
    头部和内容，返回 {字段名: 字节}。不引入 cgi（Python 3.13 已把它删掉）。
    """
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not match:
        raise ValueError("上传格式不是 multipart/form-data。")
    boundary = ("--" + match.group(1)).encode()
    parts = {}
    for chunk in body.split(boundary):
        if not chunk.strip() or chunk.strip() == b"--":
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        if not _:
            continue
        # 末尾的 \r\n 是分隔符的一部分，不属于文件内容
        data = data[:-2] if data.endswith(b"\r\n") else data
        name = re.search(rb'name="([^"]+)"', head)
        if not name:
            continue
        filename = re.search(rb'filename="([^"]*)"', head)
        parts[name.group(1).decode()] = (data, filename.group(1).decode() if filename else None)
    return parts


def clamp(value, low, high):
    return max(low, min(high, value))


class Handler(http.server.BaseHTTPRequestHandler):
    """网页界面后端：把浏览器的请求翻译成 CORE.detect_characters / CORE.run_pipeline。"""

    key = ""
    vision_model = ""
    image_model = ""

    def log_message(self, template, *args):
        print("[web]", template % args)

    # ---- 路由 ----
    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                return self.page()
            if url.path == "/api/info":
                return self.api_info()
            if url.path == "/api/media":
                return self.api_media(parse_qs(url.query))
            return self.fail(404, "未知地址。")
        except Exception as error:
            return self.fail(500, CORE.clean(str(error)))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            content_type = self.headers.get("Content-Type") or ""
            if path == "/api/upload":
                return self.api_upload(raw, content_type)
            if path == "/api/analyze":
                return self.api_analyze(json.loads(raw or b"{}"))
            if path == "/api/preview":
                return self.api_preview(json.loads(raw or b"{}"))
            if path == "/api/run":
                return self.api_run(json.loads(raw or b"{}"))
            if path == "/api/compare":
                return self.api_compare(json.loads(raw or b"{}"))
            return self.fail(404, "未知地址。")
        except Exception as error:
            return self.fail(400, CORE.clean(str(error)))

    # ---- 回复工具 ----
    def reply(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def fail(self, code, message):
        self.reply(code, {"ok": False, "error": message})

    def page(self):
        if not PAGE.exists():
            return self.fail(404, f"找不到界面文件：{PAGE}")
        raw = PAGE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def api_info(self):
        """告诉前端当前用的模型和默认参数。"""
        self.reply(200, {"ok": True, "vision_model": self.vision_model,
                         "image_model": self.image_model, "port": WEB_PORT})

    # ---- 业务接口 ----
    def api_upload(self, raw, content_type):
        """接收图片，统一转成 PNG 存进 web_sessions/<id>/source.png。"""
        parts = parse_multipart(raw, content_type)
        if "image" not in parts:
            raise ValueError("没有收到图片字段。")
        data, filename = parts["image"]
        if not data:
            raise ValueError("上传的图片是空的。")
        if len(data) > MAX_UPLOAD:
            raise ValueError("图片不能超过 20 MiB。")

        # 用 PIL 统一转 PNG，顺便拿到宽高和原始格式（WEBP/JPEG 也能用）
        from PIL import Image
        import io
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception as error:
            raise ValueError(f"这不是能识别的图片：{error}") from None
        original_size = image.size
        original_format = image.format or "?"
        image = image.convert("RGB")

        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder = ROOT / SESSION_DIR / name
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "source.png"
        image.save(path)
        # 自动排掉截图底部自带的信息条。游戏截图常压一条浅色信息栏（标题/参数/色卡），
        # 留着会被模型当画面内容，还会让自动框选的 y 一路延伸到画面底部把它框进去。
        keep = CORE.detect_banner_rows(path)
        trimmed = 0
        if keep < image.size[1]:
            trimmed = image.size[1] - keep
            image = image.crop((0, 0, image.size[0], keep))
            image.save(path)
            print(f"[web] trimmed {trimmed}px UI banner from the bottom")
        CORE.save_json(folder / "upload.json", {
            "original_filename": filename, "original_format": original_format,
            "original_size": list(original_size), "stored_size": list(image.size),
            "banner_trimmed_px": trimmed, "bytes": len(data)})
        print(f"[web] uploaded {filename} {original_size} -> {path}")
        self.reply(200, {"ok": True, "id": name, "width": image.size[0], "height": image.size[1],
                         "original_format": original_format, "banner_trimmed_px": trimmed,
                         "preview": f"/api/media?session={quote(name)}&name=source.png"})

    def _session(self, session_id):
        """把会话 id 死死限制在 web_sessions 里面，防止读到本机其他文件。"""
        root = (ROOT / SESSION_DIR).resolve()
        folder = (root / str(session_id or "")).resolve()
        if folder != root and root not in folder.parents:
            raise ValueError("会话不合法。")
        if not (folder / "source.png").exists():
            raise ValueError("找不到这个会话的图片，请重新上传。")
        return folder

    def api_analyze(self, data):
        """自动识别角色：返回每个角色的框（0~1）。"""
        folder = self._session(data.get("session"))
        source = folder / "source.png"
        margin = float(data.get("margin", 1.0))  # 外扩强度，网页上可调
        found = CORE.detect_characters(self.key, self.vision_model, source)
        from PIL import Image
        with Image.open(source) as handle:
            width, height = handle.size
        for item in found:
            raw_box = item["box"]
            if margin and margin > 0:
                # 直接用 expand_box 的默认参数，不给它额外的比例倍率时用 1:1
                item["box"] = CORE.expand_box(
                    raw_box, scale=0.3 * margin, min_height=0.78 * margin,
                    min_width=min(0.9, 0.16 * margin))
            else:
                item["box"] = raw_box
            box = item["box"]
            item["pixels"] = [int(box["left"] * width), int(box["top"] * height),
                              int(box["right"] * width), int(box["bottom"] * height)]
        print(f"[web] analyze -> {len(found)} candidate(s)")
        self.reply(200, {"ok": True, "characters": found,
                         "image": {"width": width, "height": height}})

    def api_preview(self, data):
        """把框裁出来的效果回给前端，让人在花钱之前先看对不对。"""
        folder = self._session(data.get("session"))
        box = CORE.normalize_box(data.get("box"))
        if box is None:
            raise ValueError("框不合法。")
        from PIL import Image
        import io
        with Image.open(folder / "source.png") as handle:
            width, height = handle.size
            region = handle.convert("RGB").crop((
                int(box["left"] * width), int(box["top"] * height),
                int(box["right"] * width), int(box["bottom"] * height)))
        buffer = io.BytesIO()
        region.save(buffer, format="PNG")
        raw = buffer.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def api_compare(self, data):
        """对比两张图里的角色是否同一个。会调用一次视觉模型（两张图在同一请求里）。"""
        folder_a = self._session(data.get("session_a"))
        folder_b = self._session(data.get("session_b"))
        if folder_a == folder_b:
            raise ValueError("两张图是同一个上传，换一张再比。")
        result = CORE.compare_characters(self.key, self.vision_model,
                                         folder_a / "source.png", folder_b / "source.png")
        result["ok"] = True
        result["a"] = {"session": folder_a.name,
                       "preview": f"/api/media?session={quote(folder_a.name)}&name=source.png"}
        result["b"] = {"session": folder_b.name,
                       "preview": f"/api/media?session={quote(folder_b.name)}&name=source.png"}
        # 留个记录，便于事后回看（web_sessions/<b>/compare_vs_<a>.json）
        try:
            CORE.save_json(folder_b / f"compare_vs_{folder_a.name}.json", result)
        except Exception as error:
            print("[web] compare record not saved:", CORE.clean(str(error)))
        print(f"[web] compare {folder_a.name} vs {folder_b.name} -> "
              f"{result['verdict']} ({result['confidence']})")
        self.reply(200, result)

    def api_run(self, data):
        """对每个角色跑一次流水线，各自输出一张白底图。"""
        folder = self._session(data.get("session"))
        boxes = data.get("boxes") or []
        if not isinstance(boxes, list) or not boxes:
            raise ValueError("没有要处理的角色，请先识别或手动画框。")
        options = {
            "trim": bool(data.get("trim", True)),
            "padding": int(data.get("padding", 32)),
            "min_pixels": int(data.get("min_pixels", CORE.MIN_OUTPUT_PIXELS)),
            "background": "white",
            "size": "auto",
        }
        results = []
        for index, entry in enumerate(boxes, 1):
            box = CORE.normalize_box(entry.get("box"))
            if box is None:
                results.append({"index": index, "ok": False, "error": "框不合法，已跳过。"})
                continue
            label = str(entry.get("name") or f"character{index}")
            try:
                # 每个角色一个独立结果目录；输入图放在目录里并标记为"已准备"，
                # 这样流水线不会再裁一次（否则会二次裁切、比例出错）
                run_folder = CORE.new_folder("web")
                from PIL import Image
                with Image.open(folder / "source.png") as handle:
                    width, height = handle.size
                    handle.convert("RGB").crop((
                        int(box["left"] * width), int(box["top"] * height),
                        int(box["right"] * width), int(box["bottom"] * height))
                    ).save(run_folder / "input.png")
                size = CORE.resolve_size("auto", run_folder / "input.png", run_folder,
                                         options["min_pixels"])
                record = CORE.run_pipeline(
                    self.key, self.vision_model, self.image_model, run_folder / "input.png",
                    run_folder, dry_run=False, background=options["background"],
                    method="api", tolerance=28, remove_islands=True, size=size,
                    trim=options["trim"], padding=options["padding"],
                    prepared=True,
                    crop_info={"source": folder.name, "box": box, "label": label})
                ok = record.get("status") == "succeeded"
                relative = run_folder.relative_to(ROOT).as_posix()
                results.append({
                    "index": index, "ok": ok, "name": record.get("character", {}).get("name", label),
                    "label": label, "box": box, "status": record.get("status"),
                    "error": record.get("error"), "size": size,
                    "output": [f"/api/media?path={quote(relative)}&name={quote(record['output_file'])}",
                               f"/api/media?path={quote(relative)}&name=input.png"]
                    if record.get("output_file") else None,
                    "folder": run_folder.name,
                })
                print(f"[web] run {index}/{len(boxes)} {label} -> {record.get('status')}")
            except Exception as error:
                results.append({"index": index, "ok": False, "label": label,
                                "error": CORE.clean(str(error))})
                print(f"[web] run {index} failed: {CORE.clean(str(error))}")
        self.reply(200, {"ok": True, "results": results, "options": options})

    def api_media(self, params):
        """按 session/name 或 path/name 取图，防止目录穿越。"""
        params = {k: (v[0] if isinstance(v, list) else v) for k, v in params.items()}
        if params.get("session"):
            candidate = ROOT / SESSION_DIR / str(params["session"]) / str(params.get("name") or "")
        elif params.get("path"):
            candidate = ROOT / str(params["path"]) / str(params.get("name") or "")
        else:
            return self.fail(400, "缺少 session 或 path。")
        resolved = candidate.resolve()
        allowed = [(ROOT / SESSION_DIR).resolve(), (ROOT / "results").resolve()]
        if not any(resolved == base or base in resolved.parents for base in allowed):
            return self.fail(403, "路径不允许。")
        if not resolved.is_file():
            return self.fail(404, "找不到文件。")
        raw = resolved.read_bytes()
        suffix = resolved.suffix.lower()
        kind = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp"}.get(suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

def open_when_ready(address):
    """等服务真的起来了再打开浏览器，避免打开一个空白页。"""
    import threading
    import webbrowser

    def worker():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(address + "/api/info", timeout=2):
                    break
            except Exception:
                time.sleep(0.3)
        webbrowser.open(address)

    threading.Thread(target=worker, daemon=True).start()


def interface_ready(address, timeout=2):
    try:
        with urllib.request.urlopen(address + "/api/info", timeout=timeout) as response:
            return "vision_model" in json.loads(response.read())
    except Exception:
        return False


def port_in_use(host, port, timeout=2):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def serve(host=WEB_HOST, port=WEB_PORT, open_page=False):
    """启动网页界面。密钥留在服务端，前端只拿到处理结果。"""
    if not PAGE.exists():
        raise ValueError(f"找不到界面文件：{PAGE}")
    key, vision_model, image_model = CORE.settings()
    Handler.key, Handler.vision_model, Handler.image_model = key, vision_model, image_model
    if not 1 <= port <= 65535:
        raise ValueError(f"端口号 {port} 不合法。")
    address = f"http://{host}:{port}"
    if interface_ready(address):
        raise ValueError(f"角色界面已经在 {address} 上运行了，不用再启动一个。")
    if port_in_use(host, port):
        raise ValueError(f"端口 {port} 被别的程序占用了，请换一个，例如：--port {port + 1}")
    try:
        server = http.server.ThreadingHTTPServer((host, port), Handler)
    except OSError as error:
        raise ValueError(f"端口 {port} 无法绑定：{error}") from None
    print(f"角色抠图界面已启动：{address}")
    print(f"视觉模型：{vision_model}    生图模型：{image_model}")
    print("按 Ctrl+C 停止。先「识别角色」或手动拖框，确认裁切预览后再点「开始生成」。")
    if open_page:
        open_when_ready(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=["serve"], default="serve")
    parser.add_argument("--host", default=WEB_HOST)
    parser.add_argument("--port", type=int, default=WEB_PORT)
    parser.add_argument("--open", dest="open_page", action="store_true",
                        help="启动后自动打开浏览器。")
    parser.add_argument("--no-open", dest="open_page", action="store_false")
    parser.add_argument("--check", action="store_true", help="只检查本地配置，不联网。")
    parser.set_defaults(open_page=False)
    args = parser.parse_args(argv)
    try:
        if args.check:
            key, vision_model, image_model = CORE.settings()
            print("LOCAL CHECK: key is set (hidden); vision model:", vision_model,
                  "; image model:", image_model, "; page:", PAGE.name, "exists:", PAGE.exists())
            return 0
        return serve(args.host, args.port, args.open_page)
    except Exception as error:
        print("Error:", CORE.clean(str(error)))
        return 1


if __name__ == "__main__":
    exit_code = main()
    if len(sys.argv) == 1:
        input("\n按回车键关闭这个窗口...")
    raise SystemExit(exit_code)
