(() => {
  "use strict";

  const form = document.getElementById("preview-form");
  if (!form) return;

  const statusEl = document.getElementById("preview-status");
  const confirmStatus = document.getElementById("confirm-status");
  const root = document.getElementById("preview-root");
  const summary = document.getElementById("preview-summary");
  const stack = document.getElementById("preview-downstreams");
  const confirmButton = document.getElementById("confirm-button");
  const discardButton = document.getElementById("discard-button");
  const tpl = document.getElementById("downstream-template");

  let currentPreview = null;

  const setStatus = (el, text, kind) => {
    el.textContent = text || "";
    el.className = "status" + (kind ? " " + kind : "");
  };

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  const labelFor = (name) => (name === "settlement" ? "结算系统" : "客流系统");

  async function errorText(response) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      /* ignore */
    }
    if (payload && payload.message) {
      let text = payload.message;
      if (payload.details && payload.details.rows && payload.details.rows.length) {
        const rows = payload.details.rows.slice(0, 5).map((r) => {
          const line = r.line || r.row || "?";
          return `第 ${line} 行：${r.reason || "格式错误"}`;
        });
        text += "\n" + rows.join("\n");
      }
      return { text, payload };
    }
    return { text: `请求失败（HTTP ${response.status}）`, payload };
  }

  function renderHeader(table, columns, types, badColumns) {
    const thead = table.querySelector("thead");
    const tr = el("tr");
    tr.appendChild(el("th", "rownum", "#"));
    columns.forEach((name, i) => {
      const th = el("th", badColumns && badColumns.has(name) ? "bad-col" : "");
      th.appendChild(el("span", "", name));
      th.appendChild(el("span", "tag " + types[i], types[i]));
      tr.appendChild(th);
    });
    thead.replaceChildren(tr);
  }

  function renderTable(container, ds) {
    const table = container.querySelector(".preview-table");
    const tbody = container.querySelector(".preview-table tbody");
    renderHeader(table, ds.columns, ds.column_types, new Set());
    tbody.replaceChildren();
    ds.preview.forEach((row) => {
      const tr = el("tr", row.bad_columns && row.bad_columns.length ? "row-bad" : "");
      tr.appendChild(el("td", "rownum", row.row));
      row.values.forEach((value, i) => {
        const bad = row.bad_columns && row.bad_columns.includes(ds.columns[i]);
        tr.appendChild(el("td", bad ? "cell-bad" : "", value));
      });
      tbody.appendChild(tr);
    });
    if (!ds.preview.length) {
      const tr = el("tr");
      const td = el("td", "empty", "没有数据行");
      td.colSpan = ds.columns.length + 1;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  function renderIssues(container, ds) {
    const box = container.querySelector(".ds-issues");
    const tbody = box.querySelector(".issue-table tbody");
    tbody.replaceChildren();
    if (!ds.issues.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    ds.issues.forEach((issue) => {
      const tr = el("tr");
      tr.appendChild(el("td", "", issue.row));
      tr.appendChild(el("td", "", issue.line));
      tr.appendChild(el("td", "", issue.column));
      tr.appendChild(el("td", "mono", issue.value));
      tr.appendChild(el("td", "", issue.reason));
      tbody.appendChild(tr);
    });
    const note = box.querySelector(".issue-note");
    const total = ds.bad_row_count;
    note.textContent = ds.issues_truncated
      ? `仅显示前 ${ds.issues.length} 条问题；共有 ${total} 行不兼容。整批不可发布。`
      : `共有 ${total} 行不兼容，整批不可发布。`;
  }

  function renderDownstream(name, ds) {
    const node = tpl.content.firstElementChild.cloneNode(true);
    node.dataset.downstream = name;
    node.querySelector(".ds-label").textContent = labelFor(name);
    node.querySelector(".ds-name").textContent = name;
    const badge = node.querySelector(".ds-badge");
    const ok = ds.status === "ok";
    badge.textContent = ok ? "可转换" : "不兼容";
    badge.classList.add(ok ? "ok" : "bad");
    node.classList.add(ok ? "is-ok" : "is-bad");

    const contract = currentPreview.contracts[name];
    const cols = contract.columns
      .map((c) => `${c.name}(${c.type === "integer" ? "整数" : "文本"})`)
      .join(" → ");
    node.querySelector(".ds-contract").textContent = `契约 v${contract.version}：${cols}`;

    const missing = node.querySelector(".ds-missing");
    if (ds.missing_columns && ds.missing_columns.length) {
      missing.hidden = false;
      missing.textContent = `源 CSV 缺少必需列：${ds.missing_columns.join("、")}`;
    }

    renderTable(node, ds);
    node.querySelector(".ds-rowinfo").textContent =
      `（${ds.row_count} 行，前 ${ds.preview.length} 行）`;
    renderIssues(node, ds);
    return node;
  }

  function renderPreview(preview) {
    currentPreview = preview;
    stack.replaceChildren();
    summary.replaceChildren();

    const card = (label, value, cls) => {
      const box = el("div", "summary-card" + (cls ? " " + cls : ""));
      box.appendChild(el("div", "summary-label", label));
      box.appendChild(el("div", "summary-value", value));
      summary.appendChild(box);
    };
    card("业务日期", preview.day);
    card("基于修订", "r" + preview.base_rev);
    card("原始文件", preview.original.name);
    card("数据行数", preview.original.row_count);
    card("整体结果", preview.convertible ? "可发布" : "不可发布",
      preview.convertible ? "ok" : "bad");

    ["settlement", "ridership"].forEach((name) => {
      stack.appendChild(renderDownstream(name, preview.downstreams[name]));
    });

    root.hidden = false;
    confirmButton.disabled = !preview.convertible;
    confirmButton.title = preview.convertible
      ? ""
      : "存在不兼容的列或行，修正前不能发布";
    setStatus(confirmStatus, preview.convertible
      ? "请核对预览后确认发布；确认后该日修订号 +1。"
      : "存在不兼容内容，整批不可发布。", preview.convertible ? "" : "bad");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setStatus(statusEl, "上传并校验中…", "busy");
    root.hidden = true;
    const data = new FormData(form);
    try {
      const response = await fetch("/api/previews", { method: "POST", body: data });
      if (!response.ok) {
        const { text } = await errorText(response);
        setStatus(statusEl, text, "bad");
        return;
      }
      const preview = await response.json();
      setStatus(statusEl, "", "");
      renderPreview(preview);
    } catch (err) {
      setStatus(statusEl, "网络错误：" + err.message, "bad");
    }
  });

  confirmButton.addEventListener("click", async () => {
    if (!currentPreview) return;
    confirmButton.disabled = true;
    setStatus(confirmStatus, "发布中…", "busy");
    try {
      const response = await fetch(
        `/api/previews/${encodeURIComponent(currentPreview.preview_id)}/confirm`,
        { method: "POST", headers: { Accept: "application/json" } }
      );
      if (!response.ok) {
        const { text } = await errorText(response);
        setStatus(confirmStatus, text, "bad");
        if (response.status === 409 || response.status === 404) {
          root.hidden = true;
          currentPreview = null;
          setStatus(statusEl, "预览已失效，请重新上传并预览。", "bad");
        } else {
          confirmButton.disabled = false;
        }
        return;
      }
      const published = await response.json();
      setStatus(confirmStatus, "已发布，正在跳转…", "ok");
      window.location.href = "/days/" + encodeURIComponent(published.day);
    } catch (err) {
      setStatus(confirmStatus, "网络错误：" + err.message, "bad");
      confirmButton.disabled = false;
    }
  });

  discardButton.addEventListener("click", async () => {
    if (!currentPreview) return;
    const id = currentPreview.preview_id;
    try {
      await fetch(`/api/previews/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch (_) {
      /* best effort */
    }
    currentPreview = null;
    root.hidden = true;
    setStatus(statusEl, "已放弃预览。", "");
    form.reset();
  });
})();
