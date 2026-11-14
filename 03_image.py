#!/usr/bin/env python3
"""Generate and download one Seedream image. Run: python 03_image.py --help"""
import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
TASK = "image"
KEY_FIELD = "ARK_API_KEY"
MODEL_FIELD = "SEEDREAM_MODEL_ID"
DEFAULT_MODEL = "doubao-seedream-5-0-260128"
API_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
SECRETS = []

# 1. Read or edit the task here. Credentials stay in .env.
# 提示词不再写死在文件里，改成运行时由你输入——见下面的 ask_prompt()。
SIZE = "2K"  # 图片尺寸档位


def ask_prompt():
    """在控制台里问一句提示词，返回去掉首尾空白的结果。

    这是给「双击运行」准备的：双击这个 .py 文件后，窗口里就会弹出这个问题，
    把你想生成的画面描述一遍、回车，它就去生成对应的图片。
    """
    try:
        text = input("请输入提示词（描述你想要的画面）: ").strip()
    except EOFError:
        # 在没有控制台、或输入被掐断的地方调用时读不到内容，给一句看得懂的提示
        raise ValueError("没有读到输入。请双击这个文件，在弹出的窗口里输入提示词。") from None
    if not text:
        raise ValueError("提示词不能为空，请重新运行并输入内容。")
    return text


