/* 流影 frontend — polling, no build step. */
(() => {
  "use strict";

  const QUALITY_LABELS = {
    best: "最佳",
    "1080p": "1080p",
    "720p": "720p",
    "480p": "480p",
    audio: "仅音频",
  };

  const AUTH_HELP =
    "抖音公开视频会自动用本地临时 Chrome 解析，不需要复制 cookies。只有 YouTube 年龄限制等场景才需要导入你自己的浏览器登录态；数据只存在这台电脑，不会上传。";

  const ICONS = {
    youtube: `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.6 12 3.6 12 3.6s-7.5 0-9.4.5A3 3 0 0 0 .5 6.2 31 31 0 0 0 0 12a31 31 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.5 9.4.5 9.4.5s7.5 0 9.4-.5a3 3 0 0 0 2.1-2.1A31 31 0 0 0 24 12a31 31 0 0 0-.5-5.8zM9.8 15.5v-7l6.3 3.5-6.3 3.5z"/></svg>`,
    bilibili: `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M5.4 2.6 7.2 4H3.8A1.8 1.8 0 0 0 2 5.8v12.4A1.8 1.8 0 0 0 3.8 20h16.4a1.8 1.8 0 0 0 1.8-1.8V5.8A1.8 1.8 0 0 0 20.2 4h-3.4l1.8-1.4-1.1-1.4L14.6 4H9.4L6.5 1.2 5.4 2.6zM7 9.2h2.2v5.2H7V9.2zm7.8 0H17v5.2h-2.2V9.2z"/></svg>`,
    douyin: `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M14.2 2v2.4a6.6 6.6 0 0 0 5.8 2.2v3a9.4 9.4 0 0 1-5.8-2v7.2A6.8 6.8 0 1 1 9.2 8.2v3.1a3.7 3.7 0 1 0 2.4 3.5V2h2.6z"/></svg>`,
    tiktok: `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M14.2 2v2.4a6.6 6.6 0 0 0 5.8 2.2v3a9.4 9.4 0 0 1-5.8-2v7.2A6.8 6.8 0 1 1 9.2 8.2v3.1a3.7 3.7 0 1 0 2.4 3.5V2h2.6z"/></svg>`,
    twitter: `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M17.2 3h3.1l-6.8 7.8L22 21h-6.2l-4.9-6.4L5.4 21H2.2l7.3-8.3L2 3h6.4l4.4 5.8L17.2 3zm-1.1 16.2h1.7L8 4.7H6.1l10 14.5z"/></svg>`,
  };

  const $ = (id) => document.getElementById(id);

  const els = {
    form: $("parse-form"),
    url: $("url"),
    parseBtn: $("parse-btn"),
    parseError: $("parse-error"),
    parseHint: $("parse-hint"),
    platformChip: $("platform-chip"),
    infoCard: $("info-card"),
    thumb: $("thumb"),
    infoPlatform: $("info-platform"),
    infoDuration: $("info-duration"),
    infoTitle: $("info-title"),
    infoUploader: $("info-uploader"),
    qualityList: $("quality-list"),
    downloadBtn: $("download-btn"),
    dlHint: $("dl-hint"),
    progressCard: $("progress-card"),
    progressStatus: $("progress-status"),
    progressPercent: $("progress-percent"),
    progressBar: $("progress-bar"),
    progressTitle: $("progress-title"),
    progressSpeed: $("progress-speed"),
    progressEta: $("progress-eta"),
    doneCard: $("done-card"),
    doneName: $("done-name"),
    doneSize: $("done-size"),
    donePath: $("done-path"),
    copyPath: $("copy-path"),
    saveFile: $("save-file"),
    copyToast: $("copy-toast"),
    jobList: $("job-list"),
    jobEmpty: $("job-empty"),
    refreshJobs: $("refresh-jobs"),
    ffmpegBanner: $("ffmpeg-banner"),
    authWrap: $("auth-wrap"),
    authToggle: $("auth-toggle"),
    authPanel: $("auth-panel"),
    authStatusLabel: $("auth-status-label"),
    authBrowser: $("auth-browser"),
    authSaveBrowser: $("auth-save-browser"),
    authFile: $("auth-file"),
    authUpload: $("auth-upload"),
    authFileName: $("auth-file-name"),
    authClear: $("auth-clear"),
    authMsg: $("auth-msg"),
    authHelpInline: $("auth-help-inline"),
    creatorCopy: $("creator-copy"),
    creatorToast: $("creator-toast"),
  };

  const state = {
    info: null,
    quality: "best",
    watching: new Set(),
    pollTimer: null,
    selectedJob: null,
    activeDownloadUrl: null,
    auth: { has_cookies: false, browser: null },
  };

  function detail(err) {
    if (!err) return "出了点问题，请稍后重试。";
    if (typeof err === "string") return err;
    if (err.detail) {
      if (typeof err.detail === "string") return err.detail;
      if (Array.isArray(err.detail) && err.detail[0]?.msg) return err.detail[0].msg;
    }
    return "出了点问题，请稍后重试。";
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (!res.ok) {
      throw new Error(detail(data) || `请求失败（${res.status}）`);
    }
    return data;
  }

  function thumbSrc(url) {
    if (!url) return "";
    return `/api/thumb?u=${encodeURIComponent(url)}`;
  }

  function isAgeOrLoginError(msg) {
    return /年龄|登录|cookies|cookie|确认年龄|登录态|抖音现在要|浏览器 cookies|fresh/i.test(msg || "");
  }

  function chipHtml(platform, label) {
    const icon = ICONS[platform] || ICONS.youtube;
    return `${icon}<span>${label}</span>`;
  }

  function detectLocal(url) {
    const u = (url || "").trim().toLowerCase();
    if (!u) return null;
    if (/youtu\.be|youtube\.com|youtube-nocookie/.test(u)) return { platform: "youtube", label: "YouTube" };
    if (/bilibili\.com|b23\.tv|b23\.wtf/.test(u)) return { platform: "bilibili", label: "哔哩哔哩" };
    if (/douyin\.com|iesdouyin\.com/.test(u)) return { platform: "douyin", label: "抖音" };
    if (/tiktok\.com/.test(u)) return { platform: "tiktok", label: "TikTok" };
    if (/(^|\.)twitter\.com|(^|\.)x\.com|\/\/x\.com/.test(u) || /https?:\/\/(www\.)?x\.com/.test(u) || u.includes("x.com/") || u.includes("twitter.com")) {
      return { platform: "twitter", label: "Twitter/X" };
    }
    return null;
  }

  function showPlatform(info) {
    if (!info) {
      els.platformChip.hidden = true;
      return;
    }
    els.platformChip.hidden = false;
    els.platformChip.innerHTML = chipHtml(info.platform, info.label);
  }

  function setParseError(msg) {
    if (!msg) {
      els.parseError.hidden = true;
      els.parseError.textContent = "";
      if (els.authHelpInline) els.authHelpInline.hidden = true;
      return;
    }
    els.parseError.hidden = false;
    els.parseError.textContent = msg;
    if (isAgeOrLoginError(msg)) {
      if (els.authHelpInline) {
        els.authHelpInline.hidden = false;
        els.authHelpInline.textContent = AUTH_HELP;
      }
      openAuthPanel();
    } else if (els.authHelpInline) {
      els.authHelpInline.hidden = true;
    }
  }

  function authStatusText(auth) {
    if (auth.has_cookies) return "已导入 cookies";
    const names = { chrome: "Chrome", chromium: "Chromium", firefox: "Firefox", edge: "Edge", brave: "Brave" };
    if (auth.browser) return `使用 ${names[auth.browser] || auth.browser}`;
    return "未导入（抖音自动）";
  }

  function renderAuth(auth) {
    state.auth = auth || { has_cookies: false, browser: null };
    els.authStatusLabel.textContent = authStatusText(state.auth);
    els.authToggle.classList.toggle("is-empty", !state.auth.has_cookies && !state.auth.browser);
    if (state.auth.browser && els.authBrowser) {
      els.authBrowser.value = state.auth.browser;
    }
  }

  function openAuthPanel() {
    if (!els.authPanel) return;
    els.authPanel.hidden = false;
    els.authToggle.setAttribute("aria-expanded", "true");
  }

  function closeAuthPanel() {
    if (!els.authPanel) return;
    els.authPanel.hidden = true;
    els.authToggle.setAttribute("aria-expanded", "false");
  }

  function toggleAuthPanel() {
    if (els.authPanel.hidden) openAuthPanel();
    else closeAuthPanel();
  }

  function setAuthMsg(msg, ok) {
    if (!els.authMsg) return;
    if (!msg) {
      els.authMsg.hidden = true;
      els.authMsg.textContent = "";
      els.authMsg.classList.remove("is-ok", "is-err");
      return;
    }
    els.authMsg.hidden = false;
    els.authMsg.textContent = msg;
    els.authMsg.classList.toggle("is-ok", !!ok);
    els.authMsg.classList.toggle("is-err", !ok);
  }

  async function refreshAuth() {
    try {
      const a = await api("/api/auth");
      renderAuth(a);
      return a;
    } catch {
      return null;
    }
  }

  function renderQualities(list) {
    els.qualityList.innerHTML = "";
    const available = list && list.length ? list : ["best", "audio"];
    if (!available.includes(state.quality)) state.quality = available[0];
    available.forEach((q) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chip" + (q === state.quality ? " selected" : "");
      btn.dataset.q = q;
      btn.textContent = QUALITY_LABELS[q] || q;
      btn.addEventListener("click", () => {
        state.quality = q;
        els.qualityList.querySelectorAll(".chip").forEach((c) => {
          c.classList.toggle("selected", c.dataset.q === q);
        });
        els.dlHint.textContent = q === "audio"
          ? "将尽量保存为 m4a 音频。"
          : (q === "best" ? "将选取最高画质，并在有 ffmpeg 时合并为 mp4。" : `将选取不超过 ${q} 的最高画质。`);
      });
      els.qualityList.appendChild(btn);
    });
    els.dlHint.textContent = state.quality === "audio"
      ? "将尽量保存为 m4a 音频。"
      : "文件会保存在本机 downloads 目录。";
  }

  function renderInfo(info) {
    state.info = info;
    state.quality = (info.qualities || []).includes("best") ? "best" : (info.qualities || ["best"])[0];
    els.infoCard.hidden = false;
    els.infoTitle.textContent = info.title || "未命名视频";
    els.infoUploader.textContent = info.uploader ? `UP / 作者 · ${info.uploader}` : "作者未知";
    els.infoDuration.textContent = info.duration_string ? `时长 ${info.duration_string}` : "";
    els.infoPlatform.innerHTML = chipHtml(info.platform, info.platform_label || info.platform);
    if (info.thumbnail) {
      els.thumb.src = thumbSrc(info.thumbnail);
      els.thumb.referrerPolicy = "no-referrer";
      els.thumb.hidden = false;
    } else {
      els.thumb.removeAttribute("src");
    }
    renderQualities(info.qualities);
    const qualityFieldset = els.qualityList.closest("fieldset");
    if (qualityFieldset) qualityFieldset.hidden = info.platform === "douyin";
    if (info.platform === "douyin") {
      els.dlHint.textContent = "抖音已自动开始下载最佳画质；文件会保存在本机 downloads 目录。";
    }
    els.doneCard.hidden = true;
  }

  function applyJobToProgress(job) {
    const active = ["queued", "extracting", "downloading", "merging"].includes(job.status);
    if (state.activeDownloadUrl && job.url === state.activeDownloadUrl) {
      els.downloadBtn.disabled = active;
      if (active) els.downloadBtn.textContent = "下载中";
      else els.downloadBtn.textContent = "开始下载";
      if (!active) state.activeDownloadUrl = null;
    }
    els.progressCard.hidden = !active && job.status !== "finished";
    if (job.status === "finished") {
      els.progressCard.hidden = true;
    }
    if (active) {
      els.progressStatus.textContent = job.status_label || job.status;
      els.progressPercent.textContent = `${Math.round(job.percent || 0)}%`;
      els.progressBar.style.width = `${Math.max(2, job.percent || 0)}%`;
      els.progressTitle.textContent = job.title || job.url;
      els.progressSpeed.textContent = job.speed ? `速度 ${job.speed}` : "";
      els.progressEta.textContent = job.eta ? `剩余 ${job.eta}` : (job.status === "queued" ? "等待前一个任务结束" : "");
    }
    if (job.status === "finished") {
      els.doneCard.hidden = false;
      els.doneName.textContent = job.filename || job.title || "已完成";
      els.doneSize.textContent = job.filesize_label ? `大小 ${job.filesize_label}` : "";
      els.donePath.textContent = job.output_path || "";
      els.saveFile.href = `/api/files/${job.id}`;
      els.saveFile.setAttribute("download", job.filename || "video");
      els.copyPath.onclick = async () => {
        const text = job.output_path || "";
        try {
          await navigator.clipboard.writeText(text);
          els.copyToast.hidden = false;
          els.copyToast.textContent = "已复制到剪贴板";
          setTimeout(() => { els.copyToast.hidden = true; }, 1800);
        } catch {
          els.copyToast.hidden = false;
          els.copyToast.textContent = "复制失败，请手动选择路径。";
        }
      };
    }
    if (job.status === "error") {
      els.progressCard.hidden = true;
      setParseError(job.error || "下载失败。");
    }
  }

  function renderJobs(jobs) {
    els.jobList.innerHTML = "";
    const has = jobs && jobs.length;
    els.jobEmpty.hidden = !!has;
    if (!has) return;
    jobs.forEach((job) => {
      const li = document.createElement("li");
      li.className = "job-item" + (state.selectedJob === job.id ? " active" : "");
      const title = job.title || job.url;
      const sub = job.status === "error"
        ? (job.error || "失败")
        : (job.status_label || job.status) + (job.percent && job.status !== "finished" ? ` · ${Math.round(job.percent)}%` : "")
          + (job.filesize_label && job.status === "finished" ? ` · ${job.filesize_label}` : "");
      li.innerHTML = `<span class="dot ${job.status}"></span><div><div class="job-title"></div><div class="job-sub"></div></div>`;
      li.querySelector(".job-title").textContent = title;
      li.querySelector(".job-sub").textContent = sub;
      li.addEventListener("click", () => {
        state.selectedJob = job.id;
        applyJobToProgress(job);
        renderJobs(jobs);
      });
      els.jobList.appendChild(li);
      if (["queued", "extracting", "downloading", "merging"].includes(job.status)) {
        state.watching.add(job.id);
      }
    });
  }

  async function refreshJobs() {
    try {
      const data = await api("/api/jobs");
      renderJobs(data.jobs || []);
      return data.jobs || [];
    } catch {
      return [];
    }
  }

  async function poll() {
    const ids = Array.from(state.watching);
    if (!ids.length) return;
    for (const id of ids) {
      try {
        const job = await api(`/api/jobs/${id}`);
        if (["finished", "error"].includes(job.status)) {
          state.watching.delete(id);
        }
        if (state.selectedJob === id || !state.selectedJob) {
          state.selectedJob = id;
          applyJobToProgress(job);
        }
      } catch {
        state.watching.delete(id);
      }
    }
    await refreshJobs();
  }

  function ensurePolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(() => {
      if (state.watching.size === 0) return;
      poll();
    }, 800);
  }

  els.url.addEventListener("input", () => {
    setParseError("");
    const detected = detectLocal(els.url.value);
    showPlatform(detected);
  });

  els.url.addEventListener("paste", () => {
    setTimeout(() => {
      const detected = detectLocal(els.url.value);
      showPlatform(detected);
    }, 0);
  });

  els.form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = els.url.value.trim();
    if (!url) {
      setParseError("请先粘贴一条视频链接。");
      return;
    }
    setParseError("");
    els.parseBtn.disabled = true;
    els.parseBtn.textContent = "解析中";
    try {
      const info = await api("/api/info", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      renderInfo(info);
      // Douyin's everyday flow is intentionally one click: once the local
      // browser has returned a public video stream, queue the default best
      // quality immediately.  Other platforms retain the explicit download
      // button so their quality choice can be reviewed first.
      if (info.platform === "douyin") {
        await queueDownload(info.url, "best");
      }
    } catch (err) {
      els.infoCard.hidden = true;
      setParseError(err.message || "解析失败。");
    } finally {
      els.parseBtn.disabled = false;
      els.parseBtn.textContent = "解析";
    }
  });

  async function queueDownload(url, quality) {
    if (!url) return;
    els.downloadBtn.disabled = true;
    setParseError("");
    try {
      const job = await api("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, quality: quality || state.quality }),
      });
      state.activeDownloadUrl = url;
      state.watching.add(job.id);
      state.selectedJob = job.id;
      applyJobToProgress(job);
      ensurePolling();
      await refreshJobs();
    } catch (err) {
      setParseError(err.message || "无法开始下载。");
    } finally {
      if (!state.activeDownloadUrl) els.downloadBtn.disabled = false;
    }
  }

  els.downloadBtn.addEventListener("click", async () => {
    const url = (state.info && state.info.url) || els.url.value.trim();
    await queueDownload(url, state.quality);
  });

  els.refreshJobs.addEventListener("click", () => refreshJobs());

  if (els.creatorCopy) {
    els.creatorCopy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText("liu18530971787");
        if (els.creatorToast) {
          els.creatorToast.hidden = false;
          setTimeout(() => { els.creatorToast.hidden = true; }, 1800);
        }
      } catch {
        if (els.creatorToast) {
          els.creatorToast.hidden = false;
          els.creatorToast.textContent = "请手动复制微信号";
        }
      }
    });
  }

  if (els.authToggle) {
    els.authToggle.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleAuthPanel();
    });
    document.addEventListener("click", (e) => {
      if (!els.authWrap.contains(e.target)) closeAuthPanel();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeAuthPanel();
    });
    els.authPanel.addEventListener("click", (e) => e.stopPropagation());
  }

  if (els.authFile) {
    els.authFile.addEventListener("change", () => {
      const f = els.authFile.files && els.authFile.files[0];
      els.authFileName.textContent = f ? f.name : "尚未选择文件";
    });
  }

  if (els.authUpload) {
    els.authUpload.addEventListener("click", async () => {
      const f = els.authFile.files && els.authFile.files[0];
      if (!f) {
        setAuthMsg("请先选择 cookies.txt 文件。", false);
        return;
      }
      const fd = new FormData();
      fd.append("file", f, f.name || "cookies.txt");
      try {
        const res = await fetch("/api/cookies", { method: "POST", body: fd });
        const data = await res.json().catch(() => null);
        if (!res.ok) throw new Error(detail(data) || "导入失败。");
        setAuthMsg("已导入 cookies。只存在这台电脑。", true);
        await refreshAuth();
      } catch (err) {
        setAuthMsg(err.message || "导入失败。", false);
      }
    });
  }

  if (els.authSaveBrowser) {
    els.authSaveBrowser.addEventListener("click", async () => {
      const browser = els.authBrowser.value;
      try {
        const data = await api("/api/auth/browser", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ browser }),
        });
        renderAuth(data);
        setAuthMsg("已能读取该浏览器。可以去解析了。", true);
      } catch (err) {
        setAuthMsg(err.message || "无法保存浏览器。", false);
      }
    });
  }

  if (els.authClear) {
    els.authClear.addEventListener("click", async () => {
      try {
        await api("/api/cookies", { method: "DELETE" });
        await api("/api/auth/browser", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ browser: null }),
        });
        if (els.authFile) els.authFile.value = "";
        if (els.authFileName) els.authFileName.textContent = "尚未选择文件";
        setAuthMsg("已清除本机登录态。", true);
        await refreshAuth();
      } catch (err) {
        setAuthMsg(err.message || "清除失败。", false);
      }
    });
  }

  async function boot() {
    try {
      const h = await api("/api/health");
      if (h && h.ffmpeg === false) {
        els.ffmpegBanner.hidden = false;
      }
    } catch {
      // health failed — still allow UI
    }
    await refreshAuth();
    await refreshJobs();
    const jobs = await refreshJobs();
    jobs.forEach((j) => {
      if (["queued", "extracting", "downloading", "merging"].includes(j.status)) {
        state.watching.add(j.id);
      }
    });
    ensurePolling();
  }

  boot();
})();
