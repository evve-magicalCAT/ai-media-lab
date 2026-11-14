# ai-media-lab

一组「文本 / 视觉 / 图片 / 视频 / 角色抠图」的多模态小工具，每个脚本都能单独运行，
共享同一套 `.env` 配置与结果留档约定。

## 目录

| 文件 | 作用 | 模型 / 接口 |
| --- | --- | --- |
| `01_text.py` | 文本生成；支持 HW2 参数扫描（temperature / top_p 各 3 档 × 3 次） | 火山方舟 `Seed 2.0 Lite` |
| `02_vision.py` | 读一张图并用三句话描述（含参数扫描） | 火山方舟 `Seed 2.0 Lite` |
| `03_image.py` | 文生图，提示词运行时输入 | 火山方舟 `Seedream 5.0` |
| `04_video.py` | 视频生成：提交 / 查询 / 下载 + 网页界面 | `MiniMax H3` |
| `05_character.py` | 角色抠图（背景换纯白/透明）+ 双图角色一致性校验 | `Seed 2.0 Lite` + `Seedream 5.0` |
| `character_web.py` | 角色抠图网页后端 | — |
| `04_video.html` / `05_character.html` | 两个网页界面 | — |

## 快速开始

1. 复制配置模板并填入自己的密钥：

   ```bat
   copy .env.example .env
   ```

   `.env` 里需要：`ARK_API_KEY`（火山方舟）、`MINIMAX_API_KEY`（MiniMax 视频）。

2. 检查本地配置（不联网、不花钱）：

   ```bat
   python 01_text.py --check
   python 04_video.py --check
   python 05_character.py --check
   ```

3. 各个脚本的用法：

   ```bat
   python 01_text.py --dry-run              :: 只写请求计划，不联网
   python 01_text.py --sweep                :: 18 次参数扫描
   python 03_image.py                        :: 生成一张图（运行时会问你提示词）
   python 04_video.py serve --open           :: 视频界面 http://127.0.0.1:8000
   python 05_character.py --image in.png     :: 角色抠图（命令行）
   python character_web.py serve --open      :: 角色界面 http://127.0.0.1:8001
   ```

   也可以直接双击 `启动视频生成.bat` / `启动角色抠图.bat`（Windows）。

## 费用与安全约定

- `--dry-run` / `--check` **不联网**；`--sweep`、生成图片、生成视频、识别角色都会**真实计费**。
- 密钥只从 `.env` 或环境变量读取，**不会写进结果文件**：所有落盘内容都经过脱敏，
  密钥会被替换成 `[redacted]`。
- `.env`、`results/`、`web_sessions/`、`assets/` 均已加入 `.gitignore`，不会提交。

## 结果留档

每次运行都会在 `results/<任务>/<UTC 时间戳>-<标签>/` 下写入：

- `request.json` / `prompt.txt`：实际发出的请求与提示词
- `response.json`：服务端原始响应（视频/图片还会额外下载媒体文件）
- `run.json`：运行记录与状态机（`planned` → `submitting` → `succeeded` 等）

视频任务遵循「**先拿到 task_id 立刻存盘**」的原则，查询只认磁盘上已有的 ID，
`status` 永远不会新建任务，避免重复扣费。

## 环境

- Python 3.10+
- 依赖：`Pillow`、`numpy`（仅角色抠图的本地抠图与裁切需要）；其余全部使用标准库
- 无第三方 SDK、无需前端构建
