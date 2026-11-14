#!/usr/bin/env python3
"""识别图片中的角色，并把背景换成空白（只保留角色）。

用法示例：
    python 05_character.py --image assets/campus.png              # 生成式改图（花钱）
    python 05_character.py --image assets/campus.png --method local  # 本地抠图（免费）
    python 05_character.py --image assets/campus.png --dry-run     # 只写请求计划，不联网
    python 05_character.py --image assets/campus.png --check       # 只检查本地配置，不联网
    python 05_character.py --resume <结果目录>                      # 复用已保存的响应，只重试下载

这个脚本把 02_vision.py（读图）和 03_image.py（生成图）串成一条流水线：
    第 1 步 视觉模型看图，找出角色是谁、长什么样、在画面什么位置；
    第 2 步 把第 1 步的描述整理成改图指令，把背景换成纯白，只留角色；
    第 3 步 保存结果，并把每一步的原始响应都存盘，方便事后回溯。

两种换背景的方式：
    --method api   （默认）用 Seedream 在参考图上做生成式编辑。能处理复杂背景，
                    但角色是"重新画一遍"，细节可能有出入，且每次调用会花钱。
    --method local 本地按颜色连通性抠图，不联网、不花钱，角色像素原样保留、
                    一致性 100%。适合插画/扁平背景；背景杂乱时效果有限。

背景有两种：
    --background white       纯白底（默认，两种 method 都支持）
    --background transparent 透明底（建议配 --method local，见下方实测说明）

两个后处理开关：
    --crop-bottom 0.09  先裁掉底部 9% 再送进模型。游戏/相机截图常自带信息条，
                        那是"图上真实存在的像素"，不裁掉可能被模型当画面内容留下。
                        同理还有 --crop-top / --crop-left / --crop-right。
    --trim              生成后按角色外接框裁掉四周空白。模型只保证"保持取景"，
                        不保证把角色放大，所以 16:9 出图常常角色偏小、四周大片留白。

    实测记录：用 doubao-seedream-5-0-260128 传官方参数
    background=transparent + output_format=png + 带 alpha 的 PNG 参考图，
    返回的 PNG 仍是 RGB（colortype=2）、没有 alpha 通道。所以"要真正的透明底"
    请用 --method local；--method api --background transparent 仍会照常发参数，
    但结果不保证透明，脚本会打印一行提醒。

    size 的真实约束（都来自服务端的 400 报错原文，不是文档推测）：
      * 只接受 WIDTHxHEIGHT / '2k' / '3k' / '4k'，没有 auto、没有 1k；
      * 总像素不得低于 3686400（约 3.7MP）。
    所以 --size auto 由脚本按输入图比例换算成满足下限的 WxH。
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent
TASK = "character"
KEY_FIELD = "ARK_API_KEY"
VISION_MODEL_FIELD = "SEED_MODEL_ID"
IMAGE_MODEL_FIELD = "SEEDREAM_MODEL_ID"
DEFAULT_VISION_MODEL = "doubao-seed-2-0-lite-260428"
DEFAULT_IMAGE_MODEL = "doubao-seedream-5-0-260128"
CHAT_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
IMAGE_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
SECRETS = []

# 默认输入图：和 02_vision.py 用同一张，方便对比两个脚本的结果
DEFAULT_IMAGE = "assets/campus.png"
# 输出尺寸。默认 auto：由脚本按输入图的比例算出具体宽高再发出去。
# 为什么不用服务端的预设档位：实测 "2K" 会被解析成 2048x2048 正方形，
# 一张 16:9 的图会变成"角色缩在正方形中间、上下大片空白"，取景被破坏。
# 而服务端明确拒绝 "auto"——它的原话是：
#   size must be one of 'WIDTHxHEIGHT', '2k', '3k', or '4k'
# （注意：没有 1k，也没有 auto）。所以 auto 只能由客户端换算成 WxH。
SIZE = "auto"
MAX_OUTPUT_SIDE = 4096          # 显式指定 WxH 时的最长边上限
MIN_OUTPUT_PIXELS = 3686400     # 服务端下限：实测 2048x1152 被拒，原话是
                                # "image size must be at least 3686400 pixels"

# 第 1 步：认角色的提示词。要求它输出机器能接着用的结构化描述，
# 并且明确禁止编造看不见的信息——否则后面的改图指令会带着幻觉一起生成。
IDENTIFY_PROMPT = (
    'Look at the supplied image and identify the main character (the primary subject a viewer '
    'would name first). Reply with ONLY a JSON object, no markdown fence, using exactly these '
    'keys:\n'
    '{"found": true/false, "name": "short name of the character", "kind": "e.g. robot / animal / '
    'person / mascot / object", "appearance": "2-4 sentences describing only visible details: '
    'colours, shapes, proportions, clothing, accessories, markings", "pose": "what the character '
    'is doing and its facing direction", "position": "where it sits in the frame", '
    '"other_subjects": ["any other notable subjects that are NOT the character"], '
    '"background": "what the background currently shows"}\n'
    'Describe only what is actually visible. Do not invent details, text, brands, or names that '
    'are not present. If there is no clear main character, set "found" to false and explain in '
    '"name".'
)

# 第 2 步：改图指令。按 Seedream 图像编辑的推荐写法组织成四段——
# 「编辑对象 / 操作 / 保持不变 / 避免项」，比一句话笼统描述更容易守住角色一致性。
# 主角的具体信息由第 1 步识别结果动态拼进 "KEEP UNCHANGED" 段。
EDIT_OBJECT = "the main character of the reference image"
EDIT_ACTION = ("Replace the entire background with a flat, uniform pure white background "
               "(#FFFFFF). Remove every background element completely.")
EDIT_AVOID = ("a busy or textured background, scenery, floor, ground, buildings, sky, plants, "
              "props, other characters, text, logos, watermarks, borders, frames, cast shadows on "
              "the background, gradients, vignettes")


def build_edit_prompt(character):
    """把第 1 步认出来的角色信息拼成第 2 步的改图指令。

    结构参考 Seedream 图像编辑的推荐写法：先说清编辑对象，再说做什么、
    什么必须保持不变、什么必须避免。角色描述越具体，生成时越不容易把角色
    换成"另一个长得差不多的东西"。
    """
    keep = ["the character's identity, silhouette, proportions and scale",
            "the exact colours, materials and markings of the character",
            "the character's pose, facial expression and facing direction",
            "the character's outline quality: crisp, clean anti-aliased edges",
            "the overall composition: the character keeps its current position and framing"]
    # 把识别到的具体特征补进"必须保持不变"，越具体越守得住
    if character.get("name"):
        keep.append(f"it is still the same individual: {character['name']}"
                    + (f" ({character['kind']})" if character.get("kind") else ""))
    if character.get("appearance"):
        keep.append("visible appearance to preserve exactly: " + str(character["appearance"]))
    if character.get("pose"):
        keep.append("pose to preserve exactly: " + str(character["pose"]))

    parts = [
        "Edit the supplied reference image.",
        f"EDIT TARGET: {EDIT_OBJECT}"
        + (f" — {character['name']}" if character.get("name") else "") + ".",
        f"ACTION: {EDIT_ACTION}",
        "KEEP UNCHANGED:",
    ]
    parts += [f"- {item}" for item in keep]
    parts.append(f"AVOID: {EDIT_AVOID}")
    if character.get("other_subjects"):
        others = "; ".join(str(item) for item in character["other_subjects"])
        parts.append(f"ALSO REMOVE these other subjects entirely (do not place them on the new "
                     f"background): {others}")
    parts.append("OUTPUT: one image containing only the character on a flat pure white background. "
                 "Do not redraw, restyle or replace the character.")
    return "\n".join(parts)


def parse_character(text):
    """从视觉模型的回答里解析出角色 JSON。

    模型有时会把 JSON 包在 ```json 代码块里，或在前后加一句解释，
    所以这里先剥掉代码块、再截取最外层花括号，最后交给 json.loads。
    解析不出来就抛 ValueError——宁可报错，也不要拿半截文本去改图。
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        # 去掉开头的 ```json / ``` 和结尾的 ```
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        cleaned = cleaned.rsplit("```", 1)[0]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("视觉模型没有返回 JSON，无法确定角色。原始回答：" + (text or "")[:200])
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as error:
        raise ValueError(f"视觉模型的回答不是合法 JSON：{error}") from None
    if not isinstance(value, dict):
        raise ValueError("视觉模型的回答不是 JSON 对象。")
    return value


