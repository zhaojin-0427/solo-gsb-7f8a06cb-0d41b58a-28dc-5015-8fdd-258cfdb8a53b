/* 静态教程离线封包工具前端（原生 JavaScript，无第三方依赖） */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const drop = $("drop"), fileInput = $("fileInput"),
        resultBox = $("result"), uploadErr = $("uploadErr"),
        spinner = $("spinner"), downloadBtn = $("downloadBtn");

  let currentJob = null;

  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("drag");
    if (e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) upload(fileInput.files[0]);
  });
  downloadBtn.addEventListener("click", () => {
    if (currentJob) window.location.href = "/api/download/" + currentJob;
  });

  async function upload(file) {
    uploadErr.classList.add("hidden");
    if (!file.name.toLowerCase().endsWith(".zip")) {
      showUploadErr("请选择 .zip 文件");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      showUploadErr("压缩包超过 10 MB 限制（实际 " +
        (file.size / 1024 / 1024).toFixed(2) + " MB）");
      return;
    }
    spinner.style.display = "inline";
    resultBox.classList.add("hidden");

    const fd = new FormData();
    fd.append("file", file, file.name);
    try {
      const resp = await fetch("/api/analyze", { method: "POST", body: fd });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail || ("HTTP " + resp.status));
      }
      const report = await resp.json();
      currentJob = report.jobId;
      render(report);
    } catch (err) {
      showUploadErr("上传/分析失败：" + err.message);
    } finally {
      spinner.style.display = "none";
      fileInput.value = "";
    }
  }

  function showUploadErr(msg) {
    uploadErr.textContent = msg;
    uploadErr.classList.remove("hidden");
    spinner.style.display = "none";
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }

  function render(r) {
    resultBox.classList.remove("hidden");

    const banner = $("banner");
    if (r.summary.downloadable) {
      banner.className = "banner ok";
      banner.innerHTML = "✅ 校验与离线复检全部通过：仅 <b>" +
        r.summary.reachableFiles + "</b> 个从入口可达的文件将被导出，" +
        "可下载离线封包。";
      downloadBtn.disabled = false;
    } else {
      banner.className = "banner no";
      banner.innerHTML = "⛔ 发现 <b>" + r.summary.problemCount +
        "</b> 个阻塞问题，<b>禁止下载</b>。请按下列来源文件与原因修正后重新上传。";
      downloadBtn.disabled = true;
    }

    $("stats").innerHTML = [
      ["文件总数", r.summary.totalFiles],
      ["可达（将导出）", r.summary.reachableFiles],
      ["阻塞问题", r.summary.problemCount],
      ["提示/外链", r.summary.noticeCount],
    ].map(([k, v]) => '<div class="stat"><b>' + v + "</b>" + k + "</div>").join("");

    $("problemCount").textContent = r.summary.problemCount;
    $("noticeCount").textContent = r.summary.noticeCount;

    renderProblems(r.problems, $("problems"), false);
    renderProblems(r.notices, $("notices"), true);
    renderFiles(r.files);
    renderRefs(r.refs);

    $("problemsDetails").open = r.summary.problemCount > 0;
  }

  function sourceCell(p) {
    let s = esc(p.source || "（压缩包）");
    if (p.line) s += ":" + p.line;
    return '<span class="src">' + s + "</span>";
  }

  function renderProblems(items, container, isNotice) {
    if (!items.length) {
      container.innerHTML = '<div class="empty">无。</div>';
      return;
    }
    container.innerHTML =
      '<table><thead><tr><th style="width:110px">类型</th><th>原因</th>' +
      '<th style="width:260px">来源</th><th style="width:240px">原始 URL</th>' +
      "</tr></thead><tbody>" +
      items.map((p) =>
        "<tr>" +
        '<td><span class="badge ' + (isNotice ? "amber" : "red") + '">' +
          esc(p.kindLabel) + "</span></td>" +
        "<td>" + esc(p.message) + "</td>" +
        "<td>" + sourceCell(p) + "</td>" +
        '<td><span class="url">' + esc(p.url || "") + "</span></td>" +
        "</tr>").join("") +
      "</tbody></table>";
  }

  function renderFiles(files) {
    if (!files.length) {
      $("files").innerHTML = '<div class="empty">包内无有效文件。</div>';
      return;
    }
    $("files").innerHTML =
      '<table><thead><tr><th>路径</th><th style="width:90px">类型</th>' +
      '<th style="width:110px">大小</th><th style="width:110px">可达性</th>' +
      "</tr></thead><tbody>" +
      files.map((f) =>
        "<tr>" +
        '<td class="src">' + esc(f.path) + "</td>" +
        '<td><span class="tag ' + f.kind + '">' + f.kind.toUpperCase() + "</span></td>" +
        "<td>" + fmtSize(f.size) + "</td>" +
        "<td>" + (f.reachable
          ? '<span style="color:var(--green)">可达</span>'
          : '<span class="tag off">不导出</span>') + "</td>" +
        "</tr>").join("") +
      "</tbody></table>";
  }

  const ROLE_LABEL = {
    a_href: "a[href]", img_src: "img[src]",
    link_stylesheet_href: "link 样式表", css_url: "CSS url()", base_href: "base"
  };

  function renderRefs(refs) {
    if (!refs.length) {
      $("refs").innerHTML = '<div class="empty">未解析到引用。</div>';
      return;
    }
    $("refs").innerHTML =
      '<table><thead><tr><th style="width:120px">角色</th><th>来源</th>' +
      '<th>原始 URL</th><th>解析目标 / 状态</th>' +
      "</tr></thead><tbody>" +
      refs.map((r) =>
        "<tr>" +
        '<td><span class="tag">' + esc(ROLE_LABEL[r.role] || r.role) + "</span></td>" +
        '<td class="src">' + esc(r.source) + ":" + r.line + "</td>" +
        '<td class="url">' + esc(r.url) + "</td>" +
        "<td>" + (r.external
          ? '<span class="badge amber">外部</span>'
          : r.target
            ? '<span class="src">' + esc(r.target) + "</span>"
            : '<span class="badge red">未解析</span>') + "</td>" +
        "</tr>").join("") +
      "</tbody></table>";
  }
})();
