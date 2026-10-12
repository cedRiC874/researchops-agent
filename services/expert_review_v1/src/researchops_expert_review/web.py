"""Local-only standard-library UI for the expert review service."""
from __future__ import annotations

import html
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from researchops import self_pilot

from .core import ExpertReview, ExpertReviewError, FORMAT_NOTICE, ONLINE_LIMITATION, completion_summary

_MAX_BODY_BYTES = 16 * 1024


def _display(value: Any) -> str:
    return self_pilot._safe_display_output(str(value))[0]


def _public_result(record: dict[str, Any]) -> dict[str, Any]:
    """Never return persisted question text, provider payloads, or local metadata."""
    result = {
        "status": record["status"],
        "answers": {key: _display(record["answers"][key]) for key in ("english", "chinese")},
        "tool_call_count": record["tool_call_count"],
        "output_redacted": record["output_redacted"],
        "notice": _display(record.get("notice", "无自动评分")),
    }
    if "completion_telemetry" in record:
        result["completion_summary"] = completion_summary(record)
    return result


def serve(review: ExpertReview, *, port: int = 8765) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ExpertReviewError("web_port_invalid", "Web 端口必须在 1 到 65535 之间。")
    handler = _build_handler(review, secrets.token_urlsafe(32), port)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        raise ExpertReviewError("web_bind_failed", "无法绑定本机端口，请更换端口。") from None
    server.daemon_threads = True
    print(f"现场提问：http://127.0.0.1:{port}")
    print("仅在本机监听；按 Ctrl+C 停止。无自动评分。")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _build_handler(review: ExpertReview, csrf_token: str, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {"http://" + host for host in allowed_hosts}
    run_lock = threading.Lock()

    class ExpertReviewHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format: str, *args: Any) -> None:
            # URLs and HTTP request data must never enter a local access log.
            pass

        def _host(self) -> None:
            if self.headers.get("Host", "").lower() not in allowed_hosts:
                raise ExpertReviewError("web_host_denied", "只接受本机 Host。")

        def _post_authorization(self) -> None:
            if not secrets.compare_digest(self.headers.get("X-ResearchOps-Token", ""), csrf_token):
                raise ExpertReviewError("web_csrf_denied", "Web 请求令牌无效。")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                raise ExpertReviewError("web_origin_denied", "Web 请求来源无效。")

        def _body(self) -> dict[str, Any]:
            if self.headers.get("Transfer-Encoding") is not None:
                raise ExpertReviewError("web_request_invalid", "不支持此请求编码。")
            if self.headers.get_content_type() != "application/json":
                raise ExpertReviewError("web_request_invalid", "请求必须使用 JSON。")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ExpertReviewError("web_request_invalid", "请求长度无效。") from None
            if not 0 < length <= _MAX_BODY_BYTES:
                raise ExpertReviewError("web_request_invalid", "请求长度无效或正文过大。")
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeError, json.JSONDecodeError):
                raise ExpertReviewError("web_request_invalid", "请求 JSON 无效。") from None
            if not isinstance(value, dict):
                raise ExpertReviewError("web_request_invalid", "请求必须为 JSON 对象。")
            return value

        def _json(self, status: int, value: Any) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def _error(self, exc: ExpertReviewError, status: int = 400) -> None:
            self._json(status, {"error": {"code": exc.code, "message": exc.message}})

        def do_GET(self) -> None:  # noqa: N802
            try:
                self._host()
                parsed = urlsplit(self.path)
                if parsed.path == "/":
                    page = _HTML.replace("__TOKEN__", html.escape(csrf_token, quote=True)).replace("__FORMAT_NOTICE__", html.escape(FORMAT_NOTICE)).replace("__ONLINE_LIMITATION__", html.escape(ONLINE_LIMITATION))
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                elif parsed.path == "/app.js":
                    self._send(200, _JAVASCRIPT.encode("utf-8"), "text/javascript; charset=utf-8")
                elif parsed.path == "/app.css":
                    self._send(200, _CSS.encode("utf-8"), "text/css; charset=utf-8")
                elif parsed.path == "/api/state":
                    catalog = [{"dataset_id": _display(item["dataset_id"]), "domain": _display(item.get("domain", ""))} for item in review.catalog()]
                    history = [{"sequence": item["sequence"], "dataset_id": _display(item["dataset_id"]), "status": _display(item["status"])} for item in review.history()]
                    self._json(200, {"datasets": catalog, "history": history})
                elif parsed.path == "/api/examples":
                    query = parse_qs(parsed.query)
                    values = query.get("dataset_id", [])
                    if set(query) != {"dataset_id"} or len(values) != 1:
                        raise ExpertReviewError("web_request_invalid", "请选择一个数据集。")
                    self._json(200, {"examples": [{"task_id": _display(item["task_id"]), "prompt": _display(item["prompt"])} for item in review.examples(values[0])]})
                else:
                    self._error(ExpertReviewError("web_not_found", "页面不存在。"), 404)
            except ExpertReviewError as exc:
                self._error(exc)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self._error(ExpertReviewError("web_internal_error", "本机请求失败；未记录异常正文。"), 500)

        def do_POST(self) -> None:  # noqa: N802
            try:
                self._host()
                self._post_authorization()
                if self.path != "/api/ask":
                    self._error(ExpertReviewError("web_not_found", "接口不存在。"), 404)
                    return
                payload = self._body()
                if payload.get("confirm_online") is not True:
                    raise ExpertReviewError("online_confirmation_required", "请先确认会把问题文本和汇总统计发送给 DeepSeek；未读取密钥。")
                if set(payload) == {"confirm_online", "from_task"} and isinstance(payload["from_task"], str):
                    arguments = {"confirm_online": True, "from_task": payload["from_task"]}
                elif set(payload) == {"confirm_online", "dataset_id", "question"} and isinstance(payload["dataset_id"], str) and isinstance(payload["question"], str):
                    arguments = {"confirm_online": True, "dataset_id": payload["dataset_id"], "question": payload["question"]}
                else:
                    raise ExpertReviewError("web_request_invalid", "请选择示例题，或选择数据集并输入问题；不能混用。")
                if not run_lock.acquire(blocking=False):
                    self._error(ExpertReviewError("web_run_busy", "本会话正在运行，请等待当前问题结束。"), 409)
                    return
                try:
                    result = review.ask(**arguments)
                finally:
                    run_lock.release()
                self._json(200, _public_result(result))
            except ExpertReviewError as exc:
                self._error(exc)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self._error(ExpertReviewError("web_internal_error", "执行失败；未记录异常正文，请检查本会话列表。"), 500)

    return ExpertReviewHandler