def detect_banner_rows(image_path):
    """检测图片底部有没有"截图自带的信息条"，返回应该保留的高度（像素）。

    游戏截图常在底部压一条浅色信息栏（标题、相机参数、色卡等）。那是图上真实
    存在的像素，不排掉会有两个坏处：模型可能把它当画面内容保留；自动框选算出来的
    y 坐标还会一路延伸到画面最底部，把信息条也框进去。

    判据：从最底行往上，只要"整行都是浅色且近乎无彩色"就继续往上，直到遇到
    有内容的行。信息条必须连续且够高（≥2% 画面高）才算数，避免误伤普通浅色画面。
    返回"建议保留的高度"；没有信息条时返回原高度。
    """
    from PIL import Image
    import numpy as np
    with Image.open(image_path) as handle:
        array = np.asarray(handle.convert("RGB")).astype(np.int16)
    height, width = array.shape[:2]
    light = (array.min(axis=2) > 195) & ((array.max(axis=2) - array.min(axis=2)) < 28)
    uniform = light.mean(axis=1) > 0.8          # 该行超过 80% 是浅色
    keep = height
    for y in range(height - 1, -1, -1):
        if uniform[y]:
            keep = y
        else:
            break
    band = height - keep
    if band < max(8, int(height * 0.04)):
        return height                            # 太薄，多半不是信息条

    # 相对判据：信息条一定比它上面的画面"更亮、更平"。
    # 不能用绝对阈值——信息条里往往印着深色文字和 logo（实测某张只有 91.7% 是浅色），
    # 卡太严会漏掉；而暗色场景底部那种"均匀但有内容"的画面又会误判成信息条。
    above = array[max(0, keep - band):keep]
    if not len(above):
        return height
    band_light = light[keep:].mean()
    above_light = light[max(0, keep - band):keep].mean()
    band_bright = float(array[keep:].mean())
    above_bright = float(above.mean())
    # 必须明显更亮（≥25）或明显更平（浅色占比高出 ≥25 个百分点）
    if band_bright - above_bright < 25 and band_light - above_light < 0.25:
        return height
    return keep


# 第 3 步：流水线主体
def normalize_box(box):
    """把模型给的框换算成 0~1 的比例框。

    实测模型**不遵守**"给我 0~1"的要求，而是按 0~1000 的刻度返回
    （例如 left=37 表示 0.037，top=417 表示 0.417）。如果直接当 0~1 用，
    框会跑到画面外，裁出来全是空白。所以这里按数值范围自动判断刻度：
    只要有任何一端大于 1.5，就认为它是 0~1000 刻度，统一除以 1000。
    """
    if not isinstance(box, dict):
        return None
    try:
        values = {k: float(box[k]) for k in ("left", "top", "right", "bottom")}
    except (KeyError, TypeError, ValueError):
        return None
    if max(values.values()) > 1.5:
        values = {k: v / 1000.0 for k, v in values.items()}
    left, top = values["left"], values["top"]
    right, bottom = values["right"], values["bottom"]
    # 容错：模型偶尔会把左右/上下写反
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    left, top = max(0.0, left), max(0.0, top)
    right, bottom = min(1.0, right), min(1.0, bottom)
    if right - left < 0.01 or bottom - top < 0.01:
        return None
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def expand_box(box, scale=0.3, min_margin=0.02, min_height=0.78, min_width=0.16):
    """把框外扩成一个"能装下整个角色"的可用框。

    为什么要这么麻烦：模型给的框经常只圈住**躯干**，把头和脚都漏在外面。
    实测女角色的框高度只有 0.24（y 0.417-0.653），而她的真实范围是 y 0.150-0.880
    （高度 0.73）。这种情况下无论按框自身比例怎么加边距都追不上——0.6 倍只到 0.275，
    1.0 倍也只到 0.181，仍然切头。

    所以这里分两步：
      1. 先按框自身大小的 scale 倍外扩（保留左右、上下的相对余量）；
      2. 如果框高仍不足 min_height，就以原框中心为准把高度撑到 min_height
         （宽度同理用 min_width 兜底），四周留白总比切掉角色好。

    角色通常是竖长的，切头切脚是主要风险，所以 min_height 给得比 min_width 大。
    """
    if not box:
        return None
    width = box["right"] - box["left"]
    height = box["bottom"] - box["top"]
    pad_x = max(width * scale, min_margin)
    pad_y = max(height * scale, min_margin)
    left = box["left"] - pad_x
    right = box["right"] + pad_x
    top = box["top"] - pad_y
    bottom = box["bottom"] + pad_y

    # 第二步：撑到最小可用尺寸，以原框中心为基准
    center_x = (box["left"] + box["right"]) / 2
    center_y = (box["top"] + box["bottom"]) / 2
    if right - left < min_width:
        left, right = center_x - min_width / 2, center_x + min_width / 2
    if bottom - top < min_height:
        top, bottom = center_y - min_height / 2, center_y + min_height / 2

    # 落到画面内；如果贴边了，就往另一侧平移，尽量不缩小
    if left < 0:
        right = min(1.0, right - left); left = 0.0
    if right > 1:
        left = max(0.0, left - (right - 1.0)); right = 1.0
    if top < 0:
        bottom = min(1.0, bottom - top); top = 0.0
    if bottom > 1:
        top = max(0.0, top - (bottom - 1.0)); bottom = 1.0
    return {"left": left, "top": top, "right": right, "bottom": bottom,
            "padded": True}


