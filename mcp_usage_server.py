#!/usr/bin/env python3
"""Minimal MCP server: Codex usage widget host.

Zero-dependency stdio MCP server. Exposes two tools:
  - get_codex_usage:  fetch usage/balance data, return structuredContent
  - show_usage_widget: same data, plus _meta.ui.resourceUri so MCP-Apps-capable
    hosts (ChatGPT / Codex desktop) mount the HTML widget from ui://codex-usage/widget.html

Also the lifecycle owner of the overlay strip: spawns it at startup, kills it
on exit; the overlay watches our pid as a safety net.

Data layer: usage_sources.py (auto-detects ChatGPT login vs one-api relay).
"""

import json
import os
import subprocess
import sys

from usage_sources import fetch_usage  # noqa: F401  (re-exported for callers)

WIDGET_URI = "ui://codex-usage/widget.html"
RESOURCE_MIME = "text/html;profile=mcp-app"
SERVER_NAME = "codex-usage-widget"
SERVER_VERSION = "0.2.0"


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def log(*a) -> None:
    # stdout is reserved for the protocol; diagnostics go to stderr
    print(*a, file=sys.stderr, flush=True)


def summarize(u: dict) -> str:
    if u.get("mode") == "relay":
        cur = "\u00a5" if u.get("currency") == "CNY" else "$"
        line = f"Relay {u.get('provider')} balance: {cur}{u.get('balance', 0):.2f}"
        if u.get("total") is not None:
            line += f" (used {cur}{u.get('used', 0):.2f} of {cur}{u['total']:.2f})"
        return line + f", protocol {u.get('protocol')}."
    p = u.get("primary_window") or {}
    pct = p.get("used_percent")
    remain = max(0, 100 - (pct or 0))
    reset = p.get("reset_after_seconds") or 0
    days, rem = divmod(int(reset), 86400)
    hours = rem // 3600
    when = f"{days}d {hours}h" if days else f"{hours}h"
    return (f"Codex usage ({u.get('plan_type')}): {remain}% remaining "
            f"({pct}% used), resets in {when}. Limit reached: {u.get('limit_reached')}.")


# ---------------------------------------------------------------- widget HTML

