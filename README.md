# 教程离线封包工具

面向资料管理员的静态教程离线封包网页：上传含入口 `index.html` 的教程 ZIP，
系统按规则校验、重写链接并导出可离线浏览的封包。技术栈：Python + FastAPI +
lxml + tinycss2 + 原生 JavaScript。

## 启动（单条命令）

```bash
docker compose up --build
```

访问地址：**http://localhost:8000**

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
cd app && uvicorn main:app --reload
```

访问 http://127.0.0.1:8000

## 使用流程

1. 打开页面，拖入或选择教程 ZIP；
2. 点击「开始分析」，查看报告：阻断问题（文件 / 原因 / 来源 / 原 URL）与提示；
3. 无阻断问题且产物复检通过时，出现「下载 offline-tutorial.zip」按钮。

## 上传限制

- ZIP 须含入口 `index.html`（位于 ZIP 根目录）；
- 压缩 ≤ 10 MB，解压 ≤ 30 MB；
- ≤ 12 个 UTF-8 编码的 HTML/CSS 文件，≤ 30 张 PNG/JPEG；
- 仅允许 HTML/CSS/PNG/JPEG，其他文件类型报错。

## 处理规则

- **只处理** HTML 的 `a[href]`、`img[src]`、`link[rel=stylesheet][href]` 和 CSS 的 `url()`；
- 遇到 `<script>`、`<form>`、内联样式（`style` 属性与 `<style>` 块）、`srcset`
  或 CSS `@import`：列出文件与原因并**禁止下载**；
- 引用按所在 HTML 的本地 `<base>` 或所在 CSS 路径解析；`/` 开头的站点根路径指向 ZIP 根；
- 路径与片段各**百分号解码一次**，锚点保留；带查询串的本地 URL 拒绝；
- 缺失文件、越界路径、归一化重名、失效锚点、外部资源：显示来源与原 URL 并禁止下载；
- 外部普通超链接（http/https/ftp/mailto/tel、协议相对）原样保留；
- 解析出的引用无法唯一定位到原文字节时，禁止导出。

## 导出规则

- 仅含从入口 `index.html` 可达的文件（不可达文件在报告「提示」中列出并排除）；
- 本地 `<base>` 改为 `./`（当前页目录的相对基准）；
- 其他本地引用改为从所在文件到原目标的相对 URL；
- 改动过的 HTML/CSS 除 URL 属性值或 CSS URL 词元外，其余原文字节不变；
- 导出后自动**重扫产物**，确认内链与资源都能从入口离线到达，否则禁止下载。

## 目录结构

```
app/
  main.py          FastAPI 入口（/api/analyze、/api/export/{token}）
  analyzer.py      校验、引用解析、字节级重写、产物复检
  static/          原生 JavaScript 单页界面
Dockerfile
docker-compose.yml
requirements.txt
tests/smoke_test.py  规则冒烟测试（python3 tests/smoke_test.py）
```