# 多角色识别用的提示词：只要"有几个、分别在哪"，不要模型起名字下定义，
# 因为定位是否够准才是这里的关键。
DETECT_PROMPT = (
    "List every distinct character (person, humanoid, creature, robot, mascot) in this image. "
    "A character is a subject a viewer would name; ignore scenery, plants, buildings, vehicles, "
    "and text.\n"
    'Reply with ONLY a JSON object, no markdown fence: {"characters": [ {"name": "...", '
    '"kind": "...", "box": {"left": 0-1000, "top": 0-1000, "right": 0-1000, '
    '"bottom": 0-1000}} ]}\n'
    "Give each box as integers on a 0-1000 scale, where 0 is the left/top edge of the image and "
    "1000 is the right/bottom edge. Round the numbers to the nearest 50 (i.e. 0, 50, 100, ...) — "
    "do not give overly tight boxes. The box must FULLY contain the whole character including "
    "hair, headwear, hands, feet and any long coat, cape or tail — never crop the head or the "
    "feet. If the bottom of the image has a white UI bar, ignore that bar entirely and never "
    "include it in a box. If only one character is present, return one item. If none, return an "
    "empty list."
)


def detect_characters(key, vision_model, image_path, timeout=180):
    """让视觉模型列出图中的角色，返回 [{"name","kind","box"(0~1)}...]。

    这是给网页界面"自动框选"用的。注意定位精度有限（实测会漏掉头顶），
    所以调用方应该用 expand_box() 外扩后再裁切。
    """
    raw = image_path.read_bytes()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("输入图片必须是 PNG。")
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    body = {"model": vision_model, "thinking": {"type": "disabled"},
            "temperature": 0.1,  # 定位要稳定，不要发挥
            "messages": [{"role": "system", "content": "You locate characters in images precisely."},
                         {"role": "user", "content": [
                             {"type": "text", "text": DETECT_PROMPT},
                             {"type": "image_url", "image_url": {"url": uri}}]}]}
    answer = api_call("POST", CHAT_URL, key, body)["choices"][0]["message"]["content"]
    if not isinstance(answer, str):
        raise ValueError("视觉模型没有返回文本。")
    parsed = parse_character(answer)
    results = []
    for item in parsed.get("characters") or []:
        box = normalize_box(item.get("box"))
        if box is None:
            continue  # 框不可用就跳过，不猜
        results.append({"name": str(item.get("name") or "character"),
                        "kind": str(item.get("kind") or ""),
                        "box": box, "source": "auto"})
    return results


COMPARE_PROMPT = (
    "You are given two images, labelled IMAGE A and IMAGE B. Decide whether the character "
    "(the main subject a viewer would name first) in IMAGE A is the same character as the one "
    "in IMAGE B.\n"
    "Judge identity, not pose or background: the same character may be posed differently, "
    "re-coloured by lighting, drawn at another angle, or placed on a different background. "
    "Compare face/head shape, hair, eyes, distinctive clothing, accessories, markings, species "
    "and body proportion. Two images of the same character are a match; two different "
    "characters of the same art style are not.\n"
    'Reply with ONLY a JSON object, no markdown fence, exactly:\n'
    '{"character_a": "one short sentence describing the character in IMAGE A", '
    '"character_b": "one short sentence describing the character in IMAGE B", '
    '"verdict": "same" | "different" | "uncertain", '
    '"confidence": 0.0-1.0, '
    '"matching_features": ["concrete features that agree", ...], '
    '"differences": ["concrete features that disagree", ...], '
    '"notes": "anything that made the judgement hard"}\n'
    "Be strict about evidence: if an image shows several characters, compare the most "
    "prominent one. If either image has no recognisable character, use 'uncertain' and say "
    "so in notes. Never guess identity from text or watermarks in the image."
)


def palette_profile(image_path, count=5):
    """主色指纹 + 背景占比（本地计算，不联网）。

    返回 {"colors": [...], "background_ratio": 0~1}：
      colors            排除近白/透明像素后的前几种主色
      background_ratio  近白/透明像素的占比

    background_ratio 很关键：只有两张图**都是白底抠图**时，主色距离才有参考意义。
    实测过反例——两张来自同一张截图的裁切图（共享同一片草地背景）算出的颜色距离
    比"同一角色 vs 它的白底输出"还小，拿它判断会得出完全相反的结论。
    所以调用方必须先用 background_ratio 判断"这图是不是抠图"，再决定要不要用距离。
    """
    from PIL import Image
    with Image.open(image_path) as handle:
        image = handle.convert("RGBA")
        image.thumbnail((32, 32))
    counts = {}
    total = foreground = background = 0
    for r, g, b, a in image.getdata():
        total += 1
        if a < 40 or (r > 244 and g > 244 and b > 244):
            background += 1
            continue                      # 透明或近纯白都算背景
        foreground += 1
        key = (r // 32 * 32, g // 32 * 32, b // 32 * 32)
        counts[key] = counts.get(key, 0) + 1
    colours = []
    if foreground:
        ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:count]
        colours = [{"rgb": list(colour), "share": round(hits / foreground, 3)}
                   for colour, hits in ranked]
    return {"colors": colours,
            "background_ratio": round(background / total, 3) if total else 0.0}


def dominant_colors(image_path, count=5):
    """只要主色（palette_profile 的简化版），保留给命令行/调试用。"""
    return palette_profile(image_path, count)["colors"]


def palette_distance(colours_a, colours_b):
    """两组主色的粗略差异（0~441 的欧氏距离均值，越小越像）。

    取各自前 3 个主色，两两配对取最近的算平均距离。
    仅在两张图都是白底抠图时有参考意义，见 palette_profile 的说明。
    """
    if not colours_a or not colours_b:
        return None
    a = [c["rgb"] for c in colours_a[:3]]
    b = [c["rgb"] for c in colours_b[:3]]
    distances = []
    for left in a:
        nearest = min(sum((left[i] - right[i]) ** 2 for i in range(3)) ** 0.5 for right in b)
        distances.append(nearest)
    return round(sum(distances) / len(distances), 1)


