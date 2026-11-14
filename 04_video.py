#!/usr/bin/env python3
"""Submit, query and download MiniMax H3 video in one file. Run: python 04_video.py --help

命令行用法不变；另外多了一个网页界面：python 04_video.py serve
"""
# argparse：解析命令行参数（submit / status / serve 三个动作 + 若干选项）
import argparse
# hashlib：对请求体算 SHA-256 指纹，用来识别"这是不是同一个请求"，防止重复提交、重复扣费
import hashlib
# math：用 math.isfinite 校验超时和轮询间隔是不是「有限的正数」
import math
# quote：把 task_id 做 URL 转义再拼进查询地址，防止里面的特殊字符破坏 URL
from urllib.parse import quote, parse_qs, urlparse
# json：请求体序列化、响应解析、存档写成 .json 文件
import json
# os：读取环境变量，用来覆盖 .env 里的配置
import os
# Path：跨平台地拼接、遍历、创建路径
from pathlib import Path
# datetime / timezone：取 UTC 时间，作为每次运行的结果目录名前缀
from datetime import datetime, timezone
# time：计时（monotonic）和轮询等待（sleep）
import time
# urllib.request / urllib.error：发 HTTP 请求、捕获 HTTP 错误
import urllib.request
import urllib.error
# http.server：Python 标准库自带的 Web 服务器，用来在本地托管网页界面
import http.server
# sys：读取命令行参数、判断这次是不是「双击启动」
import sys
# socket：直接探测端口上有没有别的程序在监听（Windows 上绑定同端口不报错，只能靠探测）
import socket
# ctypes：只在 Windows 上用来问系统「这个控制台里挂了几个进程」，据此识别双击
import ctypes

# ============ 第 0 步：全局配置，改行为只动这一段 ============
ROOT = Path(__file__).resolve().parent  # 本文件所在目录，用来定位 .env 和 results/
TASK = "video"  # 任务名，决定结果存到 results/video/ 下
KEY_FIELD = "MINIMAX_API_KEY"  # .env 里密钥的键名（这里存的是键名，不是密钥本身）
MODEL_FIELD = "MINIMAX_VIDEO_MODEL_ID"  # .env 里模型名的键名
DEFAULT_MODEL = "MiniMax-H3"  # .env 没写模型名时用它
API_URL = "https://api.minimax.cn/v2/video_generation"  # 提交任务的接口（POST）
SECRETS = []  # 运行期收集到的密钥，交给 clean() 做脱敏；由 settings() 填充

# 1. Read or edit the task here. Credentials stay in .env.
# 视频提示词：描述要生成的画面。写得很具体（固定 5 秒、镜头缓慢前推、道具静止、
# 不要剪辑/logo/字幕），是为了让模型输出稳定、可比对的结果——这也是这份作业要求的可复现性。
PROMPT = (
    'A single continuous five-second wide shot of a friendly illustrated university campus. A '
    'small blue guide robot stands beside a modern library and slowly raises one hand in a '
    'welcoming wave. Three orange traffic cones remain still in the foreground and a wooden '
    'bench remains on the right. The camera makes a gentle forward move. Warm morning light, '
    'consistent robot shape, stable objects, no scene cuts, no logos, no captions.'
)
QUERY_URL = "https://api.minimax.cn/v2/query/video_generation/"  # 查询任务状态的接口（GET + task_id）
DURATION = 5  # 视频时长（秒）
RESOLUTION = "768P"  # 分辨率档位
RATIO = "16:9"  # 画面宽高比（命令行默认值）

# ============ 网页界面（serve）用到的档位与设置 ============
RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")  # 官方允许的宽高比；文生视频必填，且不能填 adaptive
WEB_HOST = "127.0.0.1"  # 只监听本机：密钥留在服务端，局域网里的其他人访问不到
WEB_PORT = 8000  # 网页端口，浏览器访问 http://127.0.0.1:8000
PAGE = ROOT / "04_video.html"  # 界面文件，和本脚本放在同一目录
# MiniMax H3 天生就生成 32kHz 立体声，接口没有"静音"这个参数，
# 所以"不含音频"只能靠提示词明确要求——最终效果由模型决定，不保证 100% 无声。
SILENT_HINT = ("\n\nNo dialogue, no voice-over, no music, no sound effects: "
               "a completely silent video.")


def video_options(model):
    """按模型返回官方支持的档位，既给网页生成下拉框，也用来做提交前的本地校验。"""
    if model.strip().lower() == "minimax-h3-max":
        return {"resolutions": ["480P", "768P"], "duration_min": 5, "duration_max": 15}
    return {"resolutions": ["768P", "2K"], "duration_min": 4, "duration_max": 15}


