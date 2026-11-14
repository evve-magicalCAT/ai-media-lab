#!/usr/bin/env python3
"""Text task using Seed 2.0 Lite. Run: python 01_text.py --help"""
import argparse
import csv
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
TASK = "text"
KEY_FIELD = "ARK_API_KEY"
MODEL_FIELD = "SEED_MODEL_ID"
DEFAULT_MODEL = "doubao-seed-2-0-lite-260428"
API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
SECRETS = []

# 1. Read or edit the task here. Credentials stay in .env.
PROMPT = (
    'Explain an API to a first-year university student in exactly three short English '
    'sentences. Sentence 1 explains what an API does in plain language. Sentence 2 uses a '
    'fictional campus assistant asking a library service for opening hours as an example. '
    'Sentence 3 explains why a program needs to check the response before showing it to the '
    'student. Do not invent real campus information. Return only the three sentences, with no '
    'title or bullet points.'
)
# 采样参数：决定输出的随机程度，也是 HW2 要逐个扫描的两个变量。
TEMPERATURE = 0.8  # 温度：越高越随机发散（0~2），越低越稳定保守；单次调用默认 0.8
TOP_P = 1.0  # 核采样：只在累计概率前 100% 的候选词里挑选，1.0 表示不裁剪、全量参与
MAX_TOKENS = 512  # 输出长度上限（token）：限制回答最多能写多长，超出会被截断
EXPERIMENT_PROMPT = (
    'SUPPLIED FACTS (use only these facts):\nFictional campus library facts for the controlled '
    'experiment:\nThe library opens at 09:00 and closes at 18:00 every day.\nStudents can borrow '
    'up to three books at a time.\nEach loan lasts seven days.\nStudy-room reservations require a'
    ' student card.\nFood is not allowed inside the library.\nThe help desk is on the ground '
    'floor.\nNo information is provided about fines, renewal rules, weekend exceptions, or other'
    ' facilities.\n\nTASK:\nUsing only the supplied facts above, write exactly three English '
    'sentences that welcome a new student to this campus library and explain its rules. Include'
    ' all six stated library facts accurately. Vary the wording naturally while keeping the '
    'times, quantities, conditions, and location correct. Do not add information about fines, '
    'renewals, weekend exceptions, or other facilities. Return only the three sentences.'
)


# 2. Build one complete request, then send and save it.
def run_once(key, model, prompt, temperature, top_p, folder, dry_run=False):
    """执行一次完整的 API 调用，并把过程与结果全部保存到 folder 目录。

    依次写入 request.json / prompt.txt / run.json / response.json / output.txt。
    dry_run=True 时只生成请求计划、不联网，返回本次运行的 record 字典。
    """
    content = prompt
    body = {"model": model,
            "messages": [{"role": "system", "content": "Explain clearly."}, {"role": "user", "content": content}],
            "thinking": {"type": "disabled"}, "temperature": temperature, "top_p": top_p,
            "max_tokens": MAX_TOKENS}
    saved_body = json.loads(json.dumps(body))
    # Text input is already readable in the request.
    save_json(folder / "request.json", {"method": "POST", "url": API_URL, "body": saved_body})
    (folder / "prompt.txt").write_text(clean(prompt), encoding="utf-8")
    record = {"task": TASK, "model": model, "temperature": temperature, "top_p": top_p,
              "max_tokens": MAX_TOKENS, "dry_run": dry_run, "status": "planned" if dry_run else "submitting"}
    save_json(folder / "run.json", record)
    start = time.monotonic()
    try:
        if not dry_run:
            response = api_call("POST", API_URL, key, body)
            save_json(folder / "response.json", response)
            choice = response["choices"][0]
            answer = choice["message"]["content"]
            if not isinstance(answer, str):
                raise ValueError("The response has no text answer.")
            (folder / "output.txt").write_text(clean(answer), encoding="utf-8")
            record.update(status="succeeded", usage=response.get("usage"),
                          finish_reason=choice.get("finish_reason"), output_file="output.txt")
    except Exception as error:
        record.update(status="failed_or_unknown", error=clean(str(error)))
        print("Request failed; inspect the saved record before retrying:", clean(str(error)))
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - start, 3)
        save_json(folder / "run.json", record)
    print(record["status"].upper(), "Saved:", folder)
    return record