def compare_characters(key, vision_model, image_a, image_b):
    """判断两张图片里的角色是不是同一个角色。

    把两张图一起发给视觉模型（同一请求里的两个 image_url），让它给出
    verdict / confidence / 相符特征 / 不同特征；同时用本地色彩指纹做一个
    辅助参考值，方便人交叉判断（模型可能被画风或配色误导）。

    返回字典：model_verdict / confidence / matching / differences / notes /
              character_a / character_b / palette_distance / usage ...
    """
    raw_a = Path(image_a).read_bytes()
    raw_b = Path(image_b).read_bytes()
    for label, raw in (("A", raw_a), ("B", raw_b)):
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"图片 {label} 必须是 PNG。")
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError(f"图片 {label} 不能超过 20 MiB。")
    uri_a = "data:image/png;base64," + base64.b64encode(raw_a).decode()
    uri_b = "data:image/png;base64," + base64.b64encode(raw_b).decode()

    body = {"model": vision_model, "thinking": {"type": "disabled"},
            "temperature": 0.0,   # 判断要稳定，不要发挥
            "messages": [{"role": "system", "content": "You compare characters across images precisely."},
                         {"role": "user", "content": [
                             {"type": "text", "text": COMPARE_PROMPT},
                             {"type": "text", "text": "IMAGE A:"},
                             {"type": "image_url", "image_url": {"url": uri_a}},
                             {"type": "text", "text": "IMAGE B:"},
                             {"type": "image_url", "image_url": {"url": uri_b}}]}]}
    response = api_call("POST", CHAT_URL, key, body)
    answer = response["choices"][0]["message"]["content"]
    if not isinstance(answer, str):
        raise ValueError("视觉模型没有返回文本。")
    parsed = parse_character(answer)

    verdict = str(parsed.get("verdict") or "uncertain").strip().lower()
    if verdict not in ("same", "different", "uncertain"):
        verdict = "uncertain"
    # 置信度容错：有的模型给 0~1，有的给 0~100
    try:
        confidence = float(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence > 1.0:
        confidence = confidence / 100.0
    confidence = max(0.0, min(1.0, confidence))

    profile_a = palette_profile(image_a)
    profile_b = palette_profile(image_b)
    # 只有两张图都是"白底/透明底抠图"时，主色距离才可用。
    # 否则（含背景的截图）它会把"共享同一片背景"误算成"颜色很像"，
    # 实测出现过相反结论，所以宁可不给这个数字。
    both_cutouts = profile_a["background_ratio"] >= 0.5 and profile_b["background_ratio"] >= 0.5
    distance = palette_distance(profile_a["colors"], profile_b["colors"]) if both_cutouts else None
    if both_cutouts:
        palette_note = "两张都是白底/透明底抠图，主色距离有参考意义。"
    else:
        palette_note = ("至少一张图带背景，主色会受背景干扰，所以不计算颜色距离；"
                        "请以模型判定和下面的特征对比为准。")
    return {
        "verdict": verdict,
        "confidence": round(confidence, 3),
        "character_a": str(parsed.get("character_a") or ""),
        "character_b": str(parsed.get("character_b") or ""),
        "matching_features": [str(x) for x in (parsed.get("matching_features") or [])][:12],
        "differences": [str(x) for x in (parsed.get("differences") or [])][:12],
        "notes": str(parsed.get("notes") or ""),
        "palette_a": profile_a["colors"],
        "palette_b": profile_b["colors"],
        "background_ratio_a": profile_a["background_ratio"],
        "background_ratio_b": profile_b["background_ratio"],
        "palette_distance": distance,
        "palette_note": palette_note,
        "usage": response.get("usage"),
        "model": vision_model,
    }


def resolve_size(size, image_path=None, folder=None, min_pixels=MIN_OUTPUT_PIXELS):
    """校验并换算输出尺寸，返回能直接发给服务端的字符串。

    支持三种写法：
      auto            按输入图比例换算成 WxH（默认）
      2K / 3K / 4K    服务端预设（官方只接受这三个大写档位）
      1024x1024       显式宽x高

    服务端实际接受的集合（来自它自己的 400 报错原文）是
    'WIDTHxHEIGHT' / '2k' / '3k' / '4k'，不接受 auto、也不接受 1K，
    所以 auto 必须在这里换算，不能直接发。
    """
    text = str(size).strip()
    upper = text.upper()
    if upper == "AUTO":
        if image_path is None:
            raise ValueError("auto 需要输入图片才能换算出具体宽高。")
        try:
            from PIL import Image
            with Image.open(image_path) as handle:
                width, height = handle.size
        except ImportError:
            raise ValueError("--size auto 需要 Pillow 才能读取图片尺寸；"
                             "或者显式指定 --size 2K。") from None
        # 目标：保持输入图比例，面积刚好达到下限（默认是服务端的 3.7MP 底线；
        # 想要更清晰可以调大 --min-pixels，让整张图（包括角色）拿到更多像素）
        scale = (min_pixels / (width * height)) ** 0.5
        wide = max(8, int(round(width * scale / 8)) * 8)
        tall = max(8, int(round(height * scale / 8)) * 8)
        # 取整后可能又掉到下限以下，往上补一格再确认一次
        while wide * tall < min_pixels and max(wide, tall) < MAX_OUTPUT_SIDE:
            wide += 8
            tall += 8
        if wide * tall < min_pixels:
            raise ValueError(f"无法为 {width}x{height} 换算出合法的输出尺寸。")
        chosen = f"{wide}x{tall}"
    elif upper in ("2K", "3K", "4K"):
        chosen = upper
    elif "x" in text.lower():
        left, _, right = text.lower().partition("x")
        if not (left.isdigit() and right.isdigit() and int(left) > 0 and int(right) > 0):
            raise ValueError(f"尺寸格式不对：{size}（应形如 1024x1024）")
        wide, tall = int(left), int(right)
        if wide * tall < min_pixels:
            # 提前拦掉，省得白跑一次请求再拿 400
            raise ValueError(f"尺寸太小：{wide}x{tall} 只有 {wide * tall} 像素，"
                             f"服务端要求至少 {min_pixels} 像素（约 {min_pixels / 1e6:.1f}MP）。")
        chosen = f"{wide}x{tall}"
    else:
        raise ValueError(f"--size 只能是 auto / 2K / 3K / 4K 或形如 1024x1024 的宽x高，收到的是：{size}")

    if folder is not None:
        save_json(folder / "size_resolution.json",
                  {"requested": text, "sent": chosen, "min_pixels": min_pixels})
    return chosen


def run_pipeline(key, vision_model, image_model, image_path, folder, dry_run=False,
                 background="white", method="api", tolerance=28, remove_islands=True,
                 size=SIZE, trim=False, padding=32, crop=None, focus=None, crop_info=None,
                 prepared=False):
    """跑完「认角色 → 改背景 → 下载」全流程，把每步产物都写进 folder。

    method="api"   ：用 Seedream 做生成式改图，能处理复杂背景（花钱）。
    method="local" ：用本地色块连通算法抠图，角色像素原样保留（不花钱、不联网），
                     但只认颜色连通性，适合插画/扁平背景。
    crop           ：可选，{"bottom":0.094,...} 比例的预裁切，用来先去掉截图自带的
                     UI 信息条/水印，避免它们被模型当成画面内容保留。

    返回 record 字典；即使中途失败也已经写盘，便于排查。
    """
    record = {"task": TASK, "vision_model": vision_model, "image_model": image_model,
              "input_file": image_path.name, "dry_run": dry_run, "background": background,
              "method": method, "size": size,
              "status": "planned" if dry_run else "identifying"}
    save_json(folder / "run.json", record)
    # 输入图通常已经由 prepare_input 裁好并冻结成 folder/input.png；
    # prepared=True 表示调用方已经裁好了，这里不要再裁一次（网页界面走这条路）。
    folder.mkdir(parents=True, exist_ok=True)
    if not prepared and crop_info is None:
        image_path, crop_info = crop_fractions(image_path, folder, focus=focus, **(crop or {}))
    if crop_info:
        record["crop"] = crop_info
    raw = image_path.read_bytes()
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("输入图片必须是 PNG（本脚本按 PNG 校验文件头）。")
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("输入图片不能超过 20 MiB。")
    data_uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    if not crop_info:
        # 调用方没准备好输入图时，冻结一份原始输入，保证事后能复现
        (folder / "input.png").write_bytes(raw)
    save_json(folder / "run.json", record)

    # ---- 第 1 步：找角色（两种 method 都要，先弄清"角色是谁"才好保留）----
    identify_body = {"model": vision_model, "thinking": {"type": "disabled"},
                     "temperature": 0.2,  # 认角色要稳定，不要发挥
                     "messages": [{"role": "system", "content": "You identify image content precisely."},
                                  {"role": "user", "content": [
                                      {"type": "text", "text": IDENTIFY_PROMPT},
                                      {"type": "image_url", "image_url": {"url": data_uri}}]}]}
    saved = json.loads(json.dumps(identify_body))
    saved["messages"][1]["content"][1]["image_url"]["url"] = "[actual bytes in input.png]"
    save_json(folder / "identify_request.json", {"method": "POST", "url": CHAT_URL, "body": saved})

    if dry_run:
        # 预演：把要发的请求计划都写好，但一个请求都不发
        if method == "local":
            record.update(status="planned",
                          note="dry-run：未识别、未抠图。本地抠图不会调用任何接口。")
            save_json(folder / "run.json", record)
            print("PLANNED — no API call (local method). Saved:", folder)
            return record
        plan_prompt = build_edit_prompt({"name": "[由第 1 步识别结果填入]"})
        edit_body = {"model": image_model, "prompt": plan_prompt, "size": size,
                     "image": "[actual bytes in input.png]",
                     "response_format": "url", "stream": False}
        save_json(folder / "edit_request.json", {"method": "POST", "url": IMAGE_URL, "body": edit_body})
        (folder / "edit_prompt.txt").write_text(clean(plan_prompt), encoding="utf-8")
        record.update(status="planned", note="dry-run：未识别、未生成，只写了请求计划")
        save_json(folder / "run.json", record)
        print("PLANNED — no API call. Saved:", folder)
        return record

    start = time.monotonic()
    try:
        response = api_call("POST", CHAT_URL, key, identify_body)
        save_json(folder / "identify_response.json", response)
        answer = response["choices"][0]["message"]["content"]
        if not isinstance(answer, str):
            raise ValueError("视觉模型没有返回文本。")
        character = parse_character(answer)
        record.update(character=character, identify_usage=response.get("usage"))
        if not character.get("found", True):
            record.update(status="no_character",
                          error="第 1 步没有找到明确的角色：" + str(character.get("name")))
            save_json(folder / "run.json", record)
            print("No clear character found; nothing was edited. Saved:", folder)
            return record
        record.update(status="editing")
        save_json(folder / "run.json", record)

        # ---- 第 2 步（本地）：不联网抠图，角色像素原样保留 ----
        if method == "local":
            stats = remove_background_local(folder / "input.png", folder / "output.png",
                                            tolerance=tolerance, remove_islands=remove_islands,
                                            transparent=(background == "transparent"))
            record.update(status="succeeded", output_file="output.png", local_stats=stats)
            if trim:
                try:
                    record["trim"] = trim_to_character(folder / "output.png", folder / "output.png",
                                                       padding=padding)
                except Exception as error:
                    record["trim"] = {"trimmed": False, "reason": clean(str(error))}
            record["elapsed_seconds"] = round(time.monotonic() - start, 3)
            save_json(folder / "run.json", record)
            print("SUCCEEDED (local, no API cost). Character:",
                  record.get("character", {}).get("name"))
            print("Image:", folder / "output.png")
            print("Background removed:", f"{stats['background_removed_ratio'] * 100:.1f}% of pixels")
            print("Saved:", folder)
            return record

        # ---- 第 2 步（api）：让 Seedream 保留角色、把背景换成空白 ----
        prompt = build_edit_prompt(character)
        (folder / "edit_prompt.txt").write_text(clean(prompt), encoding="utf-8")
        # image 字段带上原图，让模型在"这张图"上做编辑，而不是凭空重画一个角色。
        # 官方 OpenAPI 里 image 的类型是「字符串或字符串数组」，单个 data URI 合法。
        edit_body = {"model": image_model, "prompt": prompt, "size": size, "image": data_uri,
                     "response_format": "url", "stream": False,
                     "output_format": "png",   # 要留透明通道就必须输出 PNG
                     "watermark": False}       # 显式关水印：官方 schema 没写默认值，别赌
        if background == "transparent":
            # 官方参数 background=transparent 能直接给透明底（比纯白更"空白"），
            # 但要求参考图是带 alpha 的 PNG、且输出 PNG，所以放在这里单独开启。
            edit_body["background"] = "transparent"
        saved = json.loads(json.dumps(edit_body))
        saved["image"] = "[actual bytes in input.png]"
        save_json(folder / "edit_request.json", {"method": "POST", "url": IMAGE_URL, "body": saved})

        # 先存响应再下载：图片链接是临时的，下载失败时靠这份响应就能 --resume 重试
        response = api_call("POST", IMAGE_URL, key, edit_body)
        save_json(folder / "edit_response.json", response)
        record.update(status="generated", image_usage=response.get("usage"))
        save_json(folder / "run.json", record)

        # ---- 第 3 步：把结果下载到本地 ----
        output = download_media(response["data"][0]["url"], folder / "output.png")
        record.update(status="succeeded", output_file=output.name)
        # 可选：裁掉四周多余空白，让角色真正占满画面
        if trim:
            try:
                record["trim"] = trim_to_character(folder / output.name, folder / output.name,
                                                   padding=padding)
            except Exception as error:
                # 裁切失败不该让整个任务算失败：原图已经下载好了
                record["trim"] = {"trimmed": False, "reason": clean(str(error))}
    except Exception as error:
        # 已经拿到生成结果、只是下载失败 → download_failed（能用 --resume 救回来）
        # 其他情况无法确定服务端到底生成没有 → failed_or_unknown，不要盲目重跑以免重复扣费
        record.update(status="download_failed" if record.get("status") == "generated"
                                else "failed_or_unknown", error=clean(str(error)))
        save_json(folder / "run.json", record)
        print("Error:", clean(str(error)), "Inspect the saved responses before rerunning.")
        return record
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - start, 3)
        save_json(folder / "run.json", record)

    print("SUCCEEDED. Character:", record.get("character", {}).get("name"))
    print("Image:", folder / record["output_file"])
    print("Saved:", folder)
    return record