_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="researchops-token" content="__TOKEN__"><title>现场提问 · 专家试用</title><link rel="stylesheet" href="/app.css"><script src="/app.js" defer></script></head>
<body><main><header><p>ResearchOps · 专家试用</p><h1>现场提问</h1><p>在已准备好的公开数据集上提问。每题只运行一次，无自动评分。</p></header>
<aside><p>__FORMAT_NOTICE__</p><p>__ONLINE_LIMITATION__</p></aside>
<form id="ask-form"><fieldset id="controls" disabled><label for="dataset">公开数据集</label><select id="dataset" required></select>
<label for="example">问题来源</label><select id="example"><option value="">自己输入问题</option></select>
<label for="question">问题（最多 2000 字；示例保留英文原文）</label><textarea id="question" rows="6" maxlength="2000" required></textarea>
<label class="confirmation"><input id="confirm" type="checkbox" required>我确认会把问题文本和汇总统计发送给 DeepSeek</label>
<button id="run" type="submit">运行一次</button></fieldset></form>
<p id="message" role="status" aria-live="polite">正在加载本机会话…</p>
<section id="result" hidden><h2>本次结果</h2><p id="status"></p><p id="notice"></p><h3>English</h3><pre id="english"></pre><h3>中文</h3><pre id="chinese"></pre><p id="calls"></p><p id="completion"></p></section>
<section><h2>本会话已运行的问题</h2><table><thead><tr><th scope="col">序号</th><th scope="col">数据集</th><th scope="col">状态</th></tr></thead><tbody id="history"></tbody></table></section>
</main></body></html>"""

_CSS = """*{box-sizing:border-box}body{margin:0;color:#17313c;background:#f4f7f7;font:16px/1.65 system-ui,sans-serif}main{max-width:880px;margin:36px auto;padding:0 24px 50px}h1{font-size:36px;margin:6px 0}h2{font-size:23px}h3{font-size:18px}header>p:first-child{color:#437172}aside{background:#e8f0ee;border-left:4px solid #337e78;padding:8px 20px;margin:24px 0}form,section{background:white;padding:24px;border:1px solid #d7e2df;border-radius:10px;margin:20px 0}fieldset{border:0;margin:0;padding:0;min-width:0}label{display:block;margin:12px 0 6px}select,textarea{width:100%;font:inherit;padding:10px;border:1px solid #a6bcb5;border-radius:5px}textarea{resize:vertical}.confirmation{margin:20px 0}.confirmation input{margin-right:8px}button{font:inherit;border:0;border-radius:5px;padding:10px 24px;background:#1b6863;color:white;cursor:pointer}button:disabled,fieldset:disabled button{opacity:.5;cursor:wait}pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{text-align:left;padding:10px 6px;border-bottom:1px solid #e0e8e6;overflow-wrap:anywhere}th:first-child{width:65px}th:last-child{width:125px}#message{overflow-wrap:anywhere}@media(max-width:600px){main{margin:16px auto;padding:0 12px}form,section{padding:16px}}"""

_JAVASCRIPT = """'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="researchops-token"]').content;
let busy = false;
let examples = [];
async function api(path, payload) {
  const options = payload === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-ResearchOps-Token':token},body:JSON.stringify(payload)};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || '本机请求失败。');
  return data;
}
function showHistory(rows) {
  $('history').replaceChildren();
  for (const item of rows) {
    const row = document.createElement('tr');
    for (const value of [item.sequence, item.dataset_id, item.status]) {
      const cell = document.createElement('td'); cell.textContent = String(value); row.append(cell);
    }
    $('history').append(row);
  }
}
function updateQuestion() {
  const selected = examples.find(item => item.task_id === $('example').value);
  $('question').value = selected ? selected.prompt : '';
  $('question').readOnly = Boolean(selected);
  $('confirm').checked = false;
}
async function loadExamples() {
  const dataset = $('dataset').value;
  $('controls').disabled = true;
  try {
    const data = await api('/api/examples?dataset_id=' + encodeURIComponent(dataset));
    examples = data.examples;
    $('example').replaceChildren(new Option('自己输入问题', ''));
    for (const item of examples) $('example').add(new Option(item.task_id + ' · ' + item.prompt.slice(0,80), item.task_id));
    updateQuestion();
    $('message').textContent = '请选择示例题或输入问题。';
    $('controls').disabled = false;
  } catch (error) { $('message').textContent = error.message; }
}
$('example').addEventListener('change', updateQuestion);
$('dataset').addEventListener('change', loadExamples);
$('ask-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !$('confirm').checked) return;
  busy = true; $('controls').disabled = true; $('run').disabled = true;
  $('message').textContent = '正在运行，请等待。本题提交后不能重跑。';
  $('result').hidden = true;
  const payload = $('example').value ? {from_task:$('example').value,confirm_online:true} : {dataset_id:$('dataset').value,question:$('question').value,confirm_online:true};
  try {
    const data = await api('/api/ask', payload);
    $('status').textContent = '状态：' + data.status;
    $('notice').textContent = data.notice;
    $('english').textContent = data.answers.english;
    $('chinese').textContent = data.answers.chinese;
    $('calls').textContent = '工具调用次数：' + data.tool_call_count + (data.output_redacted ? '；回答已被安全过滤器整段屏蔽。' : '');
    $('completion').textContent = data.completion_summary || '模型调用摘要：尚无已登记的调用。';
    $('result').hidden = false;
    $('message').textContent = '本次运行已结束。无自动评分。';
  } catch (error) { $('message').textContent = error.message; }
  finally {
    try { const data = await api('/api/state'); showHistory(data.history); }
    catch (error) { $('message').textContent += ' ' + error.message; }
    busy = false; $('controls').disabled = false; $('run').disabled = false; $('confirm').checked = false;
  }
});
(async () => {
  try {
    const data = await api('/api/state'); showHistory(data.history);
    for (const item of data.datasets) $('dataset').add(new Option(item.dataset_id + (item.domain ? ' · ' + item.domain : ''), item.dataset_id));
    if (data.datasets.length) await loadExamples();
    else $('message').textContent = '登记表中没有可用的公开数据集。';
  } catch (error) { $('message').textContent = error.message; }
})();
"""
