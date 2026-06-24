from __future__ import annotations

import json
import logging
import secrets
from collections import deque
from typing import Any

from aiohttp import web

from .config import Settings
from .llm import ModelProviderConfig, ModelRouter
from .memory import MemoryStore
from .plugins import PluginManager


SESSION_COOKIE = "the_one_admin_session"


class InMemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.records: deque[dict[str, str]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append({"level": record.levelname, "message": self.format(record), "name": record.name})

    def tail(self, limit: int = 200) -> list[dict[str, str]]:
        return list(self.records)[-limit:]


class AdminServer:
    def __init__(
        self,
        settings: Settings,
        memory: MemoryStore,
        llm: ModelRouter,
        plugin_manager: PluginManager,
        log_handler: InMemoryLogHandler,
    ) -> None:
        self.settings = settings
        self.memory = memory
        self.llm = llm
        self.plugin_manager = plugin_manager
        self.log_handler = log_handler
        self.session_token = secrets.token_urlsafe(32)
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None

    async def start(self) -> None:
        app = web.Application(middlewares=[self.auth_middleware])
        app.router.add_get("/", self.index)
        app.router.add_get("/api/session", self.api_session)
        app.router.add_post("/api/login", self.api_login)
        app.router.add_post("/api/logout", self.api_logout)
        app.router.add_get("/api/state", self.api_state)
        app.router.add_get("/api/users/{user_id}", self.api_user)
        app.router.add_post("/api/users/{user_id}/persona", self.api_update_persona)
        app.router.add_post("/api/users/{user_id}/mute", self.api_set_mute)
        app.router.add_post("/api/model/switch", self.api_switch_model)
        app.router.add_post("/api/model/provider", self.api_upsert_model_provider)
        app.router.add_post("/api/plugins/{name}/enabled", self.api_set_plugin_enabled)
        app.router.add_post("/api/plugins/{name}/config", self.api_set_plugin_config)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.settings.admin.host, self.settings.admin.port)
        await self.site.start()
        logging.info("[admin] dashboard started at http://%s:%s", self.settings.admin.host, self.settings.admin.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler):
        public_paths = {"/", "/api/session", "/api/login"}
        if request.path.startswith("/api/") and request.path not in public_paths:
            if not self._authenticated(request):
                raise web.HTTPUnauthorized(text="not authenticated")
        return await handler(request)

    def _authenticated(self, request: web.Request) -> bool:
        value = request.cookies.get(SESSION_COOKIE, "")
        return bool(value) and secrets.compare_digest(value, self.session_token)

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(text=HTML, content_type="text/html")

    async def api_session(self, request: web.Request) -> web.Response:
        return web.json_response({"authenticated": self._authenticated(request)})

    async def api_login(self, request: web.Request) -> web.Response:
        data = await request.json()
        username = str(data.get("username", ""))
        password = str(data.get("password", ""))
        if not secrets.compare_digest(username, self.settings.admin.username) or not secrets.compare_digest(
            password, self.settings.admin.password
        ):
            logging.warning("[admin] login failed username=%s", username)
            raise web.HTTPUnauthorized(text="账号或密码错误")
        response = web.json_response({"ok": True})
        response.set_cookie(SESSION_COOKIE, self.session_token, httponly=True, samesite="Strict")
        logging.info("[admin] login succeeded username=%s", username)
        return response

    async def api_logout(self, request: web.Request) -> web.Response:
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE)
        return response

    async def api_state(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "users": self.memory.list_agents(),
                "model": {
                    "active_provider": self.llm.active_provider,
                    "current": self.llm.describe_current(),
                    "providers": [self.llm.public_provider_dict(name) for name in self.llm.list_provider_names()],
                },
                "plugins": self.plugin_manager.list_status(),
                "logs": self.log_handler.tail(200),
            }
        )

    async def api_user(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"]
        agent = self.memory.get_or_create_agent(
            user_id,
            self.settings.bot.default_ai_name,
            self.settings.bot.default_persona,
        )
        muted = self.memory.get_plugin_state("proactive_response", user_id, "muted", "false").lower() == "true"
        return web.json_response(
            {
                "agent": {
                    "wx_user_id": agent.wx_user_id,
                    "ai_name": agent.ai_name,
                    "persona": agent.persona,
                    "proactive_muted": muted,
                },
                "summaries": self.memory.list_summary_dicts(user_id),
                "structured_memories": self.memory.list_structured_dicts(user_id),
                "messages": self.memory.list_recent_message_dicts(user_id),
            }
        )

    async def api_update_persona(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"]
        data = await request.json()
        persona = str(data.get("persona", "")).strip()
        ai_name = str(data.get("ai_name", "")).strip() or None
        if not persona:
            raise web.HTTPBadRequest(text="persona is required")
        agent = self.memory.update_persona(user_id, ai_name, persona)
        logging.info("[admin] persona updated user=%s", user_id)
        return web.json_response(
            {"ok": True, "agent": {"wx_user_id": agent.wx_user_id, "ai_name": agent.ai_name, "persona": agent.persona}}
        )

    async def api_set_mute(self, request: web.Request) -> web.Response:
        user_id = request.match_info["user_id"]
        data = await request.json()
        muted = bool(data.get("muted", False))
        self.memory.set_plugin_state("proactive_response", user_id, "muted", "true" if muted else "false")
        logging.info("[admin] proactive mute user=%s muted=%s", user_id, muted)
        return web.json_response({"ok": True, "muted": muted})

    async def api_switch_model(self, request: web.Request) -> web.Response:
        data = await request.json()
        provider_name = str(data.get("provider", "")).strip()
        if not provider_name:
            raise web.HTTPBadRequest(text="provider is required")
        provider = self.llm.switch_provider(provider_name)
        self.memory.set_plugin_state("core_model", "__global__", "active_provider", provider.name)
        logging.info("[admin] model switched provider=%s model=%s", provider.name, provider.model)
        return web.json_response({"ok": True, "provider": provider.name, "model": provider.model})

    async def api_upsert_model_provider(self, request: web.Request) -> web.Response:
        data = await request.json()
        name = str(data.get("name", "")).strip()
        if not name:
            raise web.HTTPBadRequest(text="name is required")
        old = self.llm.providers.get(name)
        raw_api_key = str(data.get("api_key", ""))
        api_key = old.api_key if old and raw_api_key == "" else raw_api_key.strip()
        provider = ModelProviderConfig(
            name=name,
            api_format=str(data.get("api_format", old.api_format if old else "openai_compatible")).strip(),
            api_key=api_key,
            base_url=str(data.get("base_url", old.base_url if old else "")).strip().rstrip("/"),
            model=str(data.get("model", old.model if old else "")).strip(),
            endpoint_path=str(data.get("endpoint_path", old.endpoint_path if old else "/chat/completions")).strip()
            or "/chat/completions",
            max_tokens=int(data.get("max_tokens", old.max_tokens if old else 1024)),
            temperature=float(data.get("temperature", old.temperature if old else 0.7)),
            timeout_seconds=int(data.get("timeout_seconds", old.timeout_seconds if old else 60)),
            headers=_json_object(data.get("headers"), "headers"),
            extra_body=_json_object(data.get("extra_body"), "extra_body"),
        )
        self.llm.upsert_provider(provider)
        self.memory.set_plugin_state(
            "core_model_provider",
            provider.name,
            "config",
            json.dumps(self.llm.export_provider_dict(provider.name), ensure_ascii=False),
        )
        logging.info("[admin] model provider saved name=%s model=%s", provider.name, provider.model)
        return web.json_response({"ok": True, "provider": self.llm.public_provider_dict(provider.name)})

    async def api_set_plugin_enabled(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        data = await request.json()
        enabled = bool(data.get("enabled", False))
        await self.plugin_manager.set_enabled(name, enabled)
        return web.json_response({"ok": True, "name": name, "enabled": enabled})

    async def api_set_plugin_config(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        data = await request.json()
        config = data.get("config", data)
        if not isinstance(config, dict):
            raise web.HTTPBadRequest(text="config must be an object")
        try:
            saved_config = await self.plugin_manager.set_config(name, config)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        logging.info("[admin] plugin config updated name=%s", name)
        return web.json_response({"ok": True, "name": name, "config": saved_config})


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text=f"{field_name} must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise web.HTTPBadRequest(text=f"{field_name} must be a JSON object")
    return parsed


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>The One 管理后台</title>
  <style>
    :root { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; color: #172033; background: #f5f7fb; }
    body { margin: 0; }
    header { height: 56px; display: flex; align-items: center; justify-content: space-between; padding: 0 20px; background: #111827; color: #fff; }
    header span { color: #cbd5e1; font-size: 13px; }
    button, select, textarea, input { font: inherit; box-sizing: border-box; }
    button { border: 1px solid #b8c2d3; background: #fff; border-radius: 6px; padding: 6px 10px; cursor: pointer; }
    button.primary { background: #2454d6; color: #fff; border-color: #2454d6; }
    input, select, textarea { border: 1px solid #c9d2e3; border-radius: 6px; padding: 7px 8px; width: 100%; }
    input[type="checkbox"] { width: auto; }
    textarea { min-height: 100px; resize: vertical; }
    label { display: block; font-size: 13px; color: #445064; margin: 8px 0 4px; }
    .hidden { display: none !important; }
    .login-shell { min-height: 100vh; display: grid; place-items: center; background: #eef2f7; }
    .login-card { width: min(420px, calc(100vw - 32px)); background: #fff; border: 1px solid #d8e0eb; border-radius: 10px; padding: 22px; }
    .layout { display: grid; grid-template-columns: 280px minmax(0, 1fr); min-height: calc(100vh - 56px); }
    aside { background: #fff; border-right: 1px solid #d9e0ea; padding: 14px; overflow: auto; }
    main { padding: 18px; overflow: auto; }
    .tabs { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px; }
    .tab { border-color: #c9d2e3; }
    .tab.active { background: #2454d6; color: #fff; border-color: #2454d6; }
    .panel { background: #fff; border: 1px solid #d9e0ea; border-radius: 8px; padding: 14px; margin-bottom: 14px; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .full { grid-column: 1 / -1; }
    .toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 10px 0; }
    .user { width: 100%; text-align: left; margin: 5px 0; }
    .user.active { background: #edf3ff; border-color: #2454d6; }
    .muted { color: #667085; font-size: 12px; }
    .pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 2px 8px; font-size: 12px; background: #eef2f7; color: #334155; }
    .pill.ok { background: #e8f7ee; color: #146c37; }
    .pill.warn { background: #fff4df; color: #8a5700; }
    pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f2f4f8; padding: 10px; border-radius: 6px; max-height: 380px; overflow: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { border-bottom: 1px solid #e4e8f0; padding: 7px; vertical-align: top; text-align: left; }
    @media (max-width: 900px) { .layout { grid-template-columns: 1fr; } aside { border-right: 0; border-bottom: 1px solid #d9e0ea; } .grid { grid-template-columns: 1fr; } .full { grid-column: auto; } }
  </style>
</head>
<body>
  <div id="loginView" class="login-shell">
    <div class="login-card">
      <h2>The One 管理后台</h2>
      <p class="muted">请输入本地后台账号和密码。默认只监听 127.0.0.1。</p>
      <label>账号</label><input id="loginUser" autocomplete="username" value="admin" />
      <label>密码</label><input id="loginPass" type="password" autocomplete="current-password" />
      <div class="toolbar"><button class="primary" onclick="login()">登录</button><span id="loginError" class="muted"></span></div>
    </div>
  </div>

  <div id="appView" class="hidden">
    <header>
      <div><strong>The One 管理后台</strong> <span id="status">加载中</span></div>
      <button onclick="logout()">退出</button>
    </header>
    <div class="layout">
      <aside>
        <div class="toolbar"><button onclick="manualRefresh()">刷新</button></div>
        <h3>绑定用户</h3>
        <div id="users"></div>
      </aside>
      <main>
        <div class="tabs">
          <button class="tab active" data-tab="account" onclick="showTab('account')">账号</button>
          <button class="tab" data-tab="model" onclick="showTab('model')">模型</button>
          <button class="tab" data-tab="persona" onclick="showTab('persona')">人设</button>
          <button class="tab" data-tab="plugins" onclick="showTab('plugins')">插件</button>
          <button class="tab" data-tab="memory" onclick="showTab('memory')">记忆</button>
          <button class="tab" data-tab="logs" onclick="showTab('logs')">日志</button>
        </div>
        <div id="tab-account" class="tab-panel"></div>
        <div id="tab-model" class="tab-panel hidden"></div>
        <div id="tab-persona" class="tab-panel hidden"></div>
        <div id="tab-plugins" class="tab-panel hidden"></div>
        <div id="tab-memory" class="tab-panel hidden"></div>
        <div id="tab-logs" class="tab-panel hidden"></div>
      </main>
    </div>
  </div>

  <script>
    let state = null, selectedUser = null, userDetail = null, selectedProvider = null, activeTab = 'account', dirty = false;

    async function api(url, options) {
      const res = await fetch(url, options || {});
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }

    async function checkSession() {
      const session = await api('/api/session');
      document.getElementById('loginView').classList.toggle('hidden', session.authenticated);
      document.getElementById('appView').classList.toggle('hidden', !session.authenticated);
      if (session.authenticated) await loadState(true);
    }

    async function login() {
      try {
        await api('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username: loginUser.value, password: loginPass.value})});
        loginError.textContent = '';
        await checkSession();
      } catch (err) { loginError.textContent = err.message; }
    }

    async function logout() { await api('/api/logout', {method:'POST'}); location.reload(); }

    function markDirty() { dirty = true; }
    function isFormActive() { return ['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName || ''); }

    async function loadState(force=false) {
      if (!force && (dirty || isFormActive())) return;
      state = await api('/api/state');
      if (!selectedProvider) selectedProvider = state.model.active_provider;
      renderAll();
      if (selectedUser && !dirty) await loadUser(selectedUser, false);
    }

    async function manualRefresh() {
      if (dirty && !confirm('当前页面有未保存内容，刷新会丢失。继续吗？')) return;
      dirty = false;
      await loadState(true);
    }

    function renderAll() {
      status.textContent = `后台正常 · ${new Date().toLocaleString()}`;
      renderUsers(); renderAccount(); renderModel(); renderPlugins(); renderPersona(); renderMemory(); renderLogs();
    }

    function showTab(tab) {
      activeTab = tab;
      document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
      document.querySelectorAll('.tab-panel').forEach(p => p.classList.add('hidden'));
      document.getElementById('tab-' + tab).classList.remove('hidden');
    }

    function renderUsers() {
      users.innerHTML = state.users.length ? state.users.map(u => `
        <button class="user ${u.wx_user_id===selectedUser?'active':''}" onclick="selectUser('${encodeURIComponent(u.wx_user_id)}')">
          <b>${escapeHtml(u.ai_name)}</b><br>
          <span class="muted">${escapeHtml(u.wx_user_id)}<br>${u.message_count || 0} 条消息 · ${escapeHtml(u.latest_message_at || '无最近消息')}</span>
        </button>`).join('') : '<p class="muted">暂无用户。用户给 Bot 发第一条消息后会出现在这里。</p>';
    }

    async function selectUser(id) {
      if (dirty && !confirm('当前页面有未保存内容，切换用户会丢失。继续吗？')) return;
      dirty = false;
      selectedUser = decodeURIComponent(id);
      await loadUser(selectedUser, true);
      renderUsers();
    }

    async function loadUser(userId, force=false) {
      if (!force && (dirty || isFormActive())) return;
      userDetail = await api('/api/users/' + encodeURIComponent(userId));
      renderAccount(); renderPersona(); renderMemory();
    }

    function renderAccount() {
      document.getElementById('tab-account').innerHTML = `<div class="panel"><h3>账号状态</h3>
        <p>已绑定用户数：${state.users.length}</p>
        <p>当前选择：${selectedUser ? escapeHtml(selectedUser) : '未选择'}</p>
        <p class="muted">左侧选择用户后，可在人设和记忆 Tab 查看详情。</p></div>`;
    }

    function renderModel() {
      const providers = state.model.providers;
      const current = providers.find(p => p.name === selectedProvider) || providers.find(p => p.name === state.model.active_provider) || providers[0];
      selectedProvider = current?.name || '';
      const options = providers.map(p => `<option value="${escapeAttr(p.name)}" ${p.name===selectedProvider?'selected':''}>${escapeHtml(p.name)}${p.name===state.model.active_provider?'（当前）':''}</option>`).join('');
      document.getElementById('tab-model').innerHTML = `<div class="panel"><h3>模型配置</h3>
        <p><span class="pill ok">当前</span> ${escapeHtml(state.model.current)}</p>
        <div class="toolbar"><select id="providerSelect" onchange="selectedProvider=this.value; renderModel()">${options}</select><button class="primary" onclick="switchModel()">切换为当前模型</button><button onclick="newProvider()">新增自定义模型</button></div>
        <div class="grid">
          <div><label>名称</label><input id="mpName" oninput="markDirty()" value="${escapeAttr(current?.name || '')}" /></div>
          <div><label>API 格式</label><input id="mpFormat" oninput="markDirty()" value="${escapeAttr(current?.api_format || 'openai_compatible')}" /></div>
          <div><label>Base URL</label><input id="mpBaseUrl" oninput="markDirty()" value="${escapeAttr(current?.base_url || '')}" /></div>
          <div><label>模型名</label><input id="mpModel" oninput="markDirty()" value="${escapeAttr(current?.model || '')}" /></div>
          <div><label>Endpoint Path</label><input id="mpEndpoint" oninput="markDirty()" value="${escapeAttr(current?.endpoint_path || '/chat/completions')}" /></div>
          <div><label>API Key <span class="${current?.has_api_key ? 'pill ok' : 'pill warn'}">${current?.has_api_key ? '已配置 ' + escapeHtml(current.api_key_masked) : '未配置'}</span></label><input id="mpApiKey" type="password" oninput="markDirty()" placeholder="留空保留原密钥；填写则覆盖" /></div>
          <div><label>Max Tokens</label><input id="mpMaxTokens" type="number" oninput="markDirty()" value="${escapeAttr(String(current?.max_tokens ?? 1024))}" /></div>
          <div><label>Temperature</label><input id="mpTemperature" type="number" step="0.1" oninput="markDirty()" value="${escapeAttr(String(current?.temperature ?? 0.7))}" /></div>
          <div><label>Timeout 秒</label><input id="mpTimeout" type="number" oninput="markDirty()" value="${escapeAttr(String(current?.timeout_seconds ?? 60))}" /></div>
          <div class="full"><label>Headers JSON</label><textarea id="mpHeaders" oninput="markDirty()">${escapeHtml(JSON.stringify(current?.headers || {}, null, 2))}</textarea></div>
          <div class="full"><label>Extra Body JSON</label><textarea id="mpExtra" oninput="markDirty()">${escapeHtml(JSON.stringify(current?.extra_body || {}, null, 2))}</textarea></div>
        </div>
        <div class="toolbar"><button class="primary" onclick="saveProvider()">保存模型配置</button><span class="muted">API Key 不明文回显；留空表示保留原值。</span></div></div>`;
    }

    function renderPersona() {
      if (!userDetail) { document.getElementById('tab-persona').innerHTML = '<div class="panel muted">请选择用户。</div>'; return; }
      const a = userDetail.agent;
      document.getElementById('tab-persona').innerHTML = `<div class="panel"><h3>人设</h3>
        <label>微信用户 ID</label><input value="${escapeAttr(a.wx_user_id)}" disabled />
        <label>AI 名称</label><input id="aiName" oninput="markDirty()" value="${escapeAttr(a.ai_name)}" />
        <label>AI 人设</label><textarea id="persona" oninput="markDirty()">${escapeHtml(a.persona)}</textarea>
        <div class="toolbar"><button class="primary" onclick="savePersona()">保存人设</button><button onclick="setMute(${!a.proactive_muted})">${a.proactive_muted ? '恢复主动消息' : '关闭主动消息'}</button></div></div>`;
    }

    function renderPlugins() {
      document.getElementById('tab-plugins').innerHTML = `<div class="panel"><h3>插件</h3>${state.plugins.map(p => `
        <label><input type="checkbox" ${p.enabled?'checked':''} onchange="setPlugin('${escapeAttr(p.name)}', this.checked)" /> ${escapeHtml(p.name)}</label>
        <div class="muted">${escapeHtml(p.description)}<br>
          版本：${escapeHtml(p.version || '未知')} · 状态：${p.running ? '正常' : (!p.manager_started && p.enabled ? '等待微信连接' : (p.enabled ? '后台任务已停止' : '已关闭'))} · 类型：${p.has_background_task ? '后台任务' : '事件响应'} · 模块：${escapeHtml(p.module || '')}
          ${p.author ? ` · 作者：${escapeHtml(p.author)}` : ''}</div>
        ${renderPluginConfigForm(p)}`).join('<hr>')}</div>`;
    }

    function renderPluginConfigForm(plugin) {
      const schema = plugin.config_schema || {};
      const properties = schema.properties || {};
      const keys = Object.keys(properties);
      if (!keys.length) return `<pre>${escapeHtml(JSON.stringify(plugin.config || {}, null, 2))}</pre>`;
      return `<div class="grid">${keys.map(key => renderPluginField(plugin, key, properties[key] || {})).join('')}</div>
        <div class="toolbar"><button class="primary" onclick="savePluginConfig('${escapeAttr(plugin.name)}')">保存插件配置</button><span class="muted">保存后会立即应用；后台任务会自动重启。</span></div>`;
    }

    function renderPluginField(plugin, key, spec) {
      const id = pluginFieldId(plugin.name, key);
      const value = plugin.config?.[key] ?? spec.default ?? '';
      const label = spec.label || key;
      const type = spec.type || 'string';
      const description = spec.description ? `<div class="muted">${escapeHtml(spec.description)}</div>` : '';
      if (Array.isArray(spec.enum)) {
        return `<div><label>${escapeHtml(label)}</label><select id="${id}" onchange="markDirty()">${spec.enum.map(option =>
          `<option value="${escapeAttr(option)}" ${String(value) === String(option) ? 'selected' : ''}>${escapeHtml(option)}</option>`
        ).join('')}</select>${description}</div>`;
      }
      if (type === 'boolean') {
        return `<div><label><input id="${id}" type="checkbox" ${value ? 'checked' : ''} onchange="markDirty()" /> ${escapeHtml(label)}</label>${description}</div>`;
      }
      if (type === 'integer' || type === 'number') {
        const step = type === 'integer' ? '1' : 'any';
        const min = spec.minimum === undefined ? '' : ` min="${escapeAttr(spec.minimum)}"`;
        const max = spec.maximum === undefined ? '' : ` max="${escapeAttr(spec.maximum)}"`;
        return `<div><label>${escapeHtml(label)}</label><input id="${id}" type="number" step="${step}"${min}${max} oninput="markDirty()" value="${escapeAttr(String(value))}" />${description}</div>`;
      }
      if (type === 'array') {
        const text = Array.isArray(value) ? value.join('\n') : String(value || '');
        return `<div class="full"><label>${escapeHtml(label)}</label><textarea id="${id}" oninput="markDirty()">${escapeHtml(text)}</textarea><div class="muted">每行一项。</div>${description}</div>`;
      }
      if (type === 'object') {
        return `<div class="full"><label>${escapeHtml(label)}</label><textarea id="${id}" oninput="markDirty()">${escapeHtml(JSON.stringify(value || {}, null, 2))}</textarea>${description}</div>`;
      }
      const inputType = spec.secret ? 'password' : 'text';
      const placeholder = spec.secret && spec.configured ? '已配置；留空保留原值' : '';
      return `<div><label>${escapeHtml(label)}</label><input id="${id}" type="${inputType}" placeholder="${escapeAttr(placeholder)}" oninput="markDirty()" value="${escapeAttr(String(value))}" />${description}</div>`;
    }

    function renderMemory() {
      if (!userDetail) { document.getElementById('tab-memory').innerHTML = '<div class="panel muted">请选择用户。</div>'; return; }
      document.getElementById('tab-memory').innerHTML = `<div class="panel"><h3>长期结构化记忆</h3>${table(userDetail.structured_memories, ['kind','key','value','confidence','updated_at'])}</div>
        <div class="panel"><h3>中期摘要</h3>${userDetail.summaries.map(s => `<pre>${escapeHtml(s.content)}</pre>`).join('') || '<p class="muted">暂无</p>'}</div>
        <div class="panel"><h3>热上下文 / 最近消息</h3>${table(userDetail.messages, ['id','role','content','created_at','compressed'])}</div>`;
    }

    function renderLogs() { document.getElementById('tab-logs').innerHTML = `<div class="panel"><h3>最近日志和错误</h3><pre>${escapeHtml(state.logs.map(l => l.message).join('\n'))}</pre></div>`; }

    async function savePersona() {
      await api('/api/users/' + encodeURIComponent(selectedUser) + '/persona', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ai_name: aiName.value, persona: persona.value})});
      dirty = false; await loadState(true);
    }
    async function setMute(muted) { await api('/api/users/' + encodeURIComponent(selectedUser) + '/mute', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({muted})}); await loadUser(selectedUser, true); }
    async function switchModel() { await api('/api/model/switch', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({provider: selectedProvider})}); await loadState(true); }
    async function saveProvider() {
      const payload = {name: mpName.value.trim(), api_format: mpFormat.value.trim(), api_key: mpApiKey.value, base_url: mpBaseUrl.value.trim(), model: mpModel.value.trim(), endpoint_path: mpEndpoint.value.trim(), max_tokens: Number(mpMaxTokens.value), temperature: Number(mpTemperature.value), timeout_seconds: Number(mpTimeout.value), headers: JSON.parse(mpHeaders.value || '{}'), extra_body: JSON.parse(mpExtra.value || '{}')};
      const result = await api('/api/model/provider', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
      selectedProvider = result.provider.name; dirty = false; await loadState(true);
    }
    function newProvider() { selectedProvider = 'custom_new'; state.model.providers.push({name:'custom_new', api_format:'openai_compatible', has_api_key:false, api_key_masked:'', base_url:'https://your-api-host.example/v1', model:'your-model-name', endpoint_path:'/chat/completions', max_tokens:1024, temperature:0.7, timeout_seconds:60, headers:{}, extra_body:{}}); renderModel(); markDirty(); }
    async function setPlugin(name, enabled) { await api('/api/plugins/' + encodeURIComponent(name) + '/enabled', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({enabled})}); await loadState(true); }
    async function savePluginConfig(name) {
      try {
        const plugin = state.plugins.find(p => p.name === name);
        const properties = plugin?.config_schema?.properties || {};
        const config = {};
        for (const [key, spec] of Object.entries(properties)) config[key] = readPluginField(name, key, spec || {});
        await api('/api/plugins/' + encodeURIComponent(name) + '/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({config})});
        dirty = false;
        status.textContent = `插件 ${name} 配置已保存`;
        await loadState(true);
      } catch (err) {
        status.textContent = `插件配置保存失败：${err.message}`;
        alert(`插件配置保存失败：${err.message}`);
      }
    }
    function readPluginField(pluginName, key, spec) {
      const element = document.getElementById(pluginFieldId(pluginName, key));
      if (!element) return null;
      const type = spec.type || 'string';
      if (type === 'boolean') return element.checked;
      if (type === 'integer') return Number.parseInt(element.value || '0', 10);
      if (type === 'number') return Number.parseFloat(element.value || '0');
      if (type === 'array') return element.value.split('\n').map(v => v.trim()).filter(Boolean);
      if (type === 'object') return JSON.parse(element.value || '{}');
      return element.value;
    }
    function pluginFieldId(pluginName, key) { return 'plugin_' + String(pluginName + '_' + key).replace(/[^a-zA-Z0-9_]/g, '_'); }
    function table(rows, keys) { if (!rows || !rows.length) return '<p class="muted">暂无</p>'; return `<table><thead><tr>${keys.map(k=>`<th>${k}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${keys.map(k=>`<td>${escapeHtml(String(r[k] ?? ''))}</td>`).join('')}</tr>`).join('')}</tbody></table>`; }
    function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
    function escapeAttr(s) { return escapeHtml(String(s)); }
    checkSession().catch(err => { loginError.textContent = err.message; });
    setInterval(() => loadState(false).catch(() => {}), 10000);
  </script>
</body>
</html>"""