def prepare_input(image_path, folder, crop=None, focus=None):
    """把"送进模型的输入图"准备好：先按 --crop-*/--focus 裁切，写进 folder/input.png。

    必须单独成一步、并且在算 --size auto 之前调用——因为输出尺寸是按**裁切后**
    的输入图比例换算的。如果先按原图算尺寸再裁切，比例就会对不上，
    模型只好自己重新构图，结果就是"角色被缩小、四周补白"（实测踩过这个坑）。
    返回 (输入图路径, 裁切信息或 None)。
    """
    folder.mkdir(parents=True, exist_ok=True)
    return crop_fractions(image_path, folder, focus=focus, **(crop or {}))


def resume_download(key, folder):
    """复用某次已保存的响应，只重试下载，不重新生成（不花钱）。"""
    folder = folder.resolve()
    record = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    if record.get("task") != TASK or record.get("dry_run"):
        raise ValueError("请选择一次真实的 character 运行目录。")
    if record.get("status") == "succeeded" and record.get("output_file") \
            and (folder / record["output_file"]).exists():
        print("Already saved:", folder / record["output_file"])
        return 0
    response = json.loads((folder / "edit_response.json").read_text(encoding="utf-8"))
    output = download_media(response["data"][0]["url"], folder / "output.png")
    record.update(status="succeeded", output_file=output.name)
    save_json(folder / "run.json", record)
    print("SUCCEEDED. Image:", output)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help=f"输入图片，默认 {DEFAULT_IMAGE}")
    parser.add_argument("--dry-run", action="store_true", help="Save request plans; no API call.")
    parser.add_argument("--check", action="store_true", help="Check local key and input; no API call.")
    parser.add_argument("--resume", type=Path, help="Retry the download from a saved run; no new generation.")
    parser.add_argument("--background", default="white", choices=["white", "transparent"],
                        help="空白背景：white=纯白（默认）；transparent=透明（仅 --method api，"
                             "需要参考图是带 alpha 的 PNG）。")
    parser.add_argument("--size", default=SIZE,
                        help="输出尺寸：auto=按输入图比例换算（默认）；也支持 2K/3K/4K 或 1024x1024。")
    parser.add_argument("--crop-bottom", type=float, default=0.0,
                        help="先裁掉图片底部这个比例（0~1），用来去掉截图自带的信息条/水印。")
    parser.add_argument("--crop-top", type=float, default=0.0, help="先裁掉图片顶部这个比例（0~1）。")
    parser.add_argument("--crop-left", type=float, default=0.0, help="先裁掉图片左侧这个比例（0~1）。")
    parser.add_argument("--crop-right", type=float, default=0.0, help="先裁掉图片右侧这个比例（0~1）。")
    parser.add_argument("--focus", default=None,
                        help='只保留画面里的一块再送模型，形如 "0.30,0.20,0.72,0.95"（左,上,右,下，0~1）。'
                             "让角色占满参考图，输出里的角色会清晰得多。")
    parser.add_argument("--min-pixels", type=int, default=MIN_OUTPUT_PIXELS,
                        help=f"--size auto 的目标总像素（默认 {MIN_OUTPUT_PIXELS}，服务端底线；"
                             "调大可以让整张图更清晰，例如 4000000）。")
    parser.add_argument("--trim", action="store_true",
                        help="生成后裁掉四周多余空白，让角色占满画面（会就地覆盖输出文件）。")
    parser.add_argument("--padding", type=int, default=32,
                        help="仅 --trim：裁切后四周保留多少像素边距（默认 32）。")
    parser.add_argument("--method", default="api", choices=["api", "local"],
                        help="api=用 Seedream 生成式改图（花钱，能处理复杂背景）；"
                             "local=本地抠图（免费，角色像素原样保留，适合插画/扁平背景）。")
    parser.add_argument("--tolerance", type=int, default=28,
                        help="仅 local：颜色相差多少以内算同一种背景色（0~255，越大删得越多）。")
    parser.add_argument("--keep-islands", action="store_true",
                        help="仅 local：保留不与边缘连通的孤立色块（默认会删掉明显小于角色的杂物）。")
    args = parser.parse_args(argv)
    if not 0 <= args.tolerance <= 255:
        print("Error: --tolerance 必须在 0~255 之间。")
        return 1
    crop = {"bottom": args.crop_bottom, "top": args.crop_top,
            "left": args.crop_left, "right": args.crop_right}
    if any(not 0.0 <= value < 1.0 for value in crop.values()):
        print("Error: 裁切比例必须落在 0~1 之间。")
        return 1
    if crop["top"] + crop["bottom"] >= 1.0 or crop["left"] + crop["right"] >= 1.0:
        print("Error: 裁切比例过大，上下（或左右）之和不能达到 1。")
        return 1
    if args.min_pixels < 1:
        print("Error: --min-pixels 必须是正数。")
        return 1
    try:
        focus = parse_focus(args.focus)
    except ValueError as error:
        print("Error:", error)
        return 1
    if args.background == "transparent" and args.method == "api":
        # 实测（doubao-seedream-5-0-260128）：即使按官方要求传
        # background=transparent + output_format=png + alpha 参考图，
        # 返回的 PNG 仍是 RGB（colortype=2），没有 alpha 通道。
        # 所以透明底改走本地抠图，这里只是提醒一声，不阻断流程。
        print("[i] 注意：实测 Seedream 的 background=transparent 不保证返回 alpha 通道；"
              "要真正的透明底请加 --method local。")

    folder, record = None, None
    try:
        # local 抠图完全不联网，所以不需要密钥；--check / --resume 也各自按需
        needs_key = not (args.dry_run or args.resume or args.method == "local")
        key, vision_model, image_model = settings(require_key=args.check or needs_key)
        if args.check:
            print("LOCAL CHECK: key is set (hidden); vision model:", vision_model,
                  "; image model:", image_model, "— live access is not tested.")
            return 0
        if args.resume:
            if args.dry_run:
                raise ValueError("--resume 和 --dry-run 是两个不同的动作。")
            return resume_download(key, args.resume)

        image_path = (args.image or ROOT / DEFAULT_IMAGE)
        if not image_path.is_absolute():
            image_path = ROOT / image_path
        if not image_path.exists():
            raise ValueError(f"找不到输入图片：{image_path}")
        folder = new_folder("run")
        # 先裁切（--crop-*/--focus），再按裁切后的图算输出尺寸，最后才跑流水线
        prepared, crop_info = prepare_input(image_path, folder, crop, focus)
        output_size = resolve_size(args.size, prepared, folder, args.min_pixels)
        record = run_pipeline(key, vision_model, image_model, prepared, folder,
                              args.dry_run, args.background, args.method, args.tolerance,
                              not args.keep_islands, output_size, args.trim, args.padding,
                              None, None, crop_info)
        return 0 if record.get("status") in ("succeeded", "planned") else 1
    except Exception as error:
        if folder and record:
            save_json(folder / "run.json", record)
        print("Error:", clean(str(error)))
        return 1


