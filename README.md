# youdaonote-to-md

将有道云笔记批量导出为本地 Markdown 文件，图片自动内嵌为 base64，文件完全自包含，可直接导入 ima、Notion 等云端知识库。

> 基于 [liukui2020/youdaonote-md-exporter](https://github.com/liukui2020/youdaonote-md-exporter) 重构，重点完善了原版导出md丢失笔记的富文本格式的问题。

## 功能特性

### 核心功能

- 按原目录结构批量导出所有有道云笔记到本地
- 笔记格式自动转换：XML / JSON / HTML → Markdown
- 增量导出：仅下载新增或修改的笔记，跳过未更新的文件

### 图片处理

- **base64 内嵌模式（默认）**：图片以 `data:image/png;base64,...` 格式嵌入 Markdown，无需外部图片文件
- **本地文件模式**：图片下载到 `images/` 目录，Markdown 中使用相对路径引用
- 有道云笔记图片链接自动下载并替换
- `file:///` 本地绝对路径引用自动清理
- 图片文件名特殊字符自动清理（`(){}[]$~@%` 等），防止 Markdown 链接截断
- 可选上传图片到 SM.MS 图床
- **支持原笔记中富文本格式的导出**：富文本格式以内嵌HTML标签的方式直接加入到导出的Markdown文件中，目前在edge浏览器插件Markdown Viewer Enhanced和语雀上测试通过。

### 兼容性

- 兼容 Python 3.8+（已在 3.13 上验证通过）
- 兼容 Windows / macOS / Linux
- 修复 Windows 下 `os.rename` 目标已存在导致 `.note` 残留的问题

## 快速开始

### 1. 安装依赖

需要 Python 3.8+（推荐 3.11+），创建虚拟环境并安装依赖：

```shell
# Windows
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

```shell
# macOS / Linux
python3 -m venv venv
. venv/bin/activate
pip3 install -r requirements.txt
```

依赖列表（`requirements.txt`）：

| 依赖 | 版本要求 | 用途 |
|------|----------|------|
| requests | >=2.32.0 | HTTP 请求（兼容 Python 3.13） |
| markdownify | ==0.5.1 | HTML 转 Markdown |
| Brotli | >=1.1.0 | 解压响应（兼容 Python 3.13） |
| win32-setctime | ==1.1.0 | Windows 下设置文件创建时间 |

### 2. 获取有道云笔记 Cookie

由于有道云笔记登录升级了图形验证码，脚本使用 Cookie 登录。

1. 在浏览器中登录 [有道云笔记](https://note.youdao.com)
2. 按 `F12` 打开开发者工具 → Network 面板
3. 找到任意请求，复制 Cookie 中的以下三个值：
   - `YNOTE_CSTK`
   - `YNOTE_LOGIN`
   - `YNOTE_SESS`

> 也可安装浏览器插件 [Cookie-copy](https://chromewebstore.google.com/detail/cookie-copy/igfcbpdchlohbhjdfbcimhbpajlglaac) 快速复制。

### 3. 配置 cookies.json

将获取的 Cookie 填入 `cookies.json`：

```json
{
    "cookies": [
        ["YNOTE_CSTK", "你的CSTK值", ".note.youdao.com", "/"],
        ["YNOTE_LOGIN", "你的LOGIN值", ".note.youdao.com", "/"],
        ["YNOTE_SESS", "你的SESS值", ".note.youdao.com", "/"]
    ]
}
```

### 4. 配置 config.json

```json
{
    "local_dir": "",
    "ydnote_dir": "",
    "smms_secret_token": "",
    "is_relative_path": true,
    "is_base64_image": true
}
```

配置项说明：

| 参数 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `local_dir` | string | 本地存放导出文件的绝对路径，留空则导出到脚本目录下的 `youdaonote/` | `""` |
| `ydnote_dir` | string | 指定导出的有道云笔记文件夹名，留空则导出所有笔记 | `""` |
| `smms_secret_token` | string | SM.MS 图床 Token，留空则不上传 | `""` |
| `is_relative_path` | bool | 附件是否使用相对路径（base64 模式下图片不受影响） | `true` |
| `is_base64_image` | bool | 是否将图片以 base64 内嵌到 Markdown 中 | `true` |

### 5. 运行导出

```shell
# 激活虚拟环境后
python pull.py    # Windows
python3 pull.py   # macOS / Linux
```

导出完成后，所有笔记保存在 `youdaonote/` 目录下（或你指定的 `local_dir`）。

### 6. 增量导出

再次运行同一命令即可。脚本根据笔记修改时间判断是否需要更新，仅下载新增或修改的笔记，不会覆盖本地已修改的文件。

## 导出模式对比

### base64 内嵌模式（推荐，默认启用）

图片直接以 `data:image/png;base64,...` 格式嵌入 Markdown 文本中。

**适用场景**：导入 ima、Notion 等云端知识库

**优点**：
- 每个 Markdown 文件完全自包含，不依赖外部图片文件
- 导入云端知识库后图片正常显示
- 不生成 `images/` 目录，目录结构更简洁

**缺点**：
- 文件体积增大约 33%（base64 编码开销）
- 含大量图片的单个 Markdown 文件可能较大

### 本地文件模式（`is_base64_image: false`）

图片下载到各目录下的 `images/` 文件夹，Markdown 中使用相对路径引用。

**适用场景**：使用 Typora、VS Code 等本地编辑器查看

**优点**：
- 文件体积小
- 本地编辑器预览正常

**缺点**：
- 导入云端知识库时图片无法显示（图片不会上传）

## 项目结构

```
youdaonote-pull/
├── pull.py              # 主脚本入口
├── config.json          # 导出配置
├── cookies.json         # 有道云笔记 Cookie
├── requirements.txt     # Python 依赖
├── LICENSE           # MIT 许可证
├── core/                # 核心模块
│   ├── api.py           # 有道云笔记 API 封装（登录、获取目录、下载文件）
│   ├── covert.py        # 笔记格式转换（XML/JSON/HTML → Markdown）
│   ├── image.py         # 图片处理（base64 内嵌、本地下载、文件名清理、file:/// 清理）
│   ├── common.py        # 通用工具函数
│   └── log.py           # 日志配置（输出到文件和终端）
├── test/                # 测试文件
│   ├── test.py          # 单元测试
│   ├── test.note        # 测试用 XML 笔记
│   ├── test.json        # 测试用 JSON 笔记
│   └── test.md          # 测试用 Markdown 笔记
├── other/               # 其他脚本
├── logs/                # 运行日志（自动生成）
└── youdaonote/          # 导出目录（自动生成）
```

## 导出后目录结构示例

```
youdaonote/
├── 笔记文件夹1/
│   ├── 笔记A.md              # base64 图片内嵌在文件中
│   ├── 笔记B.md
│   └── 子文件夹/
│       └── 笔记C.md
├── 笔记文件夹2/
│   ├── 笔记D.md
│   └── attachments/          # 附件目录（仅当笔记包含附件时生成）
│       └── document.pdf
└── 根目录笔记.md
```

## 常见问题

### Cookie 过期 / 登录失败

Cookie 有有效期（通常数天到数周），过期后需重新获取并更新 `cookies.json`。如果出现 `Cookies 可能已过期` 提示，请重新登录有道云笔记网页版并获取最新 Cookie。

### 导出中途中断

直接重新运行脚本即可。增量同步机制会跳过已下载且未更新的笔记，不会重复下载。

### 部分笔记转换失败

极少数旧格式笔记（2017 年以前新建的 HTML 格式）可能转换失败，脚本会跳过并继续处理后续笔记，不会中断。查看 `logs/` 目录下的日志文件获取详细信息。

### 图片无法显示

- **导入云端知识库后图片不显示**：确保 `is_base64_image` 设置为 `true`
- **本地编辑器中图片不显示**：使用 `is_base64_image: false` 模式重新导出

### Windows 下 `.note` 文件残留

已在重构版本中修复。如果使用旧版本遇到此问题，手动删除 `.note` 文件即可，`.md` 文件已正常生成。

## 工作原理

1. 读取 `cookies.json` 中的 Cookie，通过 `requests.Session` 模拟浏览器登录有道云笔记
2. 调用有道云笔记 API 递归遍历目录树，获取所有笔记的文件 ID 和元信息
3. 逐个下载笔记文件，根据文件内容判断格式（XML / JSON / HTML / Markdown）
4. 将 XML / JSON / HTML 格式笔记转换为 Markdown
5. 扫描 Markdown 中的有道云笔记图片链接，下载图片并替换：
   - `is_base64_image=true`：图片编码为 base64 data URI 内嵌
   - `is_base64_image=false`：图片保存到本地 `images/` 目录，替换为相对路径
6. 清理 `file:///` 绝对路径引用，清理图片文件名特殊字符
7. 设置本地文件的创建时间和修改时间为有道云笔记中的时间

## 技术细节

### 笔记类型识别

| 有道云笔记格式 | 识别方式 | 转换目标 |
|----------------|----------|----------|
| `.md` | 文件后缀 | 无需转换 |
| `.note` (XML) | 内容以 `<?xml` 开头 | XML → Markdown |
| `.note` (JSON) | 内容以 `{"` 开头 | JSON → Markdown |
| `.note` (HTML) | XML 解析失败时回退 | HTML → Markdown |
| 其他 | 文件后缀 | 原样保存 |

### 图片文件名清理

有道云笔记中部分图片文件名包含 `)`、`(`、`[`、`]` 等特殊字符，会导致 Markdown 链接 `![](path)` 被提前截断。`_sanitize_filename()` 方法将 `(){}[]$~@%\`^` 等字符替换为 `_`，确保链接完整性。

### Windows 兼容性修复

Windows 下 `os.rename()` 在目标文件已存在时会抛出 `FileExistsError`，导致 `.note` 临时文件残留。修复方案：在 `os.rename()` 前先检查目标是否存在，若存在则先 `os.remove()` 再 `os.rename()`。

## 许可证

[MIT License](LICENSE.md) - Copyright (c) 2023 thinksmart-nicholas