# 2. Send the image request, or resume ONLY a saved image download.
def main(argv=None):
    """命令行入口，负责调度「生成一张新图」和「只重试下载」两条路径。

    这个脚本没有位置参数，全靠选项区分动作：
      什么选项都不加   → 真的调 API 生成一张新图，然后下载到本地（花钱）
      --dry-run       → 只把请求计划写进磁盘，不联网、不花钱
      --check         → 只检查本地密钥和模型名，不联网
      --resume <目录> → 复用某次已保存的响应，只重试下载，不重新生成（不花钱）

    返回 0 表示成功，1 表示出错。
    """
    # 定义三个命令行选项
    parser = argparse.ArgumentParser(description=__doc__)  # __doc__ 就是文件开头那句说明，用作 --help 的描述
    parser.add_argument("--dry-run", action="store_true", help="Save the request; no API call.")  # 出现即为 True
    parser.add_argument("--check", action="store_true", help="Check local key; no API call.")
    parser.add_argument("--resume", type=Path, help="Retry a download from a saved run folder; no new generation.")  # 要带一个目录，自动转成 Path
    args = parser.parse_args(argv)  # 解析命令行；argv=None 表示去读真实的命令行参数

    # 这两个变量先置空。放在 try 外面，是为了让 except 里也能读到它们
    # （如果在 try 里才第一次赋值，一旦前面就出错，except 里就会 NameError）
    folder, record = None, None
    try:
        # 读取密钥和模型名。要不要强制校验密钥：--check 要（它本来就是来检查密钥填没填的），
        # --dry-run / --resume 不要（这两个动作压根不联网，密钥填没填无所谓）
        key, model = settings(require_key=args.check or not (args.dry_run or args.resume))

        # 分支 1：--check 只做本地体检，打印一行就结束
        if args.check:
            print("LOCAL CHECK: key is set (hidden); model:", model, "— live access is not tested.")
            return 0  # 成功

        # 分支 2：--resume 复用已有结果，只重试下载
        if args.resume:
            # --resume 和 --dry-run 是两个互相矛盾的动作，同时给出就是写错了
            if args.dry_run:
                raise ValueError("--resume and --dry-run are separate actions.")
            candidate = args.resume.resolve()  # 把用户给的目录转成绝对路径
            saved = json.loads((candidate / "run.json").read_text())  # 读那次运行的记录文件
            # 校验这个目录是不是「真实的图片生成记录」：必须属于 image 任务，且不是 dry-run 的空跑
            if saved.get("task") != TASK or saved.get("dry_run"):
                raise ValueError("Choose a saved live image run.")
            folder, record = candidate, saved  # 校验通过，接过来接着用
            # 如果之前已经下载成功、而且文件还在，就直接返回，不做任何多余动作
            if record.get("status") == "succeeded" and (folder / record["output_file"]).exists():
                print("Already saved:", folder / record["output_file"])
                return 0
            # 关键一步：从磁盘读回上次保存的响应，复用里面的图片地址。
            # 这样就不需要再调一次 API，也就不再花一次钱
            response = json.loads((folder / "response.json").read_text())

        # 分支 3：默认动作——真的生成一张新图
        else:
            # 先问你要一句提示词。双击运行时，你就是在弹出的窗口里输入这一句
            prompt = ask_prompt()
            # 组装请求体：模型名 + 提示词 + 尺寸 + 三个固定开关
            #   sequential_image_generation=disabled → 只要一张，不要一次出一组图
            #   response_format=url                   → 先返回一个临时链接，由我们自己下载
            #   stream=False                          → 不用流式返回
            body = {"model": model, "prompt": prompt, "size": SIZE,
                    "sequential_image_generation": "disabled", "response_format": "url", "stream": False}
            folder = new_folder("single")  # 新建带时间戳的结果目录
            # 先落一份运行记录，状态 submitting（正在提交）。
            # 这样即使程序中途崩了，磁盘上也留了痕迹，能看出"曾经尝试过生成"
            record = {"task": TASK, "model": model, "dry_run": args.dry_run,
                      "status": "planned" if args.dry_run else "submitting"}
            save_json(folder / "request.json", {"method": "POST", "url": API_URL, "body": body})  # 存档请求体
            (folder / "prompt.txt").write_text(clean(prompt), encoding="utf-8")  # 存档提示词（写前脱敏）
            save_json(folder / "run.json", record)  # 存档运行记录
            # 如果是预演，到这里就结束——不联网，也就不花钱
            if args.dry_run:
                print("PLANNED — no API call. Saved:", folder)
                return 0
            # 3. Save the provider response before downloading its temporary URL.
            # 真正调用 API 生成图片。注意顺序：**先把响应存盘，再去下载图片**。
            # 因为响应里的图片地址是临时链接，万一下载失败，只要这份响应还在，
            # 就能用 --resume 把下载重试一遍，不必重新生成、不必再花一次钱
            response = api_call("POST", API_URL, key, body)
            save_json(folder / "response.json", response)

        # 走到这里说明 response 一定有值了（可能刚生成，也可能由 --resume 从磁盘读回）
        # 状态从 submitting 改成 generated：图已在服务端生成好，但还没落到本地磁盘
        record.update(status="generated", usage=response.get("usage"))  # usage 记录本次的 token 用量
        save_json(folder / "run.json", record)

        # 4. Download and keep the actual file, not just a temporary link.
        # 从响应里取出图片地址，把真正的文件下载到本地。
        # 只有这一步成功，状态才会变成 succeeded——因为临时链接会过期，落地的文件才是真正的成果
        output = download_media(response["data"][0]["url"], folder / "output.png")
        record.update(status="succeeded", output_file=output.name)  # 只记文件名，方便整个目录搬到别的机器
        save_json(folder / "run.json", record)
        print("SUCCEEDED. Image:", output)  # 图片的完整路径，就是你要看的那个文件
        print("Saved:", folder)  # 这次运行的目录
        return 0  # 退出码 0 = 成功

    # 兜底：任何异常都在这里统一处理
    except Exception as error:
        # folder 和 record 都有值，说明目录已建好、记录也写过了，那就把失败原因补记进去
        if folder and record:
            # generated 说明图已经生成、只是下载失败 → 记 download_failed（可以用 --resume 救回来）
            # 其他情况（还没拿到响应就出错）→ 记 failed_or_unknown，
            #   因为无法确定服务端到底生成了没有，不能盲目重试，否则可能重复扣费
            record.update(status="download_failed" if record.get("status") == "generated" else "failed_or_unknown",
                          error=clean(str(error)))  # 错误信息脱敏后再存
            save_json(folder / "run.json", record)
            print("Saved:", folder)  # 告诉用户去哪看现场
        # 打印错误。后面那句提醒很重要：先去看已保存的响应，确认这次到底生成了没有，
        # 再决定要不要重新生成——不要上来就重跑
        print("Error:", clean(str(error)), "Inspect the saved response before any new generation.")
        return 1  # 退出码 1 = 失败