# ---- 本地抠图（不花钱、不联网）：把与边缘相连的近似同色像素刷成纯白 ----
def remove_background_local(source, target, tolerance=28, feather=True, remove_islands=True,
                            transparent=False):
    """把背景刷成纯白，角色的像素一个都不改。

    思路：从图片四条边出发做连通扩散，把与边缘相连、且颜色相近的像素判为背景。
    这样"被角色围起来的同色区域"不会被误删，角色本身也不会被重画——
    角色区域的像素原样保留，所以角色一致性是 100%。

    代价是：它不"理解"画面，只认颜色连通性。适合插画、扁平背景这类场景；
    背景复杂（照片、渐变、杂乱场景）时效果有限，那种情况请用 --method api。

    tolerance：颜色相差多少以内算"同一种背景色"（0~255，越大删得越多）。
    feather：边缘是否做一圈半透明过渡，避免锯齿。
    remove_islands：是否把不与边缘连通、又明显小于主角的孤立色块一并删掉
                    （用来去掉站在角色前面的锥桶、长椅之类的杂物）。
    """
    from PIL import Image  # 只在用到本地抠图时才需要 Pillow
    import numpy as np
    image = Image.open(source).convert("RGB")
    width, height = image.size
    pixels = np.asarray(image).astype(np.int16)
    # 背景色的参考值取自四条边，而不是某一个角，避免角落恰好是角色时判错
    border = np.concatenate([pixels[0, :], pixels[-1, :], pixels[:, 0], pixels[:, -1]])
    reference = np.median(border, axis=0)

    # 与参考背景色足够接近的像素才算"候选背景"
    close = (np.abs(pixels - reference).sum(axis=2) <= tolerance * 3)

    # 从四条边向内做广度优先扩散，只有与边缘连通的候选像素才真正算背景。
    # 用 Python 的 deque 逐像素扩展，大图会慢一些，但换来的是不依赖任何外部模型。
    from collections import deque
    visited = np.zeros((height, width), dtype=bool)
    queue = deque()
    for x in range(width):
        for y in (0, height - 1):
            if close[y, x] and not visited[y, x]:
                visited[y, x] = True
                queue.append((y, x))
    for y in range(height):
        for x in (0, width - 1):
            if close[y, x] and not visited[y, x]:
                visited[y, x] = True
                queue.append((y, x))
    while queue:
        y, x = queue.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < height and 0 <= nx < width and not visited[ny, nx] and close[ny, nx]:
                visited[ny, nx] = True
                queue.append((ny, nx))

    mask = visited  # True = 背景
    # 角色内部的"洞"（比如站在角色前面的锥桶、脚边的长椅）不与边缘连通，
    # 上面的连通扩散碰不到它们，会被当成角色一起保留下来。这里可选地把
    # "明显比主角小、又不接触主角"的孤立块也刷白，让结果更接近"只剩角色"。
    islands = 0
    if remove_islands:
        mask, islands = _mark_small_islands(pixels, mask)

    # 输入图自带的透明区域本来就算背景（例如上游工具已经抠掉的部分）
    source = Image.open(source)
    source_alpha = np.asarray(source.convert("RGBA"))[:, :, 3] if "A" in source.getbands() else None
    if source_alpha is not None:
        mask = mask | (source_alpha == 0)
        # 原图半透明的边缘也算背景过渡，避免抠完出现一圈硬边
        mask = mask | (source_alpha < 128)

    result = np.asarray(image).copy()
    result[mask] = (255, 255, 255)

    if feather and mask.any() and not mask.all():
        # 在背景边缘一圈做半透明过渡：把紧贴背景的角色边缘像素按比例向白色靠一点
        from PIL import ImageFilter
        alpha = Image.fromarray((~mask * 255).astype(np.uint8))
        softened = alpha.filter(ImageFilter.GaussianBlur(1.2))
        ratio = (np.asarray(softened).astype(np.float32) / 255.0)[..., None]
        # 只在"原本是背景边缘"的过渡带上混合，避免把角色内部也洗淡
        band = (np.asarray(softened) > 8) & (np.asarray(softened) < 247)
        base = np.asarray(image).astype(np.float32)
        blended = base * ratio + 255.0 * (1 - ratio)
        result[band] = blended[band].astype(np.uint8)

    if transparent:
        # 输出真正的透明底：背景 alpha 置 0，角色保持不透明。
        # 这是本地方法独有的能力——实测 Seedream 的 background=transparent
        # 在这台账号/模型上并没有真的返回 alpha 通道（PNG 仍是 RGB），
        # 所以"要透明底"最可靠的办法是走这条本地路径。
        from PIL import ImageFilter
        alpha = ((~mask) * 255).astype(np.uint8)
        if feather and mask.any() and not mask.all():
            alpha = np.asarray(Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(0.8)))
        if source_alpha is not None:
            # 与原图的 alpha 取较小值：原图透明的地方保持透明
            alpha = np.minimum(alpha, source_alpha)
        rgba = np.dstack([result, alpha]).astype(np.uint8)
        Image.fromarray(rgba, mode="RGBA").save(target)
    else:
        Image.fromarray(result).save(target)

    removed = float(mask.mean())
    return {"background_removed_ratio": round(removed, 4),
            "reference_background_rgb": [int(v) for v in reference],
            "tolerance": tolerance,
            "removed_island_blocks": islands,
            "output_mode": "RGBA (transparent)" if transparent else "RGB (white)"}