def build_body(model, prompt, duration, resolution, ratio, audio=True):
    """把参数拼成请求体，并在本地先拦掉不合法的值——因为提交一次是真花钱的。

    prompt 为空、时长超范围、分辨率/宽高比不在允许列表里，都会直接抛 ValueError。
    audio=False 时会在提示词末尾追加一句"完全静音"的要求。
    """
    options = video_options(model)
    text = (prompt or "").strip()
    if not text:
        raise ValueError("提示词不能为空。")
    if not isinstance(duration, int) or isinstance(duration, bool) \
            or not options["duration_min"] <= duration <= options["duration_max"]:
        raise ValueError(f"{model} 的时长只能是 {options['duration_min']}~{options['duration_max']} 之间的整数秒。")
    if resolution not in options["resolutions"]:
        raise ValueError(f"{model} 的分辨率只能是 {' / '.join(options['resolutions'])}。")
    if ratio not in RATIOS:
        raise ValueError("宽高比只能是 " + " / ".join(RATIOS) + "。")
    if not audio:
        text += SILENT_HINT
    # 请求体结构和官方文档的文生视频示例完全一致
    return {"model": model, "content": [{"type": "text", "text": text}],
            "duration": duration, "resolution": resolution, "ratio": ratio}



# 2. Submit once and immediately save the task ID.
def submit(key, model, dry_run=False, new_task=False, prompt=None, duration=None,
           resolution=None, ratio=None, audio=True):
    """提交一次视频生成任务，并**立刻**把 task_id 存盘。

    视频生成是「提交 → 排队 → 生成 → 下载」的异步流程，一次调用几秒到几分钟不等。
    所以这里的核心不是拿结果，而是拿到 task_id 并保证它不丢——之后用 status 查询即可。

    dry_run=True：只把请求计划写进磁盘，不真的提交（用来预览，不花钱）。
    new_task=True：即使检测到有相同请求的未完成任务，也强行再提交一个。
    prompt / duration / resolution / ratio / audio：网页界面传进来的参数，
    不传（None）时就用本文件顶部的默认值，所以原来的命令行用法完全不变。

    返回本次运行的结果目录；即使中途出错，目录和记录也已经写盘了。
    """
    # 组装请求体：模型名 + 提示词 + 时长/分辨率/宽高比（build_body 会先做本地校验）
    body = build_body(model,
                      PROMPT if prompt is None else prompt,
                      DURATION if duration is None else duration,
                      RESOLUTION if resolution is None else resolution,
                      RATIO if ratio is None else ratio,
                      audio)
    # 对请求体算 SHA-256 指纹。sort_keys=True 保证同样的内容总是得到同样的指纹，
    # 这样"内容完全相同的请求"就能被识别出来。
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    # 防重复提交：翻遍历史运行记录，如果发现「同一个请求」且状态还悬着（不确定是否成功、
    # 或还在排队/生成中），就报错拦住，提示先去查询，而不是又提交一单白花钱。
    if not dry_run and not new_task:
        for file in (ROOT / "results/video").glob("*/run.json"):
            previous = json.loads(file.read_text())
            if previous.get("request_sha256") == fingerprint and previous.get("status") in (
                    "submitting", "submission_unknown", "submitted", "queued", "running", "download_failed"):
                raise ValueError(f"An existing or uncertain task is saved in {file.parent}. Query it first. "
                                 "Use --new-task only after checking that you intend another generation.")
    # 新建本次运行的结果目录
    folder = new_folder("task")
    # 先落盘一份运行记录，状态是 submitting（正在提交）。这样即使程序中途崩了，
    # 磁盘上也留有痕迹，下次能发现"可能已经提交过"。
    record = {"task": TASK, "model": model, "dry_run": dry_run, "request_sha256": fingerprint,
              "duration": body["duration"], "resolution": body["resolution"], "ratio": body["ratio"],
              "audio": bool(audio),
              "status": "planned" if dry_run else "submitting"}
    # 存档请求内容、提示词、运行记录（写文件前都经 clean() 脱敏）
    save_json(folder / "request.json", {"method": "POST", "url": API_URL, "body": body})
    (folder / "prompt.txt").write_text(clean(body["content"][0]["text"]), encoding="utf-8")
    save_json(folder / "run.json", record)
    if not dry_run:
        try:
            # 真正发提交请求
            response = api_call("POST", API_URL, key, body)
            # 兼容两种响应结构：顶层 task_id，或嵌套在 task.id 里
            task_id = response.get("task_id") or response.get("task", {}).get("id")
            if task_id:
                # 拿到 ID 立刻改状态为 submitted 并存盘——这是最关键的一步。
                # 必须在做任何其他事情之前保存，否则万一后面出错，ID 就丢了。
                record.update(task_id=str(task_id), status="submitted")
                save_json(folder / "run.json", record)  # Save before any other operation.
            # 存档完整原始响应，便于事后排查
            save_json(folder / "response.json", response)
            if not task_id:
                # 没拿到 ID 说明提交结果不明，不能盲目重试（可能已经创建了任务）
                raise ValueError("No task ID returned; check the provider before resubmitting.")
        except Exception as error:
            # 出错分两种情况：
            #   已拿到 task_id → 任务其实提交成功了，只是后续步骤失败，标记 submitted
            #   没拿到 task_id → 无法判断任务是否创建，标记 submission_unknown（结果未知）
            record.update(status="submitted" if record.get("task_id") else "submission_unknown", error=clean(str(error)))
            save_json(folder / "run.json", record)
            print("Saved:", folder)
            raise
    print(record["status"].upper(), "Saved:", folder, "Task ID:", record.get("task_id", "none (dry-run)"))
    return folder


