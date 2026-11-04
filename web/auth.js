/* ==========================================================================
   登录态管理（纯原生 JS，无构建步骤）
   - access_token / refresh_token 存 localStorage（生产环境更推荐 HttpOnly Cookie）
   - 所有请求统一走 Auth.apiFetch：自动带 Authorization、401 自动刷新一次
   - 刷新失败 -> 触发 onUnauthorized（页面弹出登录框）
   ========================================================================== */
(function () {
  "use strict";

  var API = "/api/v1";
  var STORE_KEY = "lc_agent_auth_v1";

  var state = {
    accessToken: null,
    refreshToken: null,
    user: null,
    onUnauthorized: null,
    refreshing: null,
  };

  /* ------------------------------------------------------------------ 存储 */

  function load() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (!raw) return;
      var data = JSON.parse(raw);
      state.accessToken = data.access_token || null;
      state.refreshToken = data.refresh_token || null;
      state.user = data.user || null;
    } catch (error) {
      state.accessToken = null;
      state.refreshToken = null;
      state.user = null;
    }
  }

  function save() {
    try {
      localStorage.setItem(
        STORE_KEY,
        JSON.stringify({
          access_token: state.accessToken,
          refresh_token: state.refreshToken,
          user: state.user,
        })
      );
    } catch (error) {
      /* 隐私模式下可能写入失败，忽略 */
    }
  }

  function clear() {
    state.accessToken = null;
    state.refreshToken = null;
    state.user = null;
    try {
      localStorage.removeItem(STORE_KEY);
    } catch (error) {
      /* ignore */
    }
  }

  /* ------------------------------------------------------------------ 请求 */

  function errorFrom(response, payload) {
    var message =
      (payload && payload.error && payload.error.message) ||
      (payload && payload.detail) ||
      "请求失败（HTTP " + response.status + "）";
    if (typeof message !== "string") message = JSON.stringify(message);
    var error = new Error(message);
    error.status = response.status;
    error.code = (payload && payload.error && payload.error.code) || null;
    error.details = (payload && payload.error && payload.error.details) || null;
    return error;
  }

  async function readJson(response) {
    if (response.status === 204) return null;
    try {
      return await response.json();
    } catch (error) {
      return null;
    }
  }

  function authHeaders(extra) {
    var headers = Object.assign({ "Content-Type": "application/json" }, extra || {});
    if (state.accessToken) headers.Authorization = "Bearer " + state.accessToken;
    return headers;
  }

  /**
   * 统一请求封装。
   * @param {string} path 以 /api/v1 开头的路径
   * @param {object} [options] fetch 选项
   * @param {boolean} [options._retry] 内部标记，避免刷新后无限重试
   */
  async function apiFetch(path, options) {
    var opts = options || {};
    var init = Object.assign({}, opts, { headers: authHeaders(opts.headers) });
    delete init._retry;

    var response = await fetch(API + path, init);

    if (response.status === 401 && !opts._retry && state.refreshToken) {
      var refreshed = await refreshTokens();
      if (refreshed) {
        return apiFetch(path, Object.assign({}, opts, { _retry: true }));
      }
    }

    var payload = await readJson(response);
    if (response.status === 401) {
      clear();
      if (typeof state.onUnauthorized === "function") state.onUnauthorized();
    }
    if (!response.ok) throw errorFrom(response, payload);
    return payload;
  }

  /* ---------------------------------------------------------------- 登录态 */

  async function refreshTokens() {
    if (!state.refreshToken) return false;
    // 并发请求同时 401 时只发一次刷新
    if (state.refreshing) return state.refreshing;

    state.refreshing = (async function () {
      try {
        var response = await fetch(API + "/auth/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: state.refreshToken }),
        });
        if (!response.ok) {
          clear();
          return false;
        }
        var payload = await response.json();
        applyAuth(payload);
        return true;
      } catch (error) {
        return false;
      } finally {
        state.refreshing = null;
      }
    })();

    return state.refreshing;
  }

  function applyAuth(payload) {
    state.accessToken = payload.tokens.access_token;
    state.refreshToken = payload.tokens.refresh_token;
    state.user = payload.user;
    save();
  }

  async function login(identifier, password) {
    var response = await fetch(API + "/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier: identifier, password: password }),
    });
    var payload = await readJson(response);
    if (!response.ok) throw errorFrom(response, payload);
    applyAuth(payload);
    return payload.user;
  }

  async function register(username, password, email, displayName) {
    var body = { username: username, password: password };
    if (email) body.email = email;
    if (displayName) body.display_name = displayName;
    var response = await fetch(API + "/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    var payload = await readJson(response);
    if (!response.ok) throw errorFrom(response, payload);
    applyAuth(payload);
    return payload.user;
  }

  async function fetchMe() {
    var user = await apiFetch("/auth/me");
    state.user = user;
    save();
    return user;
  }

  async function logout() {
    try {
      await apiFetch("/auth/logout", {
        method: "POST",
        body: JSON.stringify({ refresh_token: state.refreshToken }),
      });
    } catch (error) {
      /* 令牌可能已经失效，忽略即可 */
    }
    clear();
  }

  function getConfig() {
    return fetch(API + "/auth/config")
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .catch(function () {
        return null;
      });
  }

  load();

  window.Auth = {
    API: API,
    apiFetch: apiFetch,
    authHeaders: authHeaders,
    getConfig: getConfig,
    getUser: function () {
      return state.user;
    },
    getAccessToken: function () {
      return state.accessToken;
    },
    isLoggedIn: function () {
      return !!state.accessToken;
    },
    login: login,
    register: register,
    logout: logout,
    fetchMe: fetchMe,
    onUnauthorized: function (handler) {
      state.onUnauthorized = handler;
    },
  };
})();