# 3. HW2: change one parameter at a time; use the same input for every call.
def run_sweep(key, model, parameter, dry_run):
    """HW2 参数扫描：用同一个固定输入，逐个改变 temperature / top_p 各重复 3 次。

    每轮都是独立请求，结果追加写入 summary.csv；一旦某次失败就立即停止。
    返回 0 表示全部成功，1 表示中途失败（保留已有结果，不要盲目重跑整个扫描）。
    """
    folder = new_folder("sweep")
    # The experiment prompt is a fixed string for this entire loop.
    plan = [(name, value, repeat) for name, values in
            (("temperature", (0.2, 0.8, 1.4)), ("top_p", (0.3, 0.7, 1.0)))
            if parameter in ("all", name) for value in values for repeat in (1, 2, 3)]
    save_json(folder / "experiment.json", {"model": model, "prompt": EXPERIMENT_PROMPT,
        "max_tokens": MAX_TOKENS, "thinking": "disabled", "planned_trials": plan,
        "dry_run": dry_run, "note": "Fresh messages each time; stop on first error. Remaining trials may be unrun."})
    with (folder / "summary.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["parameter", "value", "repeat", "status", "completion_tokens", "finish_reason",
                         "run_folder", "accuracy_observation", "variety_observation", "notes"])
        for name, value, repeat in plan:
            trial = folder / f"{name}-{value}-{repeat}"
            trial.mkdir()
            record = run_once(key, model, EXPERIMENT_PROMPT,
                             value if name == "temperature" else 1.0,
                             value if name == "top_p" else 1.0, trial, dry_run)
            writer.writerow([name, value, repeat, record["status"],
                (record.get("usage") or {}).get("completion_tokens", ""), record.get("finish_reason", ""),
                trial.name, "", "", ""])
            stream.flush()
            if record["status"] not in ("succeeded", "planned"):
                print("Sweep stopped. Keep these results; do not blindly rerun the whole sweep.")
                return 1
    print("Summary:", folder / "summary.csv")
    return 0


# 4. Choose a single call, request preview, or the controlled experiment.
def main(argv=None):
    """命令行入口：解析参数，分派到「单次调用 / 本地检查 / 参数扫描」三条路径。

    返回 0 表示成功，1 表示出错。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Save a request plan; no API call.")
    parser.add_argument("--check", action="store_true", help="Check local key and input; no API call.")
    parser.add_argument("--sweep", action="store_true", help="Run 18 independent HW2 calls.")
    parser.add_argument("--parameter", choices=["all", "temperature", "top_p"], default="all")
    args = parser.parse_args(argv)
    try:
        key, model = settings(require_key=args.check or not args.dry_run)
        # No image is needed for this task.
        if args.check:
            print("LOCAL CHECK: key is set (hidden); model:", model, "— live access is not tested.")
            return 0
        if args.parameter != "all" and not args.sweep:
            raise ValueError("--parameter requires --sweep.")
        if args.sweep:
            return run_sweep(key, model, args.parameter, args.dry_run)
        record = run_once(key, model, PROMPT, TEMPERATURE, TOP_P, new_folder("single"), args.dry_run)
        return 0 if record["status"] in ("succeeded", "planned") else 1
    except Exception as error:
        print("Error:", clean(str(error)))
        return 1


# Small support functions are kept here so this file works on its own.
def settings(require_key=True):
    """读取配置：先解析 .env，再用同名环境变量覆盖，返回 (key, model)。

    require_key=True 时校验密钥是否填写完整，否则抛出 ValueError。
    """
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
    """把文本中出现的密钥替换为 [redacted]，防止密钥被写进日志或结果文件。"""
    for secret in SECRETS:
        text = text.replace(secret, "[redacted]")
    return text


def save_json(path, value):
    """将 value 以 UTF-8、保留中文、缩进 2 空格的 JSON 写入 path（写前先脱敏）。"""
    path.write_text(clean(json.dumps(value, ensure_ascii=False, indent=2)), encoding="utf-8")


def new_folder(label):
    """在 results/<任务名>/ 下按 UTC 时间戳新建结果目录并返回该路径。"""
    folder = ROOT / "results" / TASK / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-") + label)
    folder.mkdir(parents=True)
    return folder


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动跟随重定向：地址配置错误时立即报错，而不是被悄悄转到别的地址。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """任何重定向请求一律拒绝。"""
        raise RuntimeError("API redirect refused; check the configured endpoint.")


def api_call(method, url, key, body=None, timeout=90):
    """向 url 发送带 Bearer 鉴权的 JSON 请求，返回解析后的响应字典。

    响应上限 16MB；HTTP 错误会带上响应体抛 RuntimeError；失败不做自动重试，
    因为失败的 POST 可能已经到达服务端。
    """
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
    raise SystemExit(main())