def _mark_small_islands(pixels, mask):
    """把不与边缘连通的孤立色块按大小分流：小的当杂物删掉，大的当主角留下。

    返回 (新的 mask, 被删掉的块数)。判断顺序：先按"颜色是否相近"把前景切成
    若干块，再按面积从大到小看——最大的那块一定是主角，必须原样保留；
    其余块只有在"面积明显更小"且"不与主角相邻"时才删除，
    这样贴在角色身上的装饰（帽子、手中的道具）不会被误删。
    """
    import numpy as np
    height, width = mask.shape
    foreground = ~mask
    visited = np.zeros((height, width), dtype=bool)
    blocks = []
    # 用显式的栈做四邻域遍历，避免大图递归爆栈
    for y0 in range(height):
        for x0 in range(width):
            if not foreground[y0, x0] or visited[y0, x0]:
                continue
            colour = pixels[y0, x0]
            stack = [(y0, x0)]
            visited[y0, x0] = True
            cells = []
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < height and 0 <= nx < width and not visited[ny, nx] \
                            and foreground[ny, nx] \
                            and abs(int(pixels[ny, nx][0]) - int(colour[0])) \
                            + abs(int(pixels[ny, nx][1]) - int(colour[1])) \
                            + abs(int(pixels[ny, nx][2]) - int(colour[2])) <= 60:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
            blocks.append(cells)

    if len(blocks) < 2:
        return mask, 0  # 只有一块，说明整张图就是一个角色，没有杂物可删

    blocks.sort(key=len, reverse=True)
    main = blocks[0]
    main_set = set(main)
    new_mask = mask.copy()
    removed_blocks = 0
    for cells in blocks[1:]:
        # 明显更小（不到主角的 1/4）才考虑，避免把角色的组成部分切掉
        if len(cells) * 4 > len(main):
            continue
        # 与主角相邻的块视为角色的一部分（帽子、配件），保留
        touches_main = False
        for y, x in cells:
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if (y + dy, x + dx) in main_set:
                    touches_main = True
                    break
            if touches_main:
                break
        if touches_main:
            continue
        for y, x in cells:
            new_mask[y, x] = True
        removed_blocks += 1
    return new_mask, removed_blocks