WIDGET_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root { color-scheme: dark; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
    background: #1f1f23; color: #ececf1; width: 300px;
    padding: 14px 16px 12px; border-radius: 12px;
  }
  .head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .title { font-size: 13px; font-weight: 600; letter-spacing: .02em; flex: 1; }
  .badge {
    font-size: 10px; padding: 2px 8px; border-radius: 999px;
    background: #2f2f36; color: #b4b4bc; text-transform: uppercase;
  }
  button.refresh {
    all: unset; cursor: pointer; font-size: 13px; line-height: 1;
    padding: 3px 6px; border-radius: 6px; color: #b4b4bc;
  }
  button.refresh:hover { background: #2f2f36; color: #fff; }
  button.refresh.busy { animation: spin 1s linear infinite; display: inline-block; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .pct { font-size: 34px; font-weight: 700; line-height: 1.1; }
  .pct small { font-size: 18px; font-weight: 500; color: #b4b4bc; }
  .wlabel { font-size: 11px; color: #8e8e96; margin-bottom: 6px; }
  .bar { height: 6px; background: #2f2f36; border-radius: 999px; overflow: hidden; margin: 8px 0 6px; }
  .bar > i { display: block; height: 100%; border-radius: 999px; background: #10a37f; transition: width .6s ease; }
  .row { display: flex; justify-content: space-between; font-size: 11.5px; color: #b4b4bc; margin-top: 4px; }
  .row b { color: #ececf1; font-weight: 600; }
  .foot { margin-top: 10px; font-size: 10px; color: #6f6f78; display: flex; justify-content: space-between; }
  .err { color: #ff6b6b; font-size: 12px; line-height: 1.5; }
  a { color: #10a37f; cursor: pointer; text-decoration: underline; }
</style>
</head>
<body>
  <div class="head">
    <div class="title">&#9889; Codex &#29992;&#37327;</div>
    <span class="badge" id="plan">--</span>
    <button class="refresh" id="btn" title="&#21047;&#26032;">&#8635;</button>
  </div>
  <div id="main">
    <div class="wlabel" id="wlabel">&#21152;&#36733;&#20013;&#8230;</div>
    <div class="pct" id="pct">--<small>%</small></div>
    <div class="bar"><i id="fill" style="width:0%"></i></div>
    <div class="row"><span id="reset">&#8230;</span></div>
    <div class="row" id="weekly" style="display:none"><span>&#21608;&#38480;&#39069;</span><b id="wpct">--</b></div>
    <div class="row" id="creditrow" style="display:none"><span>&#20313;&#39069;</span><b id="credit">--</b></div>
  </div>
  <div id="errbox" style="display:none" class="err"><span id="errtext"></span> <a onclick="refresh()">&#37325;&#35797;</a></div>
  <div class="foot"><span id="updated"></span><span id="src"></span></div>

<script>
(function () {
  var pending = {}, nextId = 1000, data = null, fetchedAtMs = 0;

  function request(method, params) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      window.parent.postMessage({ jsonrpc: "2.0", id: id, method: method, params: params }, "*");
      setTimeout(function () {
        if (pending[id]) { pending[id].reject(new Error("timeout: " + method)); delete pending[id]; }
      }, 30000);
    });
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var m = event.data;
    if (!m || m.jsonrpc !== "2.0") return;
    if (m.id != null && pending[m.id]) {          // response to our request
      var p = pending[m.id]; delete pending[m.id];
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
      return;
    }
    if (m.method === "ui/notifications/tool-result") {
      var sc = (m.params || {}).structuredContent;
      if (sc && sc.primary_window !== undefined) apply(sc);
    }
  });

  function apply(u) {
    data = u; fetchedAtMs = Date.now();
    document.getElementById("errbox").style.display = "none";
    document.getElementById("main").style.display = "";
    document.getElementById("plan").textContent = u.plan_type || "--";
    var p = u.primary_window;
    if (p) {
      var secs = p.limit_window_seconds || 0;
      var label = secs <= 3600 ? "5 \u5c0f\u65f6\u7a97\u53e3" :
                  secs <= 6*86400 ? "\u672c\u5468" : secs >= 27*86400 ? "\u672c\u6708" : "\u9650\u989d\u7a97\u53e3";
      document.getElementById("wlabel").textContent = label + "\u5df2\u7528";
      var pct = Math.round(p.used_percent || 0);
      var el = document.getElementById("pct");
      el.innerHTML = pct + "<small>%</small>";
      var fill = document.getElementById("fill");
      fill.style.width = Math.min(100, pct) + "%";
      fill.style.background = pct >= 90 ? "#ef4444" : pct >= 70 ? "#f59e0b" : "#10a37f";
    }
    var w = u.secondary_window;
    document.getElementById("weekly").style.display = w ? "" : "none";
    if (w) document.getElementById("wpct").textContent = Math.round(w.used_percent || 0) + "%";
    var cb = u.credits_balance;
    document.getElementById("creditrow").style.display = (cb != null) ? "" : "none";
    if (cb != null) document.getElementById("credit").textContent = "$" + Number(cb).toFixed(2);
    document.getElementById("updated").textContent =
      "\u66f4\u65b0\u4e8e " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
    document.getElementById("src").textContent = u.limit_reached ? "\u5df2\u89e6\u53d1\u9650\u989d" : "";
    tick();
  }

  function tick() {
    if (!data || !data.primary_window) return;
    var s = (data.primary_window.reset_after_seconds || 0) - (Date.now() - fetchedAtMs) / 1000;
    if (s < 0) s = 0;
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    document.getElementById("reset").innerHTML =
      "\u91cd\u7f6e\u5012\u8ba1\u65f6 <b>" +
      (d ? d + " \u5929 " : "") + (d || h ? h + " \u5c0f\u65f6 " : "") + m + " \u5206\u949f</b>";
  }
  setInterval(tick, 30000);

  function fail(msg) {
    document.getElementById("main").style.display = "none";
    var box = document.getElementById("errbox");
    box.style.display = ""; document.getElementById("errtext").textContent = msg;
  }

  function refresh() {
    var btn = document.getElementById("btn");
    btn.classList.add("busy");
    request("tools/call", { name: "get_codex_usage", arguments: {} })
      .then(function (r) {
        var sc = r && r.structuredContent;
        if (sc && sc.error) fail(sc.error);
        else if (sc) apply(sc);
        else fail("\u65e0\u6570\u636e\u8fd4\u56de");
      })
      .catch(function (e) { fail("\u83b7\u53d6\u5931\u8d25\uff1a" + e.message); })
      .finally(function () { btn.classList.remove("busy"); });
  }
  document.getElementById("btn").onclick = refresh;
  window.refresh = refresh;

  setTimeout(refresh, 500);      // initial fetch; host notification may arrive first
  setInterval(refresh, 60000);   // same cadence as Codex CLI itself
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- protocol

def tool_defs() -> list:
    data_meta = {
        "name": "get_codex_usage",
        "description": (
            "Fetch current Codex usage / quota from the ChatGPT backend: "
            "primary window used percent (5h / weekly / monthly depending on plan), "
            "reset countdown, secondary window and credit balance if present. Returns data only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    }
    widget_meta = {
        "name": "show_usage_widget",
        "description": (
            "Show an interactive always-fresh widget displaying the user's Codex usage/quota "
            "(percent bar, reset countdown, weekly limit, balance). "
            "Call this when the user asks to see/display their Codex usage or balance."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "_meta": {"ui": {"resourceUri": WIDGET_URI}},
    }
    return [data_meta, widget_meta]


def call_tool(name: str) -> dict:
    if name not in ("get_codex_usage", "show_usage_widget"):
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True,
        }
    try:
        usage = fetch_usage()
    except Exception as e:  # noqa: BLE001 - surface any failure to the model/UI
        return {
            "content": [{"type": "text", "text": f"Failed to fetch Codex usage: {e}"}],
            "structuredContent": {"error": str(e)},
            "isError": True,
        }
    result = {
        "content": [{"type": "text", "text": summarize(usage)}],
        "structuredContent": usage,
    }
    if name == "show_usage_widget":
        result["_meta"] = {"ui": {"resourceUri": WIDGET_URI}}
    return result


def handle(msg: dict) -> dict | None:
    method = msg.get("method", "")
    msg_id = msg.get("id")
    is_request = msg_id is not None

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False, "subscribe": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tool_defs()}}
    if method == "tools/call":
        params = msg.get("params", {})
        return {"jsonrpc": "2.0", "id": msg_id, "result": call_tool(params.get("name", ""))}
    if method == "resources/list":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "resources": [{
                    "uri": WIDGET_URI, "name": "Codex usage widget",
                    "description": "Interactive Codex usage/quota widget",
                    "mimeType": RESOURCE_MIME,
                }]
            },
        }
    if method == "resources/read":
        uri = msg.get("params", {}).get("uri", "")
        if uri != WIDGET_URI:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32002, "message": f"Unknown resource: {uri}"}}
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "contents": [{
                    "uri": WIDGET_URI,
                    "mimeType": RESOURCE_MIME,
                    "text": WIDGET_HTML,
                    "_meta": {"ui": {"prefersBorder": True}},
                }]
            },
        }
    if not is_request:          # notifications (initialized, cancelled, ...)
        return None
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}}


def spawn_overlay():
    """Launch the overlay strip as our child process.

    Called at server startup (Codex spawns us), so the overlay's lifetime is
    tied to Codex's. The overlay also watches our pid as a safety net for the
    case where Codex kills us without closing stdin cleanly. A single-instance
    mutex in the overlay dedupes if one is already running.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "codex_usage_overlay.py")
    if not os.path.exists(script):
        return None
    exe_dir = os.path.dirname(sys.executable)
    gui = os.path.join(exe_dir, "codex-usage.exe")     # renamed pythonw copy
    if not os.path.exists(gui):
        gui = os.path.join(exe_dir, "pythonw.exe")
        if not os.path.exists(gui):
            gui = sys.executable
    cmd = [gui, script, "--parent-pid", str(os.getpid())]
    try:
        return subprocess.Popen(
            cmd, creationflags=0x08000000,  # CREATE_NO_WINDOW
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001 - never break the MCP server
        log("overlay spawn failed:", e)
        return None


def main() -> None:
    log(f"{SERVER_NAME} v{SERVER_VERSION} starting, codex_home={codex_home()}")
    overlay = spawn_overlay()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log("unparseable line:", line[:200])
                continue
            try:
                resp = handle(msg)
            except Exception as e:  # noqa: BLE001 - never crash the server loop
                log("handler error:", repr(e))
                resp = {"jsonrpc": "2.0", "id": msg.get("id"),
                        "error": {"code": -32603, "message": f"Internal error: {e}"}}
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        if overlay:
            try:
                overlay.terminate()
                overlay.wait(timeout=3)
            except Exception:  # noqa: BLE001 - overlay also self-exits via pid watch
                try:
                    overlay.kill()
                except Exception:  # noqa: BLE001
                    pass


if __name__ == "__main__":
    main()
