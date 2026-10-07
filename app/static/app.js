(function () {
  "use strict";

  var fileInput = document.getElementById("file");
  var fileName = document.getElementById("file-name");
  var analyzeBtn = document.getElementById("analyze");
  var drop = document.getElementById("drop");
  var resultCard = document.getElementById("result");
  var downloadCard = document.getElementById("download-card");
  var picked = null;

  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(2) + " MB";
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function setFile(f) {
    picked = f || null;
    fileName.textContent = f ? f.name + "（" + fmtBytes(f.size) + "）" : "";
    analyzeBtn.disabled = !f;
  }

  fileInput.addEventListener("change", function () {
    setFile(fileInput.files && fileInput.files[0]);
  });
  drop.addEventListener("dragover", function (e) {
    e.preventDefault();
    drop.classList.add("over");
  });
  drop.addEventListener("dragleave", function () {
    drop.classList.remove("over");
  });
  drop.addEventListener("drop", function (e) {
    e.preventDefault();
    drop.classList.remove("over");
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      setFile(e.dataTransfer.files[0]);
    }
  });

  function issueTable(list) {
    if (!list.length) return '<p class="none">无</p>';
    var rows = list.map(function (i) {
      return "<tr><td>" + esc(i.file || "—") + "</td><td>" + esc(i.reason) +
        "</td><td>" + esc(i.source || "—") + "</td><td class=\"url\">" +
        esc(i.url == null ? "—" : i.url) + "</td></tr>";
    });
    return '<table><thead><tr><th>文件</th><th>原因</th><th>来源</th><th>原 URL</th></tr></thead><tbody>' +
      rows.join("") + "</tbody></table>";
  }

  function stat(label, value) {
    return '<div class="stat"><b>' + esc(value) + "</b><span>" + esc(label) + "</span></div>";
  }

  function render(rep) {
    resultCard.classList.remove("hidden");
    var s = rep.stats || {};
    document.getElementById("stats").innerHTML =
      stat("压缩包大小", fmtBytes(s.zip_bytes || 0)) +
      stat("HTML/CSS 文件", s.text_files == null ? "—" : s.text_files) +
      stat("图片", s.image_files == null ? "—" : s.image_files) +
      stat("入口可达文件", s.reachable_files == null ? "—" : s.reachable_files) +
      stat("封包大小", s.export_bytes ? fmtBytes(s.export_bytes) : "—");

    var banner = document.getElementById("banner");
    if (rep.rescan_ok === true) {
      banner.className = "banner ok";
      banner.textContent = "产物复检通过：内链与资源均可从入口离线到达。";
    } else if (rep.errors.length) {
      banner.className = "banner bad";
      banner.textContent = "存在 " + rep.errors.length + " 个阻断问题，已禁止下载。";
    } else {
      banner.className = "banner bad";
      banner.textContent = "产物复检未通过，已禁止下载。";
    }

    document.getElementById("errors").innerHTML = issueTable(rep.errors || []);
    document.getElementById("infos").innerHTML = issueTable(rep.infos || []);

    if (rep.export_ready) {
      downloadCard.classList.remove("hidden");
      document.getElementById("download").href = "/api/export/" + rep.token;
      var ul = document.getElementById("exported");
      ul.innerHTML = (rep.exported || []).map(function (p) {
        return "<li><code>" + esc(p) + "</code></li>";
      }).join("");
    } else {
      downloadCard.classList.add("hidden");
    }
    resultCard.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  analyzeBtn.addEventListener("click", function () {
    if (!picked) return;
    var fd = new FormData();
    fd.append("file", picked);
    analyzeBtn.disabled = true;
    analyzeBtn.textContent = "分析中…";
    fetch("/api/analyze", { method: "POST", body: fd })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(render)
      .catch(function (e) {
        alert("分析请求失败：" + e.message);
      })
      .finally(function () {
        analyzeBtn.disabled = false;
        analyzeBtn.textContent = "开始分析";
      });
  });
})();
