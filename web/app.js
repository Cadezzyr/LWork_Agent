/* ==========================================================================
   前端逻辑：对接后端 /api/v1 的登录、会话、流式对话与健康检查接口
   - 登录态由 auth.js 管理（Bearer token + 自动刷新）
   - 历史会话来自服务端数据库（按登录用户隔离），不再依赖 localStorage
   - 回复走 SSE（POST /chat/stream），因此用 fetch + ReadableStream 而不是 EventSource
   ========================================================================== */
(function () {
  "use strict";

  var API = "/api/v1";
  var MAX_INPUT = 8000;
  var TITLE_LIMIT = 26;

  var dom = {
    // 登录
    authScreen: document.getElementById("auth-screen"),
    authForm: document.getElementById("auth-form"),
    authTabs: [document.getElementById("tab-login"), document.getElementById("tab-register")],
    authIdentifier: document.getElementById("auth-identifier"),
    authIdentifierLabel: document.getElementById("auth-identifier-label"),
    authEmail: document.getElementById("auth-email"),
    authEmailField: document.getElementById("auth-email-field"),
    authPassword: document.getElementById("auth-password"),
    authError: document.getElementById("auth-error"),
    authSubmit: document.getElementById("auth-submit"),
    authHint: document.getElementById("auth-hint"),
    // 侧栏
    sidebar: document.getElementById("sidebar"),
    sessionList: document.getElementById("session-list"),
    sessionSearch: document.getElementById("session-search"),
    newChatBtn: document.getElementById("new-chat"),
    clearBtn: document.getElementById("clear-local"),
    menuBtn: document.getElementById("toggle-sidebar"),
    userName: document.getElementById("user-name"),
    userAvatar: document.getElementById("user-avatar"),
    logoutBtn: document.getElementById("logout"),
    // 主区域
    messages: document.getElementById("messages"),
    emptyState: document.getElementById("empty-state"),
    chatLog: document.getElementById("chat-log"),
    title: document.getElementById("chat-title"),
    input: document.getElementById("input"),
    sendBtn: document.getElementById("send"),
    banner: document.getElementById("banner"),
    modelName: document.getElementById("model-name"),
    statusText: document.getElementById("status-text"),
    statusDot: document.getElementById("status-dot"),
    toolCount: document.getElementById("tool-count"),
    workspaceName: document.getElementById("workspace-name"),
    openWorkspaceBtn: document.getElementById("open-workspace"),
    wsModal: document.getElementById("ws-modal"),
    wsRoot: document.getElementById("ws-root"),
    wsWrites: document.getElementById("ws-writes"),
    wsValidate: document.getElementById("ws-validate"),
    wsDefault: document.getElementById("ws-default"),
    wsSave: document.getElementById("ws-save"),
    wsStatus: document.getElementById("ws-status"),
    wsTree: document.getElementById("ws-tree"),
    wsTreeBox: document.getElementById("ws-tree-box"),
    wsCurrent: document.getElementById("ws-current"),
  };

  var state = {
    sessionId: null,
    sessions: [],
    streaming: false,
    controller: null,
    healthy: false,
    stickBottom: true,
    workspace: null,
    user: null,
    authMode: "login", // login | register
    allowRegister: true,
    searchTimer: null,
  };

  /* ---------------------------------------------------------------- 工具函数 */

  function formatTime(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }

  function truncate(text, size) {
    var value = String(text || "").replace(/\s+/g, " ").trim();
    return value.length > size ? value.slice(0, size) + "…" : value;
  }

  function showBanner(message) {
    dom.banner.textContent = message;
    dom.banner.classList.add("show");
  }

  function hideBanner() {
    dom.banner.classList.remove("show");
    dom.banner.textContent = "";
  }

  /* ------------------------------------------------------------------ 登录 UI */

  function showAuthScreen(mode) {
    setAuthMode(mode || "login");
    dom.authScreen.hidden = false;
    dom.input.blur();
    setTimeout(function () {
      dom.authIdentifier.focus();
    }, 30);
  }

  function hideAuthScreen() {
    dom.authScreen.hidden = true;
  }

  function setAuthError(message) {
    dom.authError.textContent = message || "";
    dom.authError.classList.toggle("show", !!message);
  }

  function setAuthMode(mode) {
    state.authMode = mode;
    dom.authTabs.forEach(function (tab, index) {
      var active = (mode === "login" && index === 0) || (mode === "register" && index === 1);
      tab.classList.toggle("active", active);
    });
    var register = mode === "register";
    dom.authIdentifierLabel.textContent = register ? "用户名（3~32 位字母/数字/_.-）" : "用户名或邮箱";
    dom.authEmailField.hidden = !register;
    dom.authSubmit.textContent = register ? "注册并登录" : "登录";
    dom.authPassword.autocomplete = register ? "new-password" : "current-password";
    dom.authHint.textContent = register
      ? "注册后会自动登录；密码至少 8 位，且不能是常见弱口令。"
      : "登录后可查看自己的历史对话；忘记密码请联系管理员重置。";
    if (register && !state.allowRegister) {
      setAuthError("当前服务未开放自助注册");
    } else {
      setAuthError("");
    }
  }

  async function submitAuth(event) {
    event.preventDefault();
    var identifier = dom.authIdentifier.value.trim();
    var password = dom.authPassword.value;
    if (!identifier || !password) {
      setAuthError("请填写账号与密码");
      return;
    }
    dom.authSubmit.disabled = true;
    setAuthError("");
    try {
      if (state.authMode === "register") {
        await window.Auth.register(identifier, password, dom.authEmail.value.trim(), null);
      } else {
        await window.Auth.login(identifier, password);
      }
      dom.authPassword.value = "";
      hideAuthScreen();
      await afterLogin();
    } catch (error) {
      setAuthError(error.message || "登录失败");
    } finally {
      dom.authSubmit.disabled = false;
    }
  }

  async function afterLogin() {
    try {
      state.user = await window.Auth.fetchMe();
    } catch (error) {
      state.user = window.Auth.getUser();
    }
    renderUser();
    await Promise.all([loadSystemInfo(), loadWorkspace()]);
    await loadSessions();
    if (state.sessions.length) {
      await openSession(state.sessions[0].session_id);
    } else {
      startNewChat();
    }
  }

  function renderUser() {
    var user = state.user;
    if (!user) {
      dom.userName.textContent = "未登录";
      dom.userAvatar.textContent = "?";
      return;
    }
    var display = user.display_name || user.username;
    dom.userName.textContent = display + (user.role === "admin" ? "（管理员）" : "");
    dom.userName.title = user.email ? display + " <" + user.email + ">" : display;
    dom.userAvatar.textContent = String(display).slice(0, 1).toUpperCase();
  }

  async function logout() {
    if (state.streaming && state.controller) state.controller.abort();
    await window.Auth.logout();
    state.user = null;
    state.sessions = [];
    state.sessionId = null;
    dom.chatLog.innerHTML = "";
    renderUser();
    renderSessionList();
    setTitle("新对话");
    showEmptyState();
    showAuthScreen("login");
  }

  /* ---------------------------------------------------------------- 会话列表 */

  async function loadSessions(keyword) {
    var query = "/sessions?limit=100";
    if (keyword) query += "&keyword=" + encodeURIComponent(keyword);
    try {
      var data = await window.Auth.apiFetch(query);
      state.sessions = data.sessions || [];
      renderSessionList();
    } catch (error) {
      if (error.status === 401) return;
      showBanner("加载历史对话失败：" + error.message);
    }
  }

  function renderSessionList() {
    dom.sessionList.innerHTML = "";
    if (!state.sessions.length) {
      var hint = document.createElement("div");
      hint.className = "sidebar__empty";
      hint.textContent = "还没有对话记录";
      dom.sessionList.appendChild(hint);
      return;
    }
    state.sessions.forEach(function (item) {
      var row = document.createElement("div");
      row.className = "session-item" + (item.session_id === state.sessionId ? " active" : "");
      row.dataset.id = item.session_id;

      var title = document.createElement("span");
      title.className = "session-item__title";
      title.textContent = item.title || "新对话";
      title.title = (item.title || "新对话") + "\n" + (item.message_count || 0) + " 条消息";

      var meta = document.createElement("span");
      meta.className = "session-item__meta";
      meta.textContent = formatTime(item.last_active_at);

      var del = document.createElement("button");
      del.className = "session-item__del";
      del.type = "button";
      del.textContent = "✕";
      del.title = "删除会话";
      del.addEventListener("click", function (event) {
        event.stopPropagation();
        deleteSession(item.session_id);
      });

      row.appendChild(title);
      row.appendChild(meta);
      row.appendChild(del);
      row.addEventListener("click", function () {
        openSession(item.session_id);
      });
      dom.sessionList.appendChild(row);
    });
  }

  function setTitle(text) {
    dom.title.textContent = text || "新对话";
    document.title = text ? text + " · LangChain Agent" : "LangChain Agent";
  }

  function titleOf(id) {
    var found = state.sessions.find(function (item) {
      return item.session_id === id;
    });
    return found ? found.title : "新对话";
  }

  function upsertSession(id, title) {
    if (!id) return;
    var existing = state.sessions.find(function (item) {
      return item.session_id === id;
    });
    if (existing) {
      if (title) existing.title = title;
    } else {
      state.sessions.unshift({
        session_id: id,
        title: title || "新对话",
        last_active_at: new Date().toISOString(),
        message_count: 0,
        turns: 0,
      });
    }
    renderSessionList();
  }

  /* ------------------------------------------------------------------ 消息渲染 */

  function scrollToBottom(force) {
    if (!force && !state.stickBottom) return;
    dom.messages.scrollTop = dom.messages.scrollHeight;
  }

  function hideEmptyState() {
    if (dom.emptyState) dom.emptyState.hidden = true;
  }

  function showEmptyState() {
    if (dom.emptyState) dom.emptyState.hidden = false;
  }

  function appendUserMessage(text) {
    hideEmptyState();
    var wrap = document.createElement("div");
    wrap.className = "msg msg--user";
    var body = document.createElement("div");
    body.className = "msg__body";
    body.textContent = text;
    wrap.appendChild(body);
    dom.chatLog.appendChild(wrap);
    scrollToBottom(true);
  }

  /**
   * 创建一条助手消息容器。
   * 返回 { root, tools, content, meta } 供流式过程增量更新。
   */
  function createAssistantMessage() {
    hideEmptyState();
    var root = document.createElement("div");
    root.className = "msg msg--assistant";

    var avatar = document.createElement("div");
    avatar.className = "msg__avatar";
    avatar.textContent = "AI";

    var body = document.createElement("div");
    body.className = "msg__body";

    var tools = document.createElement("div");
    tools.className = "tools";
    tools.hidden = true;

    var approvals = document.createElement("div");
    approvals.className = "approvals";
    approvals.hidden = true;

    var content = document.createElement("div");
    content.className = "msg__content";

    var thinking = document.createElement("div");
    thinking.className = "thinking";
    thinking.innerHTML = "<i></i><i></i><i></i><span>正在思考…</span>";
    content.appendChild(thinking);

    var meta = document.createElement("div");
    meta.className = "msg__meta";
    meta.hidden = true;

    body.appendChild(tools);
    body.appendChild(approvals);
    body.appendChild(content);
    body.appendChild(meta);
    root.appendChild(avatar);
    root.appendChild(body);
    dom.chatLog.appendChild(root);
    scrollToBottom(true);

    return {
      root: root,
      tools: tools,
      approvals: approvals,
      content: content,
      meta: meta,
      thinking: thinking,
      partial: "",
    };
  }

  function toolCard(toolsBox, call) {
    // 人工确认后恢复执行时，同一个 tool_call 会在 updates 里再出现一次，复用已有卡片
    if (call.id) {
      var cards = toolsBox.querySelectorAll(".tool");
      for (var i = 0; i < cards.length; i++) {
        if (cards[i].dataset.callId === call.id) {
          toolsBox.hidden = false;
          return {
            card: cards[i],
            label: cards[i].querySelector(".tool__state"),
            result: cards[i].querySelector(".tool__result"),
          };
        }
      }
    }

    var card = document.createElement("details");
    card.className = "tool running";
    card.dataset.callId = call.id || call.name;

    var summary = document.createElement("summary");
    var name = document.createElement("span");
    name.className = "tool__name";
    name.textContent = call.name || "tool";
    var args = document.createElement("span");
    args.className = "tool__args";
    args.textContent = JSON.stringify(call.args || {});
    var label = document.createElement("span");
    label.className = "tool__state";
    label.textContent = "调用中…";

    summary.appendChild(name);
    summary.appendChild(args);
    summary.appendChild(label);

    var result = document.createElement("pre");
    result.className = "tool__result";
    result.textContent = "等待工具返回…";

    card.appendChild(summary);
    card.appendChild(result);
    toolsBox.appendChild(card);
    toolsBox.hidden = false;
    scrollToBottom();

    return { card: card, label: label, result: result };
  }

  function fillToolResult(toolsBox, data) {
    var cards = toolsBox.querySelectorAll(".tool");
    var target = null;
    for (var i = 0; i < cards.length; i++) {
      var card = cards[i];
      if (card.dataset.callId === data.id || (!data.id && card.classList.contains("running"))) {
        target = card;
        break;
      }
    }
    if (!target) target = cards[cards.length - 1];
    if (!target) return;

    target.classList.remove("running");
    if (data.status === "error") target.classList.add("error");
    var label = target.querySelector(".tool__state");
    if (label) label.textContent = data.status === "error" ? "失败" : "已完成";
    var result = target.querySelector(".tool__result");
    if (result) result.textContent = data.content || "（无返回内容）";
    scrollToBottom();
  }

  function setContentMarkdown(box, text, streaming) {
    var html = window.renderMarkdown(text);
    if (streaming) html += '<span class="cursor"></span>';
    box.innerHTML = html || (streaming ? "" : "<p>（空回复）</p>");
  }

  function appendMeta(box, text) {
    box.hidden = false;
    box.textContent = text;
  }

  /* -------------------------------------------------------------- 人工确认（HITL） */

  var DECISION_LABELS = {
    approve: "已批准执行",
    edit: "已修改参数",
    reject: "已拒绝执行",
  };

  function allowedDecisions(data, index) {
    var configs = data.review_configs || [];
    var action = (data.action_requests || [])[index] || {};
    var config = configs[index] || {};
    var allowed = config.allowed_decisions;
    if (!allowed || !allowed.length) {
      for (var i = 0; i < configs.length; i++) {
        if (configs[i].action_name === action.name) {
          allowed = configs[i].allowed_decisions;
          break;
        }
      }
    }
    return allowed && allowed.length ? allowed : ["approve", "edit", "reject"];
  }

  function smallButton(text, className, onClick) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = text;
    button.addEventListener("click", onClick);
    return button;
  }

  /**
   * 渲染一条「需要人工确认」的卡片。
   * 每个待执行工具一行，用户点 approve / edit / reject 后，
   * 全部行都有决策时把 decisions 提交给 POST /chat/resume。
   */
  function renderApproval(node, data) {
    var requests = data.action_requests || [];
    if (!requests.length) return;

    hideEmptyState();
    node.approvals.hidden = false;

    var card = document.createElement("div");
    card.className = "approval";

    var head = document.createElement("div");
    head.className = "approval__head";
    head.innerHTML =
      "<b>需要你确认</b><span>以下是 Agent 准备执行的工具调用，确认后才会真正运行</span>";
    card.appendChild(head);

    var decisions = new Array(requests.length);
    var rows = [];
    var locked = false;

    function submitIfReady() {
      if (locked) return;
      for (var i = 0; i < decisions.length; i++) {
        if (!decisions[i]) return;
      }
      locked = true;
      card.classList.add("approval--submitted");
      setCardNote(card, "正在按你的决定继续…");
      submitDecisions(node, card, decisions.slice());
    }

    function decide(index, decision) {
      if (locked) return;
      decisions[index] = decision;
      var row = rows[index];
      row.classList.add("approval__item--done");
      row.querySelector(".approval__actions").hidden = true;
      row.querySelector(".approval__editor").hidden = true;
      row.querySelector(".approval__state").textContent = DECISION_LABELS[decision.type] || "已处理";
      scrollToBottom();
      submitIfReady();
    }

    requests.forEach(function (action, index) {
      var allowed = allowedDecisions(data, index);

      var row = document.createElement("div");
      row.className = "approval__item";

      var name = document.createElement("div");
      name.className = "approval__tool";
      name.textContent = action.name || "tool";

      var args = document.createElement("pre");
      args.className = "approval__args";
      args.textContent = JSON.stringify(action.args || {}, null, 2);

      var stateLabel = document.createElement("div");
      stateLabel.className = "approval__state";

      var actions = document.createElement("div");
      actions.className = "approval__actions";

      var approveBtn = smallButton("批准执行", "primary-btn", function () {
        decide(index, { type: "approve" });
      });
      var editBtn = smallButton("修改参数", "ghost-btn", function () {
        editor.hidden = false;
        actions.hidden = true;
        argsInput.focus();
      });
      var rejectBtn = smallButton("拒绝", "ghost-btn approval__reject", function () {
        decide(index, { type: "reject" });
      });
      approveBtn.disabled = allowed.indexOf("approve") === -1;
      editBtn.disabled = allowed.indexOf("edit") === -1;
      rejectBtn.disabled = allowed.indexOf("reject") === -1;

      actions.appendChild(approveBtn);
      actions.appendChild(editBtn);
      actions.appendChild(rejectBtn);

      // 修改参数：name 可换工具，args 用 JSON 编辑
      var editor = document.createElement("div");
      editor.className = "approval__editor";
      editor.hidden = true;

      var nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.className = "approval__name";
      nameInput.value = action.name || "";
      nameInput.title = "工具名";

      var argsInput = document.createElement("textarea");
      argsInput.className = "approval__textarea";
      argsInput.rows = 4;
      argsInput.spellcheck = false;
      argsInput.value = JSON.stringify(action.args || {}, null, 2);

      var hint = document.createElement("div");
      hint.className = "approval__hint";
      hint.textContent = "按 JSON 修改入参，提交后会用修改后的内容执行";

      var editorActions = document.createElement("div");
      editorActions.className = "approval__actions";
      var saveEdit = smallButton("确认修改", "primary-btn", function () {
        var parsed;
        try {
          parsed = JSON.parse(argsInput.value || "{}");
        } catch (error) {
          hint.textContent = "⚠ 入参不是合法的 JSON：" + error.message;
          hint.classList.add("approval__hint--err");
          return;
        }
        if (!parsed || typeof parsed !== "object" || Object.prototype.toString.call(parsed) === "[object Array]") {
          hint.textContent = "⚠ 入参必须是一个 JSON 对象，例如 {\"command\": \"echo hi\"}";
          hint.classList.add("approval__hint--err");
          return;
        }
        hint.classList.remove("approval__hint--err");
        decide(index, {
          type: "edit",
          edited_action: { name: nameInput.value.trim() || action.name, args: parsed },
        });
      });
      var cancelEdit = smallButton("取消", "ghost-btn", function () {
        editor.hidden = true;
        actions.hidden = false;
      });
      editorActions.appendChild(saveEdit);
      editorActions.appendChild(cancelEdit);

      editor.appendChild(nameInput);
      editor.appendChild(argsInput);
      editor.appendChild(hint);
      editor.appendChild(editorActions);

      row.appendChild(name);
      row.appendChild(args);
      row.appendChild(actions);
      row.appendChild(editor);
      row.appendChild(stateLabel);
      card.appendChild(row);
      rows.push(row);
    });

    var note = document.createElement("div");
    note.className = "approval__note";
    card.appendChild(note);

    node.approvals.appendChild(card);
    scrollToBottom(true);
  }

  function setCardNote(card, text, isError) {
    var note = card.querySelector(".approval__note");
    if (!note) return;
    note.textContent = text || "";
    note.classList.toggle("approval__note--err", !!isError);
  }

  /**
   * 提交决策：恢复被 HumanInTheLoopMiddleware 暂停的那一轮。
   * 恢复后的 token / 工具事件继续渲染在同一条助手消息里。
   */
  async function submitDecisions(node, card, decisions) {
    if (!state.sessionId) {
      setCardNote(card, "会话已失效，请重新发起提问", true);
      return;
    }
    state.controller = new AbortController();
    setStreaming(true);
    try {
      var response = await requestStream("/chat/resume", {
        session_id: state.sessionId,
        decisions: decisions,
      });
      var outcome = await consumeStream(response, node, {
        progress: { aggregated: node.partial || "" },
      });
      node.partial = outcome.aggregated;
      setCardNote(card, "已按你的决定继续");
    } catch (error) {
      if (error.name === "AbortError") {
        setCardNote(card, "已断开接收，工具可能已执行，可刷新页面查看结果", true);
      } else {
        card.classList.add("approval--failed");
        setCardNote(card, "提交失败：" + error.message, true);
        showBanner("提交决策失败：" + error.message);
      }
    } finally {
      state.controller = null;
      setStreaming(false);
      dom.input.focus();
    }
  }

  /** 用户发了新消息，上一轮还没确认的卡片就作废了（后端也会丢弃该中断）。 */
  function abandonPendingApprovals() {
    var cards = dom.chatLog.querySelectorAll(".approval:not(.approval--submitted)");
    for (var i = 0; i < cards.length; i++) {
      cards[i].classList.add("approval--ignored");
      setCardNote(cards[i], "已忽略（你发送了新消息）");
    }
  }

  /* ---------------------------------------------------------------- 工作区设置 */

  function shortPath(path, size) {
    var value = String(path || "");
    var limit = size || 28;
    if (value.length <= limit) return value;
    return "…" + value.slice(-(limit - 1));
  }

  function renderWorkspaceInfo() {
    var info = state.workspace;
    if (!info) {
      dom.workspaceName.textContent = "—";
      dom.workspaceName.title = "";
      dom.wsCurrent.textContent = "—";
      return;
    }
    dom.workspaceName.textContent = shortPath(info.root) + (info.allow_writes ? "" : "（只读）");
    dom.workspaceName.title = info.root + (info.allow_writes ? "" : "\n只读模式：不允许写/删文件");
    dom.wsCurrent.textContent = info.root;
    dom.wsCurrent.title = info.root;
  }

  async function loadWorkspace() {
    try {
      state.workspace = await window.Auth.apiFetch("/workspace");
    } catch (error) {
      dom.workspaceName.textContent = "读取失败";
      dom.workspaceName.title = error.message;
      return;
    }
    renderWorkspaceInfo();
  }

  function setWorkspaceStatus(text, kind) {
    dom.wsStatus.textContent = text || "";
    dom.wsStatus.className = "modal__status" + (kind ? " " + kind : "");
  }

  function openWorkspaceModal() {
    var info = state.workspace;
    dom.wsRoot.value = info ? info.root : "";
    dom.wsWrites.checked = info ? !!info.allow_writes : true;
    // 工作区按账号隔离：每个登录用户都能改自己的那一份
    setWorkspaceStatus(info ? "当前：" + info.root : "", "");

    dom.wsModal.hidden = false;
    dom.wsRoot.focus();
    dom.wsRoot.select();
    loadWorkspaceTree();
  }

  function closeWorkspaceModal() {
    dom.wsModal.hidden = true;
  }

  async function loadWorkspaceTree() {
    dom.wsTree.textContent = "加载中…";
    try {
      var data = await window.Auth.apiFetch("/workspace/tree?depth=2");
      if (!data.entries || !data.entries.length) {
        dom.wsTree.textContent = "（空目录或全部被忽略）";
        return;
      }
      dom.wsTree.textContent = data.entries
        .map(function (item) {
          return item.type === "dir" ? "📁 " + item.path : "📄 " + item.path;
        })
        .join("\n");
    } catch (error) {
      dom.wsTree.textContent = "预览失败：" + error.message;
    }
  }

  async function validateWorkspacePath() {
    var root = dom.wsRoot.value.trim();
    if (!root) {
      setWorkspaceStatus("请输入绝对路径", "err");
      return;
    }
    setWorkspaceStatus("校验中…", "");
    try {
      var result = await window.Auth.apiFetch("/workspace/validate", {
        method: "POST",
        body: JSON.stringify({ root: root }),
      });
      if (result.valid) {
        var writable = result.writable ? "可写" : "不可写（只读模式仍可读取）";
        setWorkspaceStatus("✔ " + result.message + "（" + writable + "）：" + result.root, "ok");
      } else {
        setWorkspaceStatus("✘ " + result.message + "：" + result.root, "err");
      }
    } catch (error) {
      setWorkspaceStatus("校验失败：" + error.message, "err");
    }
  }

  async function saveWorkspace() {
    var root = dom.wsRoot.value.trim();
    dom.wsSave.disabled = true;
    setWorkspaceStatus("保存中…", "");
    try {
      var info = await window.Auth.apiFetch("/workspace", {
        method: "PUT",
        body: JSON.stringify({ root: root || null, allow_writes: dom.wsWrites.checked }),
      });
      state.workspace = info;
      renderWorkspaceInfo();
      setWorkspaceStatus(
        "✔ 已生效：" + info.root + (info.allow_writes ? "（可写）" : "（只读）"),
        "ok"
      );
      closeWorkspaceModal();
      showBanner("工作区已切换为 " + info.root + "（" + (info.allow_writes ? "可写" : "只读") + "）");
      setTimeout(hideBanner, 2800);
    } catch (error) {
      setWorkspaceStatus("保存失败：" + error.message, "err");
    } finally {
      dom.wsSave.disabled = false;
    }
  }

  /* -------------------------------------------------------------- 初始化信息 */

  async function loadSystemInfo() {
    try {
      var health = await window.Auth.apiFetch("/health");
      state.healthy = health.status === "ok";
      dom.statusDot.className = "status-dot" + (state.healthy ? "" : " degraded");
      dom.statusText.textContent = state.healthy ? "服务正常" : "服务降级";

      var info = await window.Auth.apiFetch("/agent/info");
      dom.modelName.textContent = info.provider + " / " + info.model;
      dom.toolCount.textContent = (info.tools || []).length + " 个工具";
      if (info.ready === false) {
        showBanner("模型未就绪：" + (info.readiness_error || "请检查 .env 里的 AGENT_API_KEY"));
      }
    } catch (error) {
      if (error.status === 401) return;
      dom.statusDot.className = "status-dot down";
      dom.statusText.textContent = "无法连接后端";
      showBanner("无法连接后端服务（" + error.message + "），请确认服务已启动。");
    }
  }

  /* -------------------------------------------------------------- 会话的加载 */

  async function openSession(id) {
    if (state.streaming) return;
    state.sessionId = id;
    setTitle(titleOf(id));
    renderSessionList();
    hideBanner();

    dom.chatLog.innerHTML = "";
    showEmptyState();

    try {
      var detail = await window.Auth.apiFetch("/sessions/" + encodeURIComponent(id));
      setTitle(detail.title || "新对话");
      var messages = detail.messages || [];
      if (!messages.length) {
        showEmptyState();
        return;
      }
      hideEmptyState();
      renderHistory(messages);
      scrollToBottom(true);
    } catch (error) {
      if (error.status === 404) {
        state.sessions = state.sessions.filter(function (item) {
          return item.session_id !== id;
        });
        state.sessionId = null;
        renderSessionList();
        setTitle("新对话");
        showEmptyState();
        showBanner("该对话不存在或已被删除。");
      } else if (error.status !== 401) {
        showBanner("加载历史失败：" + error.message);
      }
    }
  }

  function renderHistory(messages) {
    messages.forEach(function (message) {
      if (message.role === "user") {
        appendUserMessage(message.content || "");
        return;
      }
      if (message.role === "assistant") {
        var node = createAssistantMessage();
        if (node.thinking && node.thinking.parentNode) node.thinking.remove();
        var calls = message.tool_calls || [];
        if (calls.length) {
          calls.forEach(function (call) {
            var card = toolCard(node.tools, call);
            card.card.classList.remove("running");
            card.label.textContent = call.status === "error" ? "失败" : "已完成";
            card.result.textContent = call.result || "（无返回内容）";
          });
        }
        setContentMarkdown(node.content, message.content || "", false);
        return;
      }
      // role === "tool" 的消息已经在工具卡片里体现，这里不再重复渲染
    });
  }

  function startNewChat() {
    if (state.streaming) return;
    state.sessionId = null;
    dom.chatLog.innerHTML = "";
    showEmptyState();
    setTitle("新对话");
    renderSessionList();
    hideBanner();
    dom.input.value = "";
    autoGrow();
    dom.input.focus();
  }

  async function deleteSession(id) {
    try {
      await window.Auth.apiFetch("/sessions/" + encodeURIComponent(id), { method: "DELETE" });
    } catch (error) {
      if (error.status !== 401 && error.status !== 404) {
        showBanner("删除失败：" + error.message);
        return;
      }
    }
    state.sessions = state.sessions.filter(function (item) {
      return item.session_id !== id;
    });
    if (state.sessionId === id) startNewChat();
    renderSessionList();
  }

  async function clearAllSessions() {
    if (!window.confirm("确定删除你的全部历史对话吗？此操作不可撤销。")) return;
    try {
      var result = await window.Auth.apiFetch("/sessions?limit=200", { method: "DELETE" });
      state.sessions = [];
      startNewChat();
      renderSessionList();
      showBanner("已删除 " + ((result && result.deleted) || 0) + " 个对话");
      setTimeout(hideBanner, 2400);
    } catch (error) {
      if (error.status !== 401) showBanner("清空失败：" + error.message);
    }
  }

  /* ------------------------------------------------------------------ 发送流程 */

  function setStreaming(on) {
    state.streaming = on;
    dom.sendBtn.classList.toggle("stop", on);
    dom.sendBtn.title = on ? "停止生成" : "发送";
    dom.sendBtn.innerHTML = on
      ? '<svg width="13" height="13" viewBox="0 0 12 12" aria-hidden="true"><rect x="1" y="1" width="10" height="10" rx="2" fill="currentColor"/></svg>'
      : '<svg width="17" height="17" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12l16-8-6 8 6 8-16-8z" fill="currentColor"/></svg>';
  }

  /** 发起一次 SSE 请求；非 2xx 时抛出带 message 的错误（401 直接回到登录页）。 */
  async function requestStream(path, payload) {
    var response = await fetch(API + path, {
      method: "POST",
      headers: window.Auth.authHeaders(),
      body: JSON.stringify(payload),
      signal: state.controller ? state.controller.signal : undefined,
    });

    if (!response.ok || !response.body) {
      var detail = "";
      try {
        var body = await response.json();
        detail = (body.error && body.error.message) || JSON.stringify(body);
      } catch (error) {
        detail = "HTTP " + response.status;
      }
      if (response.status === 401) showAuthScreen("login");
      var failure = new Error(detail);
      failure.status = response.status;
      throw failure;
    }
    return response;
  }

  /**
   * 读取一个 SSE 响应流，把事件渲染到 node（一条助手消息）。
   * options.text / options.isFirstTurn 用于新建会话时的标题；
   * options.progress.aggregated 可带入已有文本（人工确认后继续时保留）。
   */
  async function consumeStream(response, node, options) {
    var opts = options || {};
    var progress = opts.progress || { aggregated: "" };
    var usage = null;
    var interrupted = false;
    var startedAt = performance.now();

    var reader = response.body.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";

    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });

      var frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (var i = 0; i < frames.length; i++) {
        var parsed = parseFrame(frames[i]);
        if (!parsed) continue;
        var event = parsed.event;
        var data = parsed.data;

        if (event === "session") {
          if (data.session_id && data.session_id !== state.sessionId) {
            state.sessionId = data.session_id;
            upsertSession(
              data.session_id,
              opts.isFirstTurn
                ? truncate(opts.text || "", TITLE_LIMIT)
                : data.title || titleOf(data.session_id)
            );
            setTitle(titleOf(data.session_id));
          }
        } else if (event === "token") {
          if (node.thinking && node.thinking.parentNode) node.thinking.remove();
          progress.aggregated += data.content || "";
          setContentMarkdown(node.content, progress.aggregated, true);
          scrollToBottom();
        } else if (event === "tool_call") {
          if (node.thinking && node.thinking.parentNode) node.thinking.remove();
          toolCard(node.tools, data);
        } else if (event === "tool_result") {
          fillToolResult(node.tools, data);
        } else if (event === "interrupt") {
          // Agent 暂停，等用户点 approve / edit / reject
          interrupted = true;
          if (node.thinking && node.thinking.parentNode) node.thinking.remove();
          if (progress.aggregated) setContentMarkdown(node.content, progress.aggregated, false);
          renderApproval(node, data);
        } else if (event === "final") {
          if (data.reply) progress.aggregated = data.reply;
          setContentMarkdown(node.content, progress.aggregated, false);
          usage = data.usage || null;
          var seconds = data.elapsed_ms
            ? (data.elapsed_ms / 1000).toFixed(1) + "s"
            : ((performance.now() - startedAt) / 1000).toFixed(1) + "s";
          var tokens = usage && usage.total_tokens ? " · " + usage.total_tokens + " tokens" : "";
          var toolCount = (data.tool_calls || []).length;
          appendMeta(
            node.meta,
            seconds + tokens + (toolCount ? " · 工具调用 " + toolCount + " 次" : "")
          );
          if (data.saved === false) {
            showBanner("回答已生成，但历史保存失败，请检查服务端日志。");
          }
          scrollToBottom();
        } else if (event === "error") {
          if (node.thinking && node.thinking.parentNode) node.thinking.remove();
          var errBox = document.createElement("div");
          errBox.className = "msg__error";
          errBox.textContent = "出错了：" + (data.message || data.code || "未知错误");
          node.content.appendChild(errBox);
          scrollToBottom();
        } else if (event === "done") {
          if (data.interrupted) interrupted = true;
        }
      }
    }

    if (!progress.aggregated && !interrupted) {
      if (node.thinking && node.thinking.parentNode) node.thinking.remove();
      setContentMarkdown(node.content, "（本轮没有返回内容）", false);
    }

    return { aggregated: progress.aggregated, interrupted: interrupted };
  }

  async function send() {
    var text = dom.input.value.trim();
    if (!text || state.streaming) return;
    if (text.length > MAX_INPUT) {
      showBanner("超出长度上限 " + MAX_INPUT + " 字符");
      return;
    }
    if (!window.Auth.isLoggedIn()) {
      showAuthScreen("login");
      return;
    }

    hideBanner();
    abandonPendingApprovals();
    appendUserMessage(text);
    dom.input.value = "";
    autoGrow();

    var isFirstTurn = !state.sessionId;
    var node = createAssistantMessage();
    var progress = { aggregated: "" };

    state.controller = new AbortController();
    setStreaming(true);

    try {
      var response = await requestStream("/chat/stream", {
        message: text,
        session_id: state.sessionId,
      });
      var outcome = await consumeStream(response, node, {
        text: text,
        isFirstTurn: isFirstTurn,
        progress: progress,
      });
      node.partial = outcome.aggregated;

      // 刷新会话列表里的排序/标题（以服务端为准）
      if (state.sessionId) loadSessions(dom.sessionSearch.value.trim() || undefined);
    } catch (error) {
      if (node.thinking && node.thinking.parentNode) node.thinking.remove();
      if (error.name === "AbortError") {
        setContentMarkdown(node.content, progress.aggregated || "（已停止生成）", false);
        appendMeta(node.meta, "已手动停止");
      } else {
        var box = document.createElement("div");
        box.className = "msg__error";
        box.textContent = "请求失败：" + error.message;
        node.content.appendChild(box);
        showBanner(error.message);
      }
    } finally {
      state.controller = null;
      setStreaming(false);
      dom.input.focus();
    }
  }

  function parseFrame(frame) {
    var lines = frame.split("\n");
    var event = null;
    var dataLines = [];
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.indexOf("event: ") === 0) event = line.slice(7).trim();
      else if (line.indexOf("data: ") === 0) dataLines.push(line.slice(6));
    }
    if (!event) return null;
    var data = {};
    if (dataLines.length) {
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch (error) {
        data = { raw: dataLines.join("\n") };
      }
    }
    return { event: event, data: data };
  }

  /* ------------------------------------------------------------------ 交互绑定 */

  function autoGrow() {
    dom.input.style.height = "auto";
    dom.input.style.height = Math.min(dom.input.scrollHeight, 200) + "px";
  }

  function bindEvents() {
    dom.authForm.addEventListener("submit", submitAuth);
    dom.authTabs[0].addEventListener("click", function () {
      setAuthMode("login");
    });
    dom.authTabs[1].addEventListener("click", function () {
      setAuthMode("register");
    });

    dom.sendBtn.addEventListener("click", function () {
      if (state.streaming && state.controller) {
        state.controller.abort();
        return;
      }
      send();
    });

    dom.input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (state.streaming) return;
        send();
      }
    });

    dom.input.addEventListener("input", autoGrow);

    dom.newChatBtn.addEventListener("click", startNewChat);

    dom.clearBtn.addEventListener("click", clearAllSessions);

    dom.logoutBtn.addEventListener("click", logout);

    dom.sessionSearch.addEventListener("input", function () {
      if (state.searchTimer) clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(function () {
        loadSessions(dom.sessionSearch.value.trim() || undefined);
      }, 260);
    });

    dom.menuBtn.addEventListener("click", function () {
      dom.sidebar.classList.toggle("collapsed");
    });

    dom.messages.addEventListener("scroll", function () {
      var gap = dom.messages.scrollHeight - dom.messages.scrollTop - dom.messages.clientHeight;
      state.stickBottom = gap < 80;
    });

    // 代码块复制
    dom.chatLog.addEventListener("click", function (event) {
      var button = event.target.closest(".code-copy");
      if (!button) return;
      var text = button.getAttribute("data-copy") || "";
      var done = function () {
        var origin = button.textContent;
        button.textContent = "已复制";
        setTimeout(function () {
          button.textContent = origin;
        }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(function () {
          fallbackCopy(text, done);
        });
      } else {
        fallbackCopy(text, done);
      }
    });

    // 建议卡片
    if (dom.emptyState) {
      dom.emptyState.addEventListener("click", function (event) {
        var card = event.target.closest(".suggestion");
        if (!card) return;
        dom.input.value = card.getAttribute("data-prompt") || "";
        autoGrow();
        dom.input.focus();
      });
    }

    // 工作区设置弹窗
    dom.openWorkspaceBtn.addEventListener("click", openWorkspaceModal);
    dom.wsValidate.addEventListener("click", validateWorkspacePath);
    dom.wsSave.addEventListener("click", saveWorkspace);
    dom.wsRoot.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        validateWorkspacePath();
      }
    });
    dom.wsDefault.addEventListener("click", function () {
      var info = state.workspace;
      dom.wsRoot.value = info ? info.default_root : "";
      validateWorkspacePath();
    });
    dom.wsTreeBox.addEventListener("toggle", function () {
      if (dom.wsTreeBox.open) loadWorkspaceTree();
    });
    dom.wsModal.addEventListener("click", function (event) {
      if (event.target.closest("[data-close]")) closeWorkspaceModal();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !dom.wsModal.hidden) closeWorkspaceModal();
    });
  }

  function fallbackCopy(text, done) {
    var area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      done();
    } catch (error) {
      /* ignore */
    }
    document.body.removeChild(area);
  }

  /* -------------------------------------------------------------------- 启动 */

  async function bootstrap() {
    bindEvents();
    renderSessionList();
    setTitle("新对话");

    window.Auth.onUnauthorized(function () {
      state.user = null;
      renderUser();
      showAuthScreen("login");
    });

    var config = await window.Auth.getConfig();
    state.allowRegister = !config || config.allow_register !== false;
    dom.authTabs[1].hidden = !state.allowRegister;

    if (!window.Auth.isLoggedIn()) {
      showAuthScreen("login");
      return;
    }

    // 有本地 token：先确认它还有效（顺带拿到最新用户信息）
    try {
      state.user = await window.Auth.fetchMe();
    } catch (error) {
      if (error.status === 401) {
        showAuthScreen("login");
        return;
      }
      state.user = window.Auth.getUser();
    }
    renderUser();
    await afterLogin();
  }

  document.addEventListener("DOMContentLoaded", bootstrap);
})();