# 3. Query the saved task. This function never submits a new video.
def status(key, folder=None, wait=False, timeout=300, interval=10):
    """查询已提交任务的进度，成功后把视频下载到本地。

    这个函数**永远不会提交新任务**——它只认磁盘上已存在的 task_id，避免重复扣费。

    folder：指定某次运行的结果目录；不传就自动取最近一次有 task_id 的运行。
    wait：True 表示在 timeout 秒内循环轮询；False 表示只查一次就返回。
    timeout/interval：轮询的总时限和每次间隔（秒）。
    返回 0 表示正常/成功，1 表示任务失败、被取消或下载失败。
    """
    # 先校验参数合法性：必须是有限的（不是 inf/NaN）正数，否则轮询逻辑会失控
    if not math.isfinite(timeout) or not math.isfinite(interval) or timeout <= 0 or interval <= 0:
        raise ValueError("Timeout and interval must be finite positive numbers.")
    if folder is None:
        # 没指定目录：在所有历史运行里找「带 task_id 的那些」，按名字倒序（时间戳前缀，
        # 所以倒序就是最新的在前），取最新的一个
        candidates = [p.parent for p in sorted((ROOT / "results/video").glob("*/run.json"), reverse=True)
                      if json.loads(p.read_text()).get("task_id")]
        if not candidates:
            raise ValueError("No saved task ID. A dry-run is not a submitted task.")
        folder = candidates[0]
    folder = folder.resolve()  # 转成绝对路径，后面拼子文件更稳妥
    record = json.loads((folder / "run.json").read_text())
    # 三道校验，防止拿错目录：必须是 video 任务、必须不是 dry-run、必须有 task_id
    if record.get("task") != TASK or record.get("dry_run") or not record.get("task_id"):
        raise ValueError("Select a live video run with a saved task ID.")
    # 如果之前已经成功下载过，直接返回，不重复请求也不重复下载
    if record["status"] == "succeeded" and (folder / "output.mp4").exists():
        print("Already downloaded:", folder / "output.mp4")
        return 0
    deadline = time.monotonic() + timeout  # 用单调时钟算截止时刻，不受系统时间调整影响
    try:
        while time.monotonic() < deadline:
            # 发查询请求。timeout 取「90 秒」和「距离截止还剩多久」中的较小值，
            # 保证单次请求不会超出总时限；max(0.001, ...) 防止出现 0 或负数。
            response = api_call("GET", QUERY_URL + quote(record["task_id"], safe=""), key,
                                timeout=min(90, max(0.001, deadline - time.monotonic())))
            # 每次查询的原始响应都单独存档，编号递增（poll_001、poll_002...），
            # 这样轮询全过程可回溯，便于写报告时举证
            number = len(list(folder.glob("poll_*.json"))) + 1
            save_json(folder / f"poll_{number:03d}.json", response)
            task = response["task"]
            state = task["status"]
            # 只认这五种状态；出现别的说明接口行为变了，立刻报错而不是瞎猜
            if state not in ("queued", "running", "succeeded", "failed", "cancelled"):
                raise ValueError("Unknown task status; inspect the saved response.")
            # 把状态写进运行记录。注意 succeeded 时先记成 download_failed——
            # 因为"生成成功"不等于"已经下载到本地"，要等下载真正完成才算 succeeded。
            record.update(status="download_failed" if state == "succeeded" else state,
                          provider_status=state, usage=task.get("usage"))
            record.pop("query_error", None)  # 本次查询成功，清掉上一次的查询错误
            save_json(folder / "run.json", record)
            print("Task:", record["task_id"], "Status:", state)
            if state == "succeeded":
                # 4. Download once; preserve the ID if a download needs retrying.
                # 下载视频文件。即使下载失败，task_id 也保留着，可以稍后单独重试下载。
                output = download_media(task["content"]["url"], folder / "output.mp4")
                record.update(status="succeeded", output_file=output.name)
                break
            if state in ("failed", "cancelled"):
                # 任务失败或被取消：记下原因并结束（但 task_id 仍保留，便于向服务方追溯）
                record["error"] = task.get("error", state)
                break
            if not wait or time.monotonic() >= deadline:
                # 不是持续等待模式，或已经到截止时间：先返回，稍后再查同一个任务
                print("Still pending. Run status again later for this same task.")
                break
            # 还没出结果，睡一会儿再查。同样取「间隔」和「剩余时间」的较小值，
            # 避免睡过头超出总时限
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
    except Exception as error:
        # 查询过程中出错（网络、接口异常等）：记录错误再抛出，保留现场
        record["query_error"] = clean(str(error))
        save_json(folder / "run.json", record)
        raise
    save_json(folder / "run.json", record)
    print("Saved:", folder)
    # 失败类状态返回 1，正常/成功返回 0
    return 1 if record["status"] in ("failed", "cancelled", "download_failed") else 0


