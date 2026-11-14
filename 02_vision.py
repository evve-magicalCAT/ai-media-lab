#!/usr/bin/env python3
"""Vision task using Seed 2.0 Lite. Run: python 02_vision.py --help"""
import argparse
import csv
import base64
import hashlib
import json
import os
from pathlib import Path
from datetime import datetime, timezone
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
TASK = "vision"
KEY_FIELD = "ARK_API_KEY"
MODEL_FIELD = "SEED_MODEL_ID"
DEFAULT_MODEL = "doubao-seed-2-0-lite-260428"
API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
SECRETS = []

# 1. Read or edit the task here. Credentials stay in .env.
PROMPT = (
    'Describe the supplied campus illustration in exactly three English sentences. Include the '
    'building sign and opening hours, the blue robot, and the orange traffic cones. Count '
    'objects carefully. Report only visible details; do not infer hidden information. Return '
    'only the three sentences.'
)
TEMPERATURE = 0.8
TOP_P = 1.0
MAX_TOKENS = 512
EXPERIMENT_PROMPT = (
    'Using only the visible campus image, write exactly three English sentences describing it '
    'to a new student. Include the building sign, displayed opening hours, the count and '
    'position of blue robots, the count of orange traffic cones, and the count and position of '
    'wooden benches. Count carefully and do not invent details. Return only the three '
    'sentences.'
)
IMAGE_FILE = "assets/campus.png"
IMAGE_BYTES = b""

# 2. Build one complete request, then send and save it.
def run_once(key, model, prompt, temperature, top_p, folder, dry_run=False):
    content = [{"type": "text", "text": prompt},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(IMAGE_BYTES).decode()}}]
    body = {"model": model,
            "messages": [{"role": "system", "content": "Explain clearly."}, {"role": "user", "content": content}],
            "thinking": {"type": "disabled"}, "temperature": temperature, "top_p": top_p,
            "max_tokens": MAX_TOKENS}
    saved_body = json.loads(json.dumps(body))
    saved_body["messages"][1]["content"][1]["image_url"]["url"] = "[actual bytes in input.png]"
    (folder / "input.png").write_bytes(IMAGE_BYTES)
    save_json(folder / "input_info.json", {"sha256": hashlib.sha256(IMAGE_BYTES).hexdigest()})
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
    folder = new_folder("sweep")
    (folder / "frozen_input.png").write_bytes(IMAGE_BYTES)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Save a request plan; no API call.")
    parser.add_argument("--check", action="store_true", help="Check local key and input; no API call.")
    parser.add_argument("--sweep", action="store_true", help="Run 18 independent HW2 calls.")
    parser.add_argument("--parameter", choices=["all", "temperature", "top_p"], default="all")
    args = parser.parse_args(argv)
    try:
        key, model = settings(require_key=args.check or not args.dry_run)
        global IMAGE_BYTES
        IMAGE_BYTES = (ROOT / IMAGE_FILE).read_bytes()
        if not IMAGE_BYTES.startswith(b"\x89PNG\r\n\x1a\n") or len(IMAGE_BYTES) > 20 * 1024 * 1024:
            raise ValueError("Use a valid PNG input under 20 MiB.")
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
    raise SystemExit(main())
