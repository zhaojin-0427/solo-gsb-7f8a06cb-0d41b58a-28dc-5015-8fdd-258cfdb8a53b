# 静态教程离线封包工具

面向资料管理员的离线教程封包网页：上传一个含入口 `index.html` 的 ZIP，
服务端按严格白名单校验、改写本地链接后重扫产物，只有从入口完全离线可达
的文件才会被导出。任一项校验不通过都会列出**来源文件、行号与原始 URL**，
并禁止下载。

技术栈：Python 3.11 · FastAPI · lxml · tinycss2 · 原生 JavaScript（前端无任何框架/CDN）。

## 一条命令启动

```bash
docker compose up --build
```

启动后访问：

- 本机：<http://localhost:8000>
- 局域网：`http://<宿主机IP>:8000`

停止：`Ctrl+C`，后台清理用 `docker compose down`。

> 不想用 Docker 时：`pip install -r requirements.txt` 后执行
> `uvicorn app.main:app --host 0.0.0.0 --port 8000`。

## 封包规则

### 体积与数量

| 项目 | 限制 |
| --- | --- |
| 压缩包体积 | ≤ 10 MB |
| 解压后总体积 | ≤ 30 MB |
| HTML/CSS（UTF-8） | ≤ 12 个 |
| PNG/JPEG 图片 | ≤ 30 张 |
| 入口 | ZIP 根目录必须有 `index.html` |

其余类型文件（`.js`、`.txt`、`.gif`、`.svg`、字体、目录项之外的任何内容）
一律拒绝。图片按魔数校验，HTML/CSS 必须是严格 UTF-8。

### 只处理这四类引用

- HTML：`a[href]`、`img[src]`、`<link rel="stylesheet" href>`（其余 `rel` 的 link 拒绝）
- CSS：`url(...)`（包括未加引号、单/双引号；注释和字符串里的 `url()` 不处理）
- 页面内可有至多一个 `<base href>`，导出时改写为当前页目录的相对基准 `.`

出现以下构造会**逐文件列出原因并禁止下载**：

- `<script>` 元素、`on*` 事件属性（脚本）
- `<form>`（表单）
- 元素 `style="…"` 属性（内联样式）
- `srcset` 属性
- CSS `@import`（外部样式表或 `<style>` 表体内都算）
- 其他一切承载 URL 的属性：`action`、`poster`、`iframe src`、`data`、
  `srcdoc`、`xlink:href`、`<meta http-equiv=refresh>` 等

### 路径与 URL 语义

- 相对路径按**所在 HTML 的本地 `<base>`**（无 base 即页面目录）解析；
  CSS 的 `url()` 一律按**所在 CSS 文件路径**解析（位于 `<style>` 内时按
  所属 HTML 路径）。
- 根相对路径（`/…`）指向 ZIP 根。
- 路径与片段各做**一次**百分号解码；`%2F` 不会被当作路径分隔符，
  `%2e%2e` 仍会被识别为越界。
- 锚点（`#fragment`）**原文保留**，并校验目标页存在同名 `id`/`a[name]`。
- 本地 URL 带查询串（`?…`）直接拒绝；外部普通超链接允许带查询串。
- 跳出 ZIP 根（`../`）、缺失文件、归一化重名（大小写不敏感 /
  Unicode NFC 冲突）、非 HTML 文件上的片段——全部拦截。
- 外部资源（`http(s)://…` 的 img/link/css-url、协议相对 `//host/…`）
  禁止封包；`a[href]` 的外部普通超链接（http/https/mailto/tel/ftp）
  **原样保留**，只在提示区列出。
- `data:`、`javascript:` 等协议拒绝。

### 改写与字节保证

- 导出仅包含**从 `index.html` 可达**的文件；不可达文件不导出但会列出。
- 所有本地引用改写为“**从所在文件到原目标**”的相对 URL（片段保留）；
  `<base href>` 改为 `.`。
- 改动过的 HTML/CSS，除 URL 属性值 / CSS `url()` 词元外，**其余原文字节
  逐字不变**（连外部链接的字节都不动）；未改动文件原样复制。
- 引用若无法唯一定位到原文字节（lxml 交叉计数不一致、属性重复、
  含 CSS 转义等），按策略禁止导出。

### 产物复检

导出 ZIP 在内存中重新组装后会再走一遍完整扫描：内链/资源必须全部命中
产物文件、锚点必须有效、不得残留禁止构造，且每个产物文件都必须能从
`index.html` 离线到达。复检失败同样禁止下载。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 上传页面（原生 JS） |
| `POST` | `/api/analyze` | 上传 `multipart/form-data` 字段 `file`（.zip），返回分析报告 |
| `GET` | `/api/report/{jobId}` | 重新获取报告 |
| `GET` | `/api/download/{jobId}` | 下载离线包；有阻塞问题时返回 409 |

分析结果保存在进程内存中（最多 50 个任务，30 分钟无访问自动过期），
服务不向磁盘写入任何用户数据。

## 目录结构

```
app/
  main.py       FastAPI 路由与内存任务表
  engine.py     扫描→解析→校验→可达性→改写→复检 总流程
  htmlscan.py   带字节位置的 HTML 扫描状态机 + lxml 计数交叉校验
  cssscan.py    tinycss2 url()/@import 扫描与括号区间回扫
  ziputil.py    ZIP 读取与体积/数量/编码/重名校验
  models.py     数据结构
  report.py     报告序列化（含中文类型标签）
  static/       单页前端（index.html + app.js）
tests/          pytest 端到端规则测试（47 个用例）
Dockerfile · docker-compose.yml · requirements.txt
```

## 运行测试

```bash
pip install -r requirements.txt pytest httpx
PYTHONPATH=. python -m pytest tests -q
```