def launch_argv(argv=None):
    """去掉「双击」时 Windows 塞进来的脚本路径，返回真正的参数列表。

    双击 .py 文件时，系统实际执行的是 python "D:\\...\\04_video.py"，
    解释器会把脚本自己的路径放进 argv[0]。命令行敲 `serve` 时也带这个路径，
    所以两种情况下都要先把它剥掉，剩下的才是用户真正写的参数。
    （Python 3.14 起解释器已经不再这样塞路径，所以这里只是「剥掉才算对」，剥不掉也不影响。）
    """
    if argv is None:
        argv = list(sys.argv[1:])
    else:
        argv = list(argv)
    # 判断第一项是不是本文件：python 2 风格会带引号，py.exe 启动器可能只给文件名
    if argv and argv[0].strip('"').replace("\\", "/").split("/")[-1].lower() == Path(__file__).name.lower():
        argv = argv[1:]
    return argv


def started_by_explorer():
    """判断父进程是不是 explorer.exe——这是「在资源管理器里双击」最直接的证据。

    需要它是因为「控制台里只有一个进程」这个条件在个别情况下不够用：
    系统有时会复用已有的控制台，这时双击也会看起来像命令行运行。
    两个条件任一成立就认为是双击，宁可多开一次界面，也不要双击之后什么都不发生。
    """
    if os.name != "nt":
        return False
    try:
        # 工具库放在函数里导入：只有 Windows 上真正需要判断时才加载
        import ctypes.wintypes

        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", ctypes.wintypes.DWORD),
                        ("cntUsage", ctypes.wintypes.DWORD),
                        ("th32ProcessID", ctypes.wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", ctypes.wintypes.DWORD),
                        ("cntThreads", ctypes.wintypes.DWORD),
                        ("th32ParentProcessID", ctypes.wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", ctypes.wintypes.DWORD),
                        ("szExeFile", ctypes.c_char * 260)]
        kernel32 = ctypes.windll.kernel32
        # 先给当前进程建一个快照，再从快照里读自己的信息，才能拿到父进程 ID
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
        if snapshot == -1:
            return False
        try:
            entry = Entry()
            entry.dwSize = ctypes.sizeof(Entry)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return False
            me, parent = os.getpid(), None
            while True:
                if entry.th32ProcessID == me:
                    parent = entry.th32ParentProcessID
                    break
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
            if not parent:
                return False
            # 用同一个快照再走一遍，查父进程的名字（快照必须在用完后再关闭）
            entry = Entry()
            entry.dwSize = ctypes.sizeof(Entry)
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return False
            while True:
                if entry.th32ProcessID == parent:
                    return entry.szExeFile.decode("mbcs", "replace").lower() == "explorer.exe"
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    return False
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return False  # 探测失败就按命令行处理，绝不因此报错


def double_clicked():
    """判断这次是不是「双击运行」（而不是在终端里敲命令）。

    两个条件满足其一就算双击：
      1. 父进程是 explorer.exe —— 在资源管理器里双击的直接证据；
      2. 当前控制台里只挂着 python 自己 —— 双击 .py 时系统会为它新建一个控制台，
         而终端里敲命令时，控制台里至少还有 cmd/powershell。
    这样不用额外参数就能区分两种用法：双击时自动打开网页界面，命令行行为完全不变。

    注意别用「先传 NULL 问长度」的写法：这个 API 在缓冲区为空时返回的 0 并不是
    "需要 0 个"，实测同一台机器上会随机给出 0 或 9 之类的值，据此判断会时对时错。
    所以这里一次性给足缓冲区，直接看真实结果。
    """
    if os.name != "nt":
        return False
    if started_by_explorer():
        return True
    try:
        buffer = (ctypes.c_uint32 * 16)()  # 16 个位置足够放一个控制台里的所有进程
        count = ctypes.windll.kernel32.GetConsoleProcessList(buffer, 16)
        # 1 = 控制台里只有我们自己（双击启动）；0 = 没有控制台；>1 = 还挂着 shell，是命令行运行
        return count == 1
    except Exception:
        return False  # 探测失败就按命令行处理，绝不因此报错


def interface_ready(address, timeout=3):
    """探测这个地址上是不是已经在跑我们的网页界面（是的话返回 True）。

    只看端口有没有人监听是不够的：那个端口可能被别的程序占着。
    所以这里真的请求一次 /api/info，返回的 JSON 里有 model 字段才算数。
    """
    try:
        with urllib.request.urlopen(address + "/api/info", timeout=timeout) as response:
            return "model" in json.loads(response.read())
    except Exception:
        return False


def port_in_use(host, port, timeout=2):
    """探测端口上是不是已经有别的程序在监听（不关心是谁）。

    和 interface_ready 配合使用：先确认不是我们的界面，再用这个判断端口被别人占了。
    有必要单独查一次，是因为 Windows 上绑定同一个端口并不会报错（SO_REUSEADDR），
    光靠「绑定失败」发现不了冲突。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)  # 连不上就说明没人监听，让 connect_ex 直接返回错误码
        return probe.connect_ex((host, port)) == 0


# 入口：解析命令行参数，分派到「提交」或「查询」两条路径
def main(argv=None):
    """命令行入口。返回 0 表示成功，1 表示出错。"""
    # 先处理「双击」：双击时 Windows 不会给任何参数，默认动作就从 status 换成 serve，
    # 这样双击 04_video.py 也能直接弹出网页界面，而不是安静地查一次任务状态。
    argv = launch_argv(argv)
    by_double_click = double_clicked() and not argv
    if by_double_click:
        argv = ["serve"]
    parser = argparse.ArgumentParser(description=__doc__)
    # 位置参数：第一个词是动作，只能是 submit / status / serve；不写则默认 status（更安全，
    # 因为 status 只查询、不会花钱新建任务）
    parser.add_argument("action", nargs="?", choices=["submit", "status", "serve"], default="status")
    parser.add_argument("--dry-run", action="store_true", help="Preview submit only; no task is created.")
    parser.add_argument("--check", action="store_true", help="Check the local MiniMax key; no API call.")
    parser.add_argument("--run", type=Path, help="Saved video run folder for status; defaults to latest saved ID.")
    parser.add_argument("--wait", action="store_true", help="Keep querying for a bounded period.")
    parser.add_argument("--timeout", type=float, default=300)  # 轮询总时限（秒）
    parser.add_argument("--interval", type=float, default=10)  # 每次轮询间隔（秒）
    parser.add_argument("--new-task", action="store_true", help="Explicitly submit another video despite a pending/uncertain match.")
    parser.add_argument("--host", default=WEB_HOST)  # serve：监听地址，默认只监听本机
    parser.add_argument("--port", type=int, default=WEB_PORT)  # serve：监听端口
    # serve 启动后要不要自动打开浏览器。默认 None 表示"看情况"：
    # 双击启动 → 自动打开；命令行手动起服务 → 不弹窗打扰。
    parser.add_argument("--open", dest="open_page", action="store_true",
                        help="serve: open the browser once the page is ready.")
    parser.add_argument("--no-open", dest="open_page", action="store_false",
                        help="serve: do not open the browser (default).")
    parser.set_defaults(open_page=None)  # None = 由「是不是双击」决定
    args = parser.parse_args(argv)
    # 双击启动时自动开浏览器，命令行起服务时默认不开；显式给了 --open/--no-open 就听显式的
    open_page = by_double_click if args.open_page is None else args.open_page
    try:
        # 读配置拿密钥和模型名。--check 需要密钥（要检查它有没有填），dry-run 不需要
        key, model = settings(require_key=args.check or not args.dry_run)
        if args.check:
            # 只检查本地配置，联网可用性不在这里验证
            print("LOCAL CHECK: MiniMax key is set (hidden); model:", model, "— live access is not tested.")
            return 0
        if args.action == "serve":
            # serve 用不到提交/查询专属的选项，给了就是写错了
            if args.run or args.wait or args.dry_run or args.new_task:
                raise ValueError("--run/--wait/--dry-run/--new-task do not belong to serve.")
            address = f"http://{args.host}:{args.port}"
            # 如果界面上一次开着还没关，端口会一直被那个窗口占着。此时再起一个只会端口冲突，
            # 所以先探一下：确认是这个界面就直接打开浏览器，省得双击的人看到一堆报错。
            if open_page and interface_ready(address):
                import webbrowser
                print("网页界面已经在运行，直接打开：", address)
                webbrowser.open(address)
                return 0
            return serve(key, model, args.host, args.port, open_page)
        if args.action == "submit":
            # --run / --wait 只在查询时有意义，提交时给了就是写错了，直接报错
            if args.run or args.wait:
                raise ValueError("--run and --wait belong to status.")
            submit(key, model, args.dry_run, args.new_task)
            return 0
        # 走到这里说明 action 是 status；--dry-run / --new-task 只对提交有意义
        if args.dry_run or args.new_task:
            raise ValueError("--dry-run and --new-task belong to submit.")
        return status(key, args.run, args.wait, args.timeout, args.interval)
    except Exception as error:
        # 统一兜底：打印脱敏后的错误信息，返回失败码 1
        print("Error:", clean(str(error)))
        return 1


# 5. 网页界面：浏览器 → 本机 Python → MiniMax
# 为什么不让 html 自己去调 MiniMax 接口？两个原因：
#   1. 密钥只能放在服务端。网页源码在浏览器里按 F12 就能全看到，等于把密钥公开；
#   2. 浏览器有跨域（CORS）限制，直接从网页请求 MiniMax 会被拦下来。
# 所以这里用 Python 标准库的 http.server 起一个小后端，网页只管发请求和显示结果。
def latest_folder():
    """返回 results/video 下最新建好的运行目录（目录名以 UTC 时间戳开头，按名字倒序即最新）。"""
    folders = sorted((ROOT / "results" / TASK).glob("*-*"), reverse=True)
    return folders[0] if folders else None


class WebHandler(http.server.BaseHTTPRequestHandler):
    """网页界面的后端：把浏览器的请求翻译成 submit() / status() 的调用。

    密钥只存在下面的类属性里，永远不会下发给浏览器——
    否则任何人"查看网页源代码"就能拿到你的密钥。
    """

    key = ""  # 由 serve() 在启动前填好
    model = ""

    def log_message(self, template, *args):
        """把默认日志改短一点，方便在终端里看。"""
        print("[web]", template % args)

    # ---- 5.1 路由：GET 取页面+查状态，POST 提交任务 ----
    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                return self.page()
            if url.path == "/api/info":
                return self.api_info()
            if url.path == "/api/query":
                return self.api_query(parse_qs(url.query))
            if url.path == "/api/media":
                return self.api_media(parse_qs(url.query))
            return self.fail(404, "未知地址。")
        except Exception as error:
            return self.fail(500, clean(str(error)))

    def do_POST(self):
        if urlparse(self.path).path != "/api/submit":
            return self.fail(404, "未知地址。")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception as error:
            return self.fail(400, "请求内容不是合法的 JSON：" + clean(str(error)))
        return self.api_submit(data)

    # ---- 5.2 回复工具 ----
    def reply(self, code, payload):
        """回一个 JSON；no-store 让浏览器每次都拿最新状态，不要缓存。"""
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
        """把 04_video.html 原样发给浏览器。每次都重新读盘，改完界面刷新即可生效。"""
        if not PAGE.exists():
            return self.fail(404, f"找不到界面文件：{PAGE}")
        raw = PAGE.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def safe_folder(self, name):
        """把浏览器给的目录名死死限制在 results/video 里面，防止它读到本机其他文件。"""
        root = (ROOT / "results" / TASK).resolve()
        folder = (root / name).resolve()
        if folder != root and root not in folder.parents:
            raise ValueError("目录不合法。")
        if not (folder / "run.json").exists():
            raise ValueError("找不到这次运行的记录。")
        return folder

    def state(self, folder):
        """从 run.json 里挑出前端需要的那几项，拼成一个好用的字典。"""
        record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        files = sorted(folder.glob("output.*"))
        return {"folder": folder.name, "status": record.get("status"),
                "task_id": record.get("task_id"), "dry_run": bool(record.get("dry_run")),
                "provider_status": record.get("provider_status"),
                "error": record.get("error") or record.get("query_error"),
                "duration": record.get("duration"), "resolution": record.get("resolution"),
                "ratio": record.get("ratio"), "audio": record.get("audio"),
                "video": f"/api/media?folder={quote(folder.name)}" if files else None}

    # ---- 5.3 三个接口 ----
    def api_info(self):
        """告诉前端：现在用哪个模型、它支持哪些档位、默认值是什么。"""
        options = video_options(self.model)
        self.reply(200, {"ok": True, "model": self.model,
                         "resolutions": options["resolutions"],
                         "duration_min": options["duration_min"], "duration_max": options["duration_max"],
                         "ratios": list(RATIOS),
                         "default_duration": DURATION, "default_resolution": RESOLUTION,
                         "default_ratio": RATIO})

    def api_submit(self, data):
        """提交一次任务。dry_run=true 时只写计划、不联网，用来免费预览。"""
        before = latest_folder()
        try:
            folder = submit(self.key, self.model,
                            dry_run=bool(data.get("dry_run")),
                            prompt=str(data.get("prompt", "")),
                            duration=int(data.get("duration", DURATION)),
                            resolution=str(data.get("resolution", RESOLUTION)),
                            ratio=str(data.get("ratio", RATIO)),
                            audio=bool(data.get("audio", True)))
        except Exception as error:
            after = latest_folder()
            # 只有这次真的建了新目录（失败发生在提交环节）才带上记录返回；
            # 参数不合法是在建目录之前就拦下的，这时最新的目录是上一次运行的，返回它会误导前端
            state = self.state(after) if after and after != before else None
            return self.reply(200, {"ok": False, "error": clean(str(error)), "state": state})
        body = json.loads((folder / "request.json").read_text(encoding="utf-8"))["body"]
        state = self.state(folder)
        state["prompt"] = body["content"][0]["text"]  # 实际发出去的提示词（可能被追加了静音要求）
        return self.reply(200, {"ok": True, "state": state})

    def api_query(self, params):
        """查询一次任务状态（只查，绝不会新建任务）。folder 不填就用最近一次有 task_id 的运行。"""
        name = (params.get("folder") or [""])[0]
        if name:
            folder = self.safe_folder(name)
        else:
            folder = None
            for file in sorted((ROOT / "results" / TASK).glob("*/run.json"), reverse=True):
                if json.loads(file.read_text(encoding="utf-8")).get("task_id"):
                    folder = file.parent
                    break
            if folder is None:
                return self.reply(200, {"ok": True, "state": None, "note": "还没有提交过真实任务。"})
        try:
            status(self.key, folder, wait=False)
        except Exception:
            # 具体原因 status() 已经写进 run.json 的 query_error 字段了，这里不重复报错
            pass
        return self.reply(200, {"ok": True, "state": self.state(folder)})

    def api_media(self, params):
        """把下载到本地的 output.mp4 发给浏览器播放；支持 Range，视频才能拖动进度条。"""
        folder = self.safe_folder((params.get("folder") or [""])[0])
        files = sorted(folder.glob("output.*"))
        if not files:
            return self.fail(404, "这次运行还没有下载好的视频文件。")
        path = files[0]
        size = path.stat().st_size
        start, end = 0, max(0, size - 1)
        head = self.headers.get("Range") or ""
        if head.startswith("bytes="):
            # 浏览器要某一段（拖动进度条时）：回 206 表示"只给这一段"
            first, _, last = head[6:].partition("-")
            start = max(0, min(int(first) if first else 0, size - 1))
            end = max(start, min(int(last) if last else size - 1, size - 1))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            left = end - start + 1
            while left > 0:
                block = stream.read(min(64 * 1024, left))
                if not block:
                    break  # 传输途中文件被改动之类的情况，直接结束这一轮
                self.wfile.write(block)
                left -= len(block)


def serve(key, model, host=WEB_HOST, port=WEB_PORT, open_page=False):
    """启动本地网页界面。浏览器打开 http://127.0.0.1:8000 就能填提示词生成视频。

    默认只监听 127.0.0.1（本机），所以开了服务也不会把密钥暴露给局域网里的其他人。
    open_page=True 时会自己把浏览器叫起来（配合「启动视频生成.bat」双击运行）：
    另起一个后台线程先探活，等端口真的通了再打开，不会出现"打开太快、页面还没起来"的空白。
    """
    if not PAGE.exists():
        raise ValueError(f"找不到界面文件，请确认它和本脚本放在同一目录：{PAGE}")
    if not 1 <= port <= 65535:
        raise ValueError(f"端口号 {port} 不合法，应该在 1~65535 之间。")
    WebHandler.key, WebHandler.model = key, model
    address = f"http://{host}:{port}"
    # 先探一下端口上有没有已经在跑的界面。不能指望「绑定失败」来发现问题：
    # Windows 的 SO_REUSEADDR 允许第二个进程照样绑上同一个端口且不报错，
    # 结果就是两个服务抢同一个端口、浏览器时好时坏，很难排查，所以这里主动拦一道。
    if interface_ready(address, timeout=2):
        raise ValueError(f"网页界面已经在 {address} 上运行了，不用再启动一个；"
                         "需要停止的话，关掉那个正在运行的窗口即可。")
    if port_in_use(host, port):
        raise ValueError(f"端口 {port} 被别的程序占用了，请换一个，例如：--port {port + 1}")
    try:
        # 构造服务器时就把端口绑好，绑定失败会在这里抛 OSError（非 Windows 平台常见）
        server = http.server.ThreadingHTTPServer((host, port), WebHandler)
    except OSError as error:
        raise ValueError(f"端口 {port} 已被占用（可能有另一个启动窗口还在运行）：{error}") from None
    print(f"网页界面已启动：{address}")
    print("按 Ctrl+C 停止。点「生成视频」才会真的花钱，「只预览」不花钱。")
    if open_page:
        # 这两个模块只有双击启动时才用得到，放在这里避免拖慢其他命令的启动
        import threading
        import webbrowser

        def open_when_ready():
            """在后台轮询等待页面可访问，最多等约 15 秒，然后打开浏览器。"""
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    with urllib.request.urlopen(address + "/api/info", timeout=2):
                        pass  # 能通就说明服务真的起来了
                    break
                except Exception:
                    time.sleep(0.3)
            webbrowser.open(address)

        threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        server.serve_forever()  # 一直提供服务，直到按 Ctrl+C
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


def download_media(url, path):
    """把生成好的媒体文件下载到 path，返回实际写成的文件路径。

    做三件防护：限制下载体积上限、先写临时文件再改名（避免留下半截文件）、
    用文件头字节校验真实格式（不信任 URL 的后缀）。
    """
    # 只允许 HTTPS，防止误把密钥或内容发到不安全的地址
    if not url.startswith("https://"):
        raise ValueError("Expected an HTTPS media URL.")
    # 先写到 .part 临时文件；只有全部校验通过才改名成正式文件，
    # 这样中途失败不会留下一个看起来"下载好了"的残缺文件
    temporary = path.with_suffix(".part")
    # 体积上限：视频 200MB，图片 25MB。防止异常响应把磁盘写满
    maximum = (200 if TASK == "video" else 25) * 1024 * 1024
    try:
        # Media links use a separate download request WITHOUT the API key.
        # 注意：媒体文件用的是独立的下载链接，**不需要也不应该带 API 密钥**
        with urllib.request.urlopen(url, timeout=90) as response, temporary.open("wb") as stream:
            # 服务器声明的文件大小（可能有，也可能没有），用来事后核对是否下完整
            declared = getattr(response, "headers", {}).get("Content-Length")
            size, prefix = 0, ""  # prefix 用来存文件头几个字节，稍后据此判断真实格式
            # 分块读取（每次 64KB）并写入，避免一次性把大文件读进内存
            while block := response.read(64 * 1024):
                size += len(block)
                if size > maximum:
                    raise ValueError("Media exceeds the classroom download limit.")
                if not prefix:
                    prefix = block[:16]  # 只保留最开始 16 字节
                stream.write(block)
        # 完整性校验：至少 16 字节，且（如果服务器声明了大小）实际大小必须与之一致
        if size < 16 or (declared is not None and size != int(declared)):
            raise ValueError("Media download is incomplete; keep the saved response and retry the download.")
        # 靠文件头「魔数」判断真实格式——不能只看 URL 后缀，因为它可能不带后缀
        if TASK == "video" and prefix[4:8] == b"ftyp":
            suffix = ".mp4"  # MP4 的容器标识 'ftyp' 在第 5-8 字节
        elif TASK == "image" and prefix.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif TASK == "image" and prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif TASK == "image" and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            # 认不出格式，说明下载到的不是预期媒体（可能是错误页面），拒绝保存
            raise ValueError("Downloaded data is not a supported media file.")
        # 校验全过，把临时文件改名为带正确后缀的正式文件
        target = path.with_suffix(suffix)
        temporary.replace(target)
        return target
    finally:
        # 无论成功失败，都清掉临时文件（已改名的话它已不存在，missing_ok 让这里不报错）
        temporary.unlink(missing_ok=True)


# Small support functions are kept here so this file works on its own.
def settings(require_key=True):
    """读取配置：先解析 .env，再用同名环境变量覆盖，返回 (key, model)。

    require_key=True 时校验密钥是否填写完整，否则抛 ValueError。
    MiniMax 和方舟两个密钥都会被收进 SECRETS，供 clean() 脱敏。
    """
    global SECRETS  # 声明下面的赋值改的是模块级变量，而不是新建一个局部变量
    values = {}
    file = ROOT / ".env"
    if file.exists():
        # 手写解析 .env：跳过空行和 # 注释，按第一个 = 切成键和值，再去掉引号
        for line in file.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip().strip('"').strip("'")
    # 环境变量优先于 .env，方便临时切换配置而不改文件
    for name in ("ARK_API_KEY", "MINIMAX_API_KEY", MODEL_FIELD):
        if name in os.environ:
            values[name] = os.environ[name]
    # 收集所有非空密钥，供后续脱敏使用
    SECRETS = [values.get(name, "") for name in ("ARK_API_KEY", "MINIMAX_API_KEY") if values.get(name)]
    key = values.get(KEY_FIELD, "")
    # 密钥体检：不能为空、不能还是模板里的 YOUR_ 占位符、不能把 "KEY=" 一起复制进来、
    # 不能含空格或不可打印字符（粘贴时最容易带进这些）
    if require_key and (not key or key.startswith("YOUR_") or key.startswith(KEY_FIELD + "=")
                        or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        raise ValueError(f"Fill {KEY_FIELD} in .env with just the complete key; no spaces or control characters.")
    return key, values.get(MODEL_FIELD) or DEFAULT_MODEL  # .env 没写模型名就用默认值


def clean(text):
    """把文本中出现的密钥替换成 [redacted]，防止密钥被写进日志或结果文件。"""
    for secret in SECRETS:
        text = text.replace(secret, "[redacted]")
    return text


def save_json(path, value):
    """把 value 以 UTF-8、保留中文、缩进 2 空格的 JSON 写入 path（写前先脱敏）。"""
    path.write_text(clean(json.dumps(value, ensure_ascii=False, indent=2)), encoding="utf-8")


def new_folder(label):
    """在 results/<任务名>/ 下按 UTC 时间戳新建结果目录，返回该路径。

    用时间戳做前缀，既能保证目录名唯一，又能让「按名字排序」等价于「按时间排序」——
    status() 取「最近一次运行」正是靠这个特性。
    """
    folder = ROOT / "results" / TASK / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + label)
    folder.mkdir(parents=True)
    return folder


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动跟随重定向：地址配置错误时立刻报错，而不是被悄悄转到别处。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """任何重定向请求一律拒绝。"""
        raise RuntimeError("API redirect refused; check the configured endpoint.")


def api_call(method, url, key, body=None, timeout=90):
    """向 url 发送带 Bearer 鉴权的 JSON 请求，返回解析后的响应字典。

    响应上限 16MB；HTTP 错误会带上响应体抛 RuntimeError；失败不做自动重试，
    因为失败的 POST 可能已经到达服务端（重试可能导致重复创建任务）。
    """
    # 有 body 就序列化成 JSON 字节流；GET 请求没有 body
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        # 用自定义的 opener 打开，以启用上面「拒绝重定向」的策略
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(16 * 1024 * 1024 + 1)  # 多读 1 字节，用来判断是否超限
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Response exceeds the lab's JSON limit.")
        value = json.loads(raw)
        # 响应必须是字典，且不能带 error 字段，否则视为异常响应
        if not isinstance(value, dict) or value.get("error"):
            raise ValueError("API returned an unexpected response: " + str(value))
        return value
    except urllib.error.HTTPError as error:
        # 服务端返回 4xx/5xx：把响应体读出来（通常写着具体原因），脱敏后抛出
        detail = error.read(4096).decode("utf-8", errors="replace")
        error.close()
        raise RuntimeError(clean(f"HTTP {error.code}: {detail}")) from None
    # No automatic retries: a failed POST might still have reached the provider.


if __name__ == "__main__":
    raise SystemExit(main())