def trim_to_character(source, target, padding=32, tolerance=24):
    """把结果图裁到角色外接框，去掉四周多余的空白（就地重写 target）。

    提示词只能要求"保持角色位置和取景"，模型不一定会把角色放大占满画面；
    实测 16:9 输入出来的角色仍偏小、四周大片空白。所以生成之后，
    按"非背景像素"的外接框裁一次，才是真正可用的"只有角色"的图。

    返回裁切信息字典。padding 是保留多少像素的安全边距。
    """
    from PIL import Image
    import numpy as np
    with Image.open(source) as handle:
        image = handle.convert("RGBA" if "A" in handle.getbands() else "RGB")
    array = np.asarray(image)

    if array.shape[2] == 4:
        # 透明底：不透明的像素就是角色
        subject = array[:, :, 3] > 8
    else:
        # 白底：明显不是白色的像素就是角色
        subject = (np.abs(array[:, :, :3].astype(np.int16) - 255).sum(axis=2) > tolerance * 3)

    if not subject.any():
        return {"trimmed": False, "reason": "整张图都是背景色，没有可裁的角色"}

    rows = np.where(subject.any(axis=1))[0]
    cols = np.where(subject.any(axis=0))[0]
    top = max(0, int(rows[0]) - padding)
    bottom = min(array.shape[0], int(rows[-1]) + 1 + padding)
    left = max(0, int(cols[0]) - padding)
    right = min(array.shape[1], int(cols[-1]) + 1 + padding)
    cropped = image.crop((left, top, right, bottom))
    cropped.save(target)
    return {"trimmed": True, "box": [left, top, right, bottom],
            "before": [array.shape[1], array.shape[0]],
            "after": [cropped.size[0], cropped.size[1]]}


def parse_focus(text):
    """解析 --focus 的 "L,T,R,B" 比例框（0~1），返回 dict 或 None。

    这是提升清晰度的关键手段：角色如果只占画面一小块，模型就只按那一小块
    的分辨率去画它；先框住角色、让它在参考图里占满，输出里的角色才会清晰。
    """
    if not text:
        return None
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 4:
        raise ValueError('--focus 需要四个 0~1 的比例，形如 "0.30,0.20,0.72,0.95"（左,上,右,下）')
    try:
        left, top, right, bottom = (float(p) for p in parts)
    except ValueError:
        raise ValueError("--focus 的四个值都必须是数字。") from None
    if not all(0.0 <= v <= 1.0 for v in (left, top, right, bottom)):
        raise ValueError("--focus 的比例必须落在 0~1 之间。")
    if right - left < 0.05 or bottom - top < 0.05:
        raise ValueError("--focus 框太小了（宽或高不足 5%）。")
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def crop_fractions(image_path, folder, bottom=0.0, top=0.0, left=0.0, right=0.0, focus=None):
    """按比例裁掉图片四周，把裁后的图写进 folder/input.png，返回新路径。

    为什么需要它：很多截图自带 UI 边框/水印/信息条（游戏截图的标题栏、
    相机参数栏等）。这些是"图上真实存在的像素"，改图模型可能把它当成
    画面内容保留下来，所以要先把它们裁掉再送进去。

    参数都是 0~1 的比例。全都为 0 时直接返回原图，不产生新文件。
    focus 是 {"left","top","right","bottom"} 的比例框，用来"只保留画面里的一块"
    ——把角色框满参考图，输出里的角色才会清晰（见 --focus 的说明）。
    """
    if not any((bottom, top, left, right)) and not focus:
        return image_path, None
    from PIL import Image
    with Image.open(image_path) as handle:
        image = handle.convert("RGB")
        width, height = image.size
        x0 = int(round(width * left))
        x1 = width - int(round(width * right))
        y0 = int(round(height * top))
        y1 = height - int(round(height * bottom))
        if focus:
            # focus 在"去掉四周之后"的范围内再取一块，两个参数可以叠加使用
            span_x, span_y = x1 - x0, y1 - y0
            fx0 = x0 + int(round(span_x * focus["left"]))
            fy0 = y0 + int(round(span_y * focus["top"]))
            fx1 = x0 + int(round(span_x * focus["right"]))
            fy1 = y0 + int(round(span_y * focus["bottom"]))
            x0, y0, x1, y1 = fx0, fy0, fx1, fy1
        if x1 - x0 < 64 or y1 - y0 < 64:
            raise ValueError("裁切后剩下的区域小于 64x64，请检查 --crop-* / --focus 参数。")
        cropped = image.crop((x0, y0, x1, y1))
        target = folder / "input.png"
        cropped.save(target)
    info = {"original_size": [width, height], "cropped_size": [cropped.size[0], cropped.size[1]],
            "box": [x0, y0, x1, y1], "fractions": {"bottom": bottom, "top": top,
                                                   "left": left, "right": right},
            "focus": focus}
    save_json(folder / "crop.json", info)
    return target, info


# ---- 以下是与 01~04 保持一致的辅助函数 ----
def settings(require_key=True):
    """读取 .env（再用环境变量覆盖），返回 (key, 视觉模型名, 生图模型名)。"""
    global SECRETS
    values = {}
    file = ROOT / ".env"
    if file.exists():
        for line in file.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip().strip('"').strip("'")
    for name in ("ARK_API_KEY", "MINIMAX_API_KEY", VISION_MODEL_FIELD, IMAGE_MODEL_FIELD):
        if name in os.environ:
            values[name] = os.environ[name]
    SECRETS = [values.get(name, "") for name in ("ARK_API_KEY", "MINIMAX_API_KEY") if values.get(name)]
    key = values.get(KEY_FIELD, "")
    if require_key and (not key or key.startswith("YOUR_") or key.startswith(KEY_FIELD + "=")
                        or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        raise ValueError(f"Fill {KEY_FIELD} in .env with just the complete key; no spaces or control characters.")
    return (key, values.get(VISION_MODEL_FIELD) or DEFAULT_VISION_MODEL,
            values.get(IMAGE_MODEL_FIELD) or DEFAULT_IMAGE_MODEL)


def clean(text):
    """把文本里出现的密钥替换成 [redacted]，避免写进日志或结果文件。"""
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
    """禁用自动跟随重定向：地址配错时立刻报错，而不是被悄悄转到别处。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("API redirect refused; check the configured endpoint.")


def api_call(method, url, key, body=None, timeout=180):
    """带 Bearer 鉴权发 JSON 请求，返回解析后的响应字典。

    生成图片可能要等一会儿，所以默认超时给到 180 秒（比文本任务的 90 秒宽）。
    失败不自动重试：POST 可能已经到达服务端，重试会造成重复生成、重复扣费。
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


def download_media(url, path, maximum=25 * 1024 * 1024):
    """下载生成的图片：限制体积、先写 .part 再改名、按文件头判断真实格式。"""
    if not url.startswith("https://"):
        raise ValueError("Expected an HTTPS media URL.")
    temporary = path.with_suffix(".part")
    try:
        # 媒体链接是独立的下载地址，不需要也不应该带 API 密钥
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
            raise ValueError("Media download is incomplete; keep the saved response and retry.")
        if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            suffix = ".png"
        elif prefix.startswith(b"\xff\xd8\xff"):
            suffix = ".jpg"
        elif prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            raise ValueError("Downloaded data is not a supported image file.")
        target = path.with_suffix(suffix)
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    # 双击运行时窗口不会一闪而过，方便看清结果路径或报错
    exit_code = main()
    if len(sys.argv) == 1:
        input("\n按回车键关闭这个窗口...")
    raise SystemExit(exit_code)