def download_media(url, path):
    if not url.startswith("https://"):
        raise ValueError("Expected an HTTPS media URL.")
    temporary = path.with_suffix(".part")
    maximum = (200 if TASK == "video" else 25) * 1024 * 1024
    try:
        # Media links use a separate download request WITHOUT the API key.
        with urllib.request.urlopen(url, timeout=90) as response, temporary.open("wb") as stream:
            declared = getattr(response, "headers", {}).get("Content-Length")
            size, prefix = 0, b""
            while block := response.read(64 * 1024):
                size += len(block)
                if size > maximum:
                    raise ValueError("Media exceeds the classroom download limit.")
                if not prefix:
                    prefix = block[:16]
                stream.write(block)
        if size < 16 or (declared is not None and size != int(declared)):
            raise ValueError("Media download is incomplete; keep the saved response and retry the download.")
        if TASK == "video" and prefix[4:8] == b"ftyp":
            suffix = ".mp4"
        elif TASK == "image" and prefix.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif TASK == "image" and prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif TASK == "image" and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            raise ValueError("Downloaded data is not a supported media file.")
        target = path.with_suffix(suffix)
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


# Small support functions are kept here so this file works on its own.
def settings(require_key=True):
    global SECRETS
    values = {}
    file = ROOT / ".env"
    if file.exists():
        for line in file.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip().strip('"').strip("'")
    for name in ("ARK_API_KEY", "MINIMAX_API_KEY", MODEL_FIELD):
        if name in os.environ:
            values[name] = os.environ[name]
    SECRETS = [values.get(name, "") for name in ("ARK_API_KEY", "MINIMAX_API_KEY") if values.get(name)]
    key = values.get(KEY_FIELD, "")
    if require_key and (not key or key.startswith("YOUR_") or key.startswith(KEY_FIELD + "=")
                        or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        raise ValueError(f"Fill {KEY_FIELD} in .env with just the complete key; no spaces or control characters.")
    return key, values.get(MODEL_FIELD) or DEFAULT_MODEL


def clean(text):
    for secret in SECRETS:
        text = text.replace(secret, "[redacted]")
    return text


def save_json(path, value):
    path.write_text(clean(json.dumps(value, ensure_ascii=False, indent=2)), encoding="utf-8")


def new_folder(label):
    folder = ROOT / "results" / TASK / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + label)
    folder.mkdir(parents=True)
    return folder


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("API redirect refused; check the configured endpoint.")


def api_call(method, url, key, body=None, timeout=90):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Response exceeds the lab's JSON limit.")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("error"):
            raise ValueError("API returned an unexpected response: " + str(value))
        return value
    except urllib.error.HTTPError as error:
        detail = error.read(4096).decode("utf-8", errors="replace")
        error.close()
        raise RuntimeError(clean(f"HTTP {error.code}: {detail}")) from None
    # No automatic retries: a failed POST might still have reached the provider.


if __name__ == "__main__":
    # 双击 .py 文件时，Windows 会用 py.exe 开一个控制台窗口来运行它。
    # 但脚本一结束，这个窗口会立刻关闭——结果路径和报错信息你都来不及看一眼。
    # 所以「没有带任何命令行参数」（基本就是双击这种情况）时，退出前停一下等你看完。
    exit_code = main()
    if len(sys.argv) == 1:
        input("\n按回车键关闭这个窗口...")
    raise SystemExit(exit_code)
