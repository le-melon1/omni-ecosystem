#!/usr/bin/env python3
"""
OmniRoute + Claude Code Smart Orchestrator & Auto-Failover Proxy
Кроссплатформенная версия (Windows 10/11, macOS, Linux).

Возможности:
1. Автоматический поиск OmniRoute (порт 20128, 8000, 3000...).
2. Параллельный Health-Check моделей с замером задержки.
3. Нумерованный список РАБОТАЮЩИХ моделей и разъяснение auto/best-free.
4. Интерактивный выбор: какую модель назначить Smart (основная), а какую Light (быстрая).
5. МГНОВЕННАЯ АКТИВАЦИЯ выбранных моделей (через прокси на лету и автогенерацию скриптов запуска).
6. Локальный Transparent Failover Proxy:
   - Если активная модель отдает 429, 500, 502 или таймаут — на лету заменяет модель на следующую из пула.
7. Живой мониторинг в реальном времени (окно 1):
   HH:MM:SS | model-name | InTokens↑ OutTokens↓ | Latency(ms)
8. Горячие команды в консоли (c — сменить модель, s — статус, r — перезапуск, q — выход).
"""

import sys
import os
import re
import time
import json
import socket
import select
import signal
import shutil
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import concurrent.futures
import threading
import subprocess

# Включение ANSI цветов на Windows консоли (CMD / PowerShell)
if sys.platform == "win32":
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass
    os.system("")

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"

DEFAULT_OMNI_PORTS = [20128, 8000, 3000, 8080, 5000, 8888, 7860]
DEFAULT_API_KEY = "sk-9e61b4bb8a128d44-5c456d-dc8deecf"

SMART_KEYWORDS = [
    "pro", "high", "opus", "sonnet", "deepseek-r1", "deepseek-v4",
    "kimi", "glm", "coder", "heavy", "claude-3-7", "claude-3-5",
    "gpt-4", "grok-4", "grok-build", "qwen-2.5-coder", "qwen-max"
]

FAST_KEYWORDS = [
    "flash", "haiku", "mini", "lite", "small", "turbo", "free",
    "gemini-3.8-flash", "fast", "8b", "7b", "3b"
]


class ModelPoolManager:
    """Управление активными моделями, пулом fallback и горячей заменой."""
    def __init__(self, config_file="active_pool.json"):
        self.config_file = config_file
        self.lock = threading.Lock()
        self.smart_model = ""
        self.light_model = ""
        self.smart_pool = []
        self.light_pool = []
        self.proxy_port = 9099
        self.proxy_url = "http://127.0.0.1:9099"
        self.api_key = ""
        self.failed_models = {}
        self.stats = {
            "total_requests": 0,
            "failed_requests": 0,
            "failovers_count": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        }

    def set_pools(self, smart_model, light_model, smart_pool, light_pool, proxy_port=9099, proxy_url="", api_key=""):
        with self.lock:
            self.smart_model = smart_model
            self.light_model = light_model
            self.smart_pool = smart_pool
            self.light_pool = light_pool
            if proxy_port:
                self.proxy_port = proxy_port
            if proxy_url:
                self.proxy_url = proxy_url
            if api_key:
                self.api_key = api_key
            self.save_to_file()

    def save_to_file(self):
        try:
            data = {
                "smart_model": self.smart_model,
                "light_model": self.light_model,
                "smart_pool": self.smart_pool,
                "light_pool": self.light_pool,
                "proxy_port": getattr(self, "proxy_port", 9099),
                "proxy_url": getattr(self, "proxy_url", f"http://127.0.0.1:{getattr(self, 'proxy_port', 9099)}"),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "Вы можете изменить smart_model или light_model прямо в этом файле — прокси применит изменения на лету!"
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def load_from_file_if_modified(self):
        if not os.path.exists(self.config_file):
            return
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self.lock:
                changed = False
                if data.get("smart_model") and data["smart_model"] != self.smart_model:
                    print(f"{Colors.YELLOW}⚡ [Hot-Swap] Смена Smart-модели: {self.smart_model} -> {data['smart_model']}{Colors.RESET}")
                    self.smart_model = data["smart_model"]
                    changed = True
                if data.get("light_model") and data["light_model"] != self.light_model:
                    print(f"{Colors.YELLOW}⚡ [Hot-Swap] Смена Light-модели: {self.light_model} -> {data['light_model']}{Colors.RESET}")
                    self.light_model = data["light_model"]
                    changed = True
                if "smart_pool" in data:
                    self.smart_pool = data["smart_pool"]
                if "light_pool" in data:
                    self.light_pool = data["light_pool"]
                if data.get("proxy_port"):
                    self.proxy_port = data["proxy_port"]
                if data.get("proxy_url"):
                    self.proxy_url = data["proxy_url"]
                if changed and hasattr(self, "proxy_url") and hasattr(self, "api_key") and self.proxy_url and self.api_key:
                    write_claude_launch_scripts(self.proxy_url, self.api_key, self.smart_model, self.light_model)
        except Exception:
            pass

    def get_model_for_role(self, role):
        self.load_from_file_if_modified()
        with self.lock:
            if role == "smart":
                return self.smart_model or (self.smart_pool[0] if self.smart_pool else "auto/best-free")
            else:
                return self.light_model or (self.light_pool[0] if self.light_pool else "auto/best-free")

    def report_failure_and_get_fallback(self, failed_model, role="smart"):
        with self.lock:
            now = time.time()
            self.failed_models[failed_model] = now
            self.stats["failovers_count"] += 1

            pool = self.smart_pool if role == "smart" else self.light_pool

            # 1. Ищем кандидата, который не падал в последние 3 минуты
            candidates = [m for m in pool if m != failed_model and (now - self.failed_models.get(m, 0)) > 180]

            # 2. Если в этом пуле все кандидаты падали, ищем во втором пуле
            if not candidates:
                other_pool = self.light_pool if role == "smart" else self.smart_pool
                candidates = [m for m in other_pool if m != failed_model and (now - self.failed_models.get(m, 0)) > 180]

            # 3. Если вообще все падали, берем любую модель из пула кроме текущей
            if not candidates:
                candidates = [m for m in pool if m != failed_model]

            if candidates:
                next_model = candidates[0]
                if role == "smart":
                    self.smart_model = next_model
                else:
                    self.light_model = next_model
                self.save_to_file()
                if hasattr(self, "proxy_url") and hasattr(self, "api_key") and self.proxy_url and self.api_key:
                    write_claude_launch_scripts(self.proxy_url, self.api_key, self.smart_model, self.light_model)
                return next_model
            return None


def format_tokens(num):
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M"
    if num >= 1_000:
        return f"{num / 1_000:.1f}K"
    return str(num)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def classify_request_role(body_json, pool_mgr):
    """
    Умное автоматическое распределение запросов:
    - Light: короткие вспомогательные задачи (саммари, коммиты, заголовки, мелкие вопросы, haiku)
    - Smart: кодинг, архитектура, системные задачи, большие файлы, diff, сложные вопросы
    """
    req_model = (body_json.get("model") or "").lower()

    # 1. Если в запросе явно указана легкая модель или haiku
    if "haiku" in req_model or "flash-lite" in req_model or "mini" in req_model:
        return "light"
    if pool_mgr.light_model and req_model == pool_mgr.light_model.lower() and pool_mgr.light_model != pool_mgr.smart_model:
        return "light"

    # 2. Короткие служебные запросы по лимиту max_tokens (Claude Code генерирует заголовки/коммиты с max_tokens 50-150)
    max_tokens = body_json.get("max_tokens", 4096)
    if max_tokens and max_tokens <= 150:
        return "light"

    messages = body_json.get("messages", [])
    if messages:
        # Извлекаем текст последнего пользовательского сообщения
        last_user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str):
                    last_user_text = c
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and "text" in part:
                            last_user_text += part["text"] + "\n"
                break

        last_lower = last_user_text.lower().strip()

        # Служебные быстрые команды Claude Code (саммари, коммит, заголовок, категория, статус)
        light_keywords = (
            "summarize", "commit message", "generate a title", "brief title",
            "one sentence", "category", "ping", "is this correct", "yes or no",
            "short summary", "git commit"
        )
        if any(kw in last_lower for kw in light_keywords) and len(last_user_text) < 600:
            return "light"

        # Если контекст крошечный (короткий вопрос без кода и файлов)
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        has_code_or_tools = any(kw in last_lower for kw in ("```", "def ", "class ", "function", "import ", "error", "patch", "diff"))
        if total_chars < 500 and not has_code_or_tools and len(messages) <= 2:
            return "light"

    # По умолчанию для глубокого анализа и написания кода -> Smart
    return "smart"


def create_proxy_handler(omni_base_url, omni_api_key, pool_mgr):
    class OmniProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            clean_path = urllib.parse.urlparse(self.path).path.rstrip('/')
            if clean_path in ("/v1/models", "/models"):
                self.handle_models_request()
            elif clean_path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            elif clean_path.startswith("/admin/switch"):
                self.handle_switch_command()
            elif clean_path.startswith("/admin/status"):
                self.handle_admin_status()
            else:
                self.forward_request("GET")

        def do_POST(self):
            clean_path = urllib.parse.urlparse(self.path).path.rstrip('/')
            if clean_path in ("/v1/messages", "/messages", "/v1/chat/completions", "/chat/completions", "/v1/complete"):
                self.handle_messages_request()
            else:
                self.forward_request("POST")

        def handle_admin_status(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            data = {
                "smart_model": pool_mgr.smart_model,
                "light_model": pool_mgr.light_model,
                "smart_pool": pool_mgr.smart_pool,
                "light_pool": pool_mgr.light_pool,
                "stats": pool_mgr.stats
            }
            self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

        def handle_switch_command(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            role = params.get("role", ["smart"])[0]
            model = params.get("model", [""])[0]
            if model:
                old_model = pool_mgr.smart_model if role == "smart" else pool_mgr.light_model
                if role == "smart":
                    pool_mgr.smart_model = model
                else:
                    pool_mgr.light_model = model
                pool_mgr.save_to_file()
                write_claude_launch_scripts(
                    pool_mgr.proxy_url, omni_api_key, pool_mgr.smart_model, pool_mgr.light_model
                )
                msg = f"OK: {role} model switched from {old_model} to {model}"
                print(f"{Colors.GREEN}⚡ [Hot-Swap Switch] {role.capitalize()}-модель переключена: {old_model} -> {model}{Colors.RESET}")
            else:
                msg = "Error: model param required"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode("utf-8"))

        def handle_models_request(self):
            seen = set()
            models_data = {"object": "list", "data": []}
            def add_m(m_id, owned="omniroute"):
                if m_id and m_id not in seen:
                    seen.add(m_id)
                    models_data["data"].append({"id": m_id, "object": "model", "owned_by": owned})

            add_m(pool_mgr.smart_model)
            add_m(pool_mgr.light_model)
            add_m("auto/best-free")
            for m in pool_mgr.smart_pool:
                add_m(m)
            for m in pool_mgr.light_pool:
                add_m(m)
            for m in ["claude-3-7-sonnet-20250219", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"]:
                add_m(m, owned="anthropic")

            body = json.dumps(models_data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_messages_request(self):
            start_time = time.time()
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)

            try:
                body_json = json.loads(raw_body.decode("utf-8"))
            except Exception:
                body_json = {}

            # Автоматическое определение роли запроса (Smart vs Light)
            role = classify_request_role(body_json, pool_mgr)
            target_model = pool_mgr.get_model_for_role(role)
            is_stream = body_json.get("stream", False)

            max_attempts = 4
            current_target = target_model
            last_err = ""

            for attempt in range(max_attempts):
                if not current_target:
                    break

                body_json["model"] = current_target
                new_body = json.dumps(body_json).encode("utf-8")

                try:
                    resp_ok, status, headers, in_tok, out_tok, err_detail = self.send_to_upstream(
                        self.command, self.path, new_body, is_stream
                    )

                    if resp_ok and status < 400:
                        elapsed_ms = int((time.time() - start_time) * 1000)
                        now_str = time.strftime("%H:%M:%S")

                        # Логирование со значком роли для прозрачности:
                        role_tag = f"{Colors.MAGENTA}[🧠 SMART]{Colors.RESET}" if role == "smart" else f"{Colors.CYAN}[⚡ LIGHT]{Colors.RESET}"
                        tok_str = f"{format_tokens(in_tok)}↑ {format_tokens(out_tok)}↓"
                        print(f"{Colors.GRAY}{now_str}{Colors.RESET} | {role_tag} {Colors.BOLD}{current_target:<28}{Colors.RESET} | {Colors.GREEN}{tok_str:>13}{Colors.RESET} | {Colors.YELLOW}{elapsed_ms:>6}ms{Colors.RESET}")

                        with pool_mgr.lock:
                            pool_mgr.stats["total_requests"] += 1
                            pool_mgr.stats["total_input_tokens"] += in_tok
                            pool_mgr.stats["total_output_tokens"] += out_tok
                        return

                    else:
                        last_err = err_detail or f"HTTP {status}"
                        fallback = pool_mgr.report_failure_and_get_fallback(current_target, role)
                        now_str = time.strftime("%H:%M:%S")
                        print(f"{Colors.RED}⚠️ [{now_str}] Модель '{current_target}' вернула ошибку ({last_err})! Авто-замена на '{fallback}'...{Colors.RESET}")
                        if not fallback:
                            break
                        current_target = fallback

                except Exception as e:
                    last_err = str(e)
                    fallback = pool_mgr.report_failure_and_get_fallback(current_target, role)
                    now_str = time.strftime("%H:%M:%S")
                    print(f"{Colors.RED}⚠️ [{now_str}] Ошибка связи с '{current_target}': {e}! Авто-замена на '{fallback}'...{Colors.RESET}")
                    if not fallback:
                        break
                    current_target = fallback

            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            err_payload = json.dumps({
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"OmniRoute failover exhausted. Last error: {last_err}"
                }
            }).encode("utf-8")
            self.wfile.write(err_payload)

        def send_to_upstream(self, method, path, body, is_stream):
            target_url = f"{omni_base_url.rstrip('/')}{path}"
            req = urllib.request.Request(target_url, data=body, method=method)

            for k, v in self.headers.items():
                if k.lower() not in ("host", "content-length"):
                    req.add_header(k, v)

            if omni_api_key:
                req.add_header("Authorization", f"Bearer {omni_api_key}")
                req.add_header("x-api-key", omni_api_key)
            req.add_header("anthropic-version", "2023-06-01")
            req.add_header("Host", urllib.parse.urlparse(omni_base_url).netloc)

            try:
                upstream_resp = urllib.request.urlopen(req, timeout=35)
                status = upstream_resp.status
                headers = dict(upstream_resp.headers)
            except urllib.error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")[:120]
                except Exception:
                    pass
                return False, e.code, dict(e.headers), 0, 0, f"HTTP {e.code}: {err_body}"
            except Exception as e:
                return False, 504, {}, 0, 0, str(e)

            in_tokens = 0
            out_tokens = 0

            try:
                req_json = json.loads(body.decode("utf-8"))
                for msg in req_json.get("messages", []):
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        in_tokens += max(1, len(content) // 4)
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and "text" in c:
                                in_tokens += max(1, len(c["text"]) // 4)
                if in_tokens == 0:
                    in_tokens = len(body) // 4
            except Exception:
                in_tokens = len(body) // 4

            if is_stream or "text/event-stream" in headers.get("Content-Type", ""):
                # Читаем первый чанк ДО отправки заголовков клиенту!
                # Если стрим вернул ошибку, клиент (Claude Code) не крашится, а происходит автозамена
                first_chunk = upstream_resp.readline()
                if not first_chunk:
                    return False, 502, {}, 0, 0, "Пустой ответ стрима"

                first_line = first_chunk.decode("utf-8", errors="replace")
                if "error" in first_line.lower() and ('"type": "error"' in first_line or '"error":' in first_line):
                    return False, 500, {}, 0, 0, first_line.strip()

                self.send_response(status)
                for k, v in headers.items():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()

                self.wfile.write(first_chunk)
                self.wfile.flush()

                buffer = first_line
                while True:
                    chunk = upstream_resp.readline()
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break

                    line = chunk.decode("utf-8", errors="replace")
                    buffer += line

                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str and data_str != "[DONE]":
                            try:
                                d = json.loads(data_str)
                                if d.get("type") == "message_start":
                                    usage = d.get("message", {}).get("usage", {})
                                    if usage.get("input_tokens"):
                                        in_tokens = usage["input_tokens"]
                                elif d.get("type") == "message_delta":
                                    usage = d.get("usage", {})
                                    if usage.get("output_tokens"):
                                        out_tokens = usage["output_tokens"]
                                if "usage" in d and d["usage"]:
                                    in_tokens = d["usage"].get("prompt_tokens", in_tokens)
                                    out_tokens = d["usage"].get("completion_tokens", out_tokens)
                            except Exception:
                                pass

                if out_tokens == 0:
                    out_tokens = max(1, len(buffer) // 4)

                return True, status, headers, in_tokens, out_tokens, ""

            else:
                content = upstream_resp.read()
                try:
                    resp_json = json.loads(content.decode("utf-8"))
                    if "error" in resp_json:
                        err_msg = resp_json["error"].get("message", "Unknown error")
                        return False, 500, {}, 0, 0, f"Error in JSON: {err_msg}"
                except Exception:
                    pass

                self.send_response(status)
                for k, v in headers.items():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

                try:
                    resp_json = json.loads(content.decode("utf-8"))
                    usage = resp_json.get("usage", {})
                    if usage:
                        in_tokens = usage.get("input_tokens", usage.get("prompt_tokens", in_tokens))
                        out_tokens = usage.get("output_tokens", usage.get("completion_tokens", out_tokens))
                except Exception:
                    out_tokens = max(1, len(content) // 4)

                return True, status, headers, in_tokens, out_tokens, ""

        def forward_request(self, method):
            target_url = f"{omni_base_url.rstrip('/')}{self.path}"
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else None

            req = urllib.request.Request(target_url, data=body, method=method)
            for k, v in self.headers.items():
                if k.lower() not in ("host", "content-length"):
                    req.add_header(k, v)
            if omni_api_key:
                req.add_header("Authorization", f"Bearer {omni_api_key}")
                req.add_header("x-api-key", omni_api_key)

            try:
                resp = urllib.request.urlopen(req, timeout=30)
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(resp.read())
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                for k, v in e.headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(e.read())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode("utf-8"))

    return OmniProxyHandler


def find_omniroute_base_url(custom_url=None, api_key=""):
    if custom_url:
        return custom_url

    env_url = os.environ.get("OMNIROUTE_URL")
    if env_url:
        return env_url

    print(f"{Colors.BLUE}🔍 Поиск работающего OmniRoute на портах...{Colors.RESET}")
    for port in DEFAULT_OMNI_PORTS:
        url = f"http://127.0.0.1:{port}"
        try:
            headers = {"User-Agent": "omni-checker"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
                headers["x-api-key"] = api_key
            req = urllib.request.Request(f"{url}/v1/models", headers=headers)
            with urllib.request.urlopen(req, timeout=1.8) as r:
                if r.status == 200:
                    print(f"{Colors.GREEN}✅ OmniRoute обнаружен на {url}{Colors.RESET}")
                    return url
        except urllib.error.HTTPError as e:
            if e.code in (401, 200) or any("omniroute" in h.lower() for h in e.headers.keys()):
                print(f"{Colors.GREEN}✅ OmniRoute обнаружен на {url} (статус {e.code}){Colors.RESET}")
                return url
        except Exception:
            continue

    return "http://127.0.0.1:20128"


NON_CHAT_KEYWORDS = ('embed', 'tts', 'audio', 'image', 'video', 'whisper', 'dall-e', 'realtime')


def pick_canaries_for_provider(models):
    """Выбирает до 2 легковесных чат-моделей в качестве канареек для быстрой проверки провайдера."""
    chat_models = [m for m in models if not any(k in m.lower() for k in NON_CHAT_KEYWORDS)]
    if not chat_models:
        chat_models = models
    # Приоритет: легкие/быстрые модели первыми
    sorted_m = sorted(chat_models, key=lambda x: (
        0 if any(k in x.lower() for k in ('free', 'flash', 'mini', 'lite', 'low', 'chat', 'fast')) else 1,
        len(x)
    ))
    return sorted_m[:2]


def probe_canary_model(omni_base_url, model_id, api_key=""):
    """Быстрый зондирующий запрос к модели-канарейке."""
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01"
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    payload = json.dumps({
        "model": model_id,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}]
    }).encode("utf-8")

    t0 = time.time()
    try:
        req = urllib.request.Request(
            f"{omni_base_url.rstrip('/')}/v1/messages",
            data=payload,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5.0) as r:
            ms = int((time.time() - t0) * 1000)
            return True, ms, ""
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            try:
                req_oai = urllib.request.Request(
                    f"{omni_base_url.rstrip('/')}/v1/chat/completions",
                    data=payload,
                    headers=headers,
                    method="POST"
                )
                with urllib.request.urlopen(req_oai, timeout=5.0) as r2:
                    ms = int((time.time() - t0) * 1000)
                    return True, ms, ""
            except Exception as e2:
                ms = int((time.time() - t0) * 1000)
                return False, ms, str(e2)
        ms = int((time.time() - t0) * 1000)
        return False, ms, f"HTTP {e.code}"
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        return False, ms, str(e)


def dynamic_canary_provider_scan(omni_base_url, all_models, api_key=""):
    """
    ДВУХФАЗНОЕ ДИНАМИЧЕСКОЕ ЗОНДИРОВАНИЕ (Canary Probing):
    Группирует модели по провайдерам, тестирует по 1-2 канарейки от каждого
    и динамически отсекает неактивные/сломанные провайдеры за 3 секунды без всякого хардкода.
    """
    providers = {}
    for m in all_models:
        prov = m.split("/")[0] if "/" in m else "direct"
        providers.setdefault(prov, []).append(m)

    total_models = len(all_models)
    total_provs = len(providers)

    print(f"\n{Colors.BOLD}{Colors.CYAN}🔍 В реестре обнаружено {total_models} моделей в {total_provs} провайдерах.{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.YELLOW}⚡ Фаза 1: Динамическая экспресс-разведка провайдеров (Canary Probe)...{Colors.RESET}")

    canaries_by_prov = {p: pick_canaries_for_provider(m_list) for p, m_list in providers.items()}

    def test_provider(prov):
        canaries = canaries_by_prov[prov]
        last_err = ""
        for c in canaries:
            ok, ms, err = probe_canary_model(omni_base_url, c, api_key)
            if ok:
                return prov, True, ms, c
            last_err = err
        return prov, False, 0, last_err

    alive_providers = []
    dead_providers = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(24, max(4, total_provs))) as executor:
        futures = {executor.submit(test_provider, p): p for p in providers.keys()}
        for future in concurrent.futures.as_completed(futures):
            prov, ok, ms, info = future.result()
            count = len(providers[prov])
            if ok:
                alive_providers.append((prov, count, ms, info))
                print(f"  {Colors.GREEN}✅ {prov:<18}{Colors.RESET} -> {Colors.GREEN}АКТИВЕН{Colors.RESET} ({count} моделей, {ms}ms)")
            else:
                dead_providers.append((prov, count, info))
                print(f"  {Colors.GRAY}❌ {prov:<18}{Colors.RESET} -> {Colors.RED}НЕ АКТИВЕН{Colors.RESET} {Colors.GRAY}({info}) [{count} мод. отсеяно]{Colors.RESET}")

    active_models = []
    for prov, _, _, _ in alive_providers:
        active_models.extend(providers[prov])

    # Если провайдер auto активен, гарантируем ключевые смарт-маршруты
    if any(p[0] == "auto" for p in alive_providers):
        for v in ["auto/best-free", "auto/best-fast", "auto/best-coding", "gemini-3.8-flash-medium"]:
            if v not in active_models:
                active_models.append(v)

    skipped = total_models - len(active_models)
    print(f"\n{Colors.BOLD}{Colors.GREEN}🎯 Разведка завершена: активно провайдеров {len(alive_providers)}/{total_provs}.{Colors.RESET}")
    print(f"   Готовы к глубокому тесту: {Colors.CYAN}{len(active_models)}{Colors.RESET} моделей (динамически отсеяно: {Colors.GRAY}{skipped}{Colors.RESET})\n")

    return sorted(list(set(active_models)))


def fetch_all_models(omni_base_url, api_key="", test_all=False):
    url = f"{omni_base_url.rstrip('/')}/v1/models"
    headers = {"User-Agent": "omni-checker"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
            models = []
            if "data" in data and isinstance(data["data"], list):
                for m in data["data"]:
                    m_id = m.get("id") if isinstance(m, dict) else m
                    if not m_id:
                        continue
                    models.append(m_id)

            for virtual_m in ["auto/best-free", "auto/best-fast", "auto/best-coding", "gemini-3.8-flash-medium", "grok-cli/grok-4.7-xhigh"]:
                if virtual_m not in models:
                    models.insert(0, virtual_m)

            if test_all:
                return sorted(list(set(models)))

            # Динамическое двухфазное зондирование: никаких зашитых имен!
            return dynamic_canary_provider_scan(omni_base_url, models, api_key)

    except Exception as e:
        print(f"{Colors.RED}❌ Не удалось получить список моделей из {url}: {e}{Colors.RESET}")
        return ["auto/best-free", "gemini-3.8-flash-medium", "grok-cli/grok-4.7-xhigh", "ddgw/gpt-5.6-luna", "gemini/gemini-3.1-flash-lite"]


def test_single_model(omni_base_url, model_id, api_key=""):
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01"
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    anthropic_payload = json.dumps({
        "model": model_id,
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "ping"}]
    }).encode("utf-8")

    t0 = time.time()
    try:
        req = urllib.request.Request(
            f"{omni_base_url.rstrip('/')}/v1/messages",
            data=anthropic_payload,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=8.0) as r:
            latency = int((time.time() - t0) * 1000)
            if r.status == 200:
                resolved_model = r.headers.get("x-omniroute-model", "")
                return (model_id, True, latency, "", resolved_model)
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            openai_payload = json.dumps({
                "model": model_id,
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "ping"}]
            }).encode("utf-8")
            try:
                t0_oa = time.time()
                req_oa = urllib.request.Request(
                    f"{omni_base_url.rstrip('/')}/v1/chat/completions",
                    data=openai_payload,
                    headers=headers,
                    method="POST"
                )
                with urllib.request.urlopen(req_oa, timeout=8.0) as r_oa:
                    latency = int((time.time() - t0_oa) * 1000)
                    if r_oa.status == 200:
                        resolved_model = r_oa.headers.get("x-omniroute-model", "")
                        return (model_id, True, latency, "", resolved_model)
            except Exception as ex:
                return (model_id, False, 0, f"HTTP {getattr(ex, 'code', 'Error')}", "")
        err_msg = f"HTTP {e.code}"
        return (model_id, False, 0, err_msg, "")
    except Exception:
        return (model_id, False, 0, "Таймаут/Сбой", "")


def benchmark_and_filter_models(omni_base_url, models, api_key=""):
    print(f"\n{Colors.BOLD}{Colors.CYAN}🚀 Запуск проверки доступных моделей ({len(models)} шт.)...{Colors.RESET}\n")
    working_models = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_model = {
            executor.submit(test_single_model, omni_base_url, m, api_key): m
            for m in models
        }
        for future in concurrent.futures.as_completed(future_to_model):
            model_id, ok, latency, err, resolved = future.result()
            if ok:
                working_models.append((model_id, latency, resolved))
                extra = f" [-> {resolved}]" if resolved else ""
                print(f"{model_id:<43} -> {Colors.GREEN}✅ РАБОТАЕТ{Colors.RESET} ({latency}ms){Colors.GRAY}{extra}{Colors.RESET}")
            else:
                print(f"{model_id:<43} -> {Colors.RED}❌ ОШИБКА{Colors.RESET} {Colors.GRAY}({err}){Colors.RESET}")

    # Сортируем рабочие модели по задержке
    working_models.sort(key=lambda x: x[1])
    return working_models


def print_working_models_table(working_models):
    """Выводит красивую нумерованную таблицу всех работающих моделей."""
    print(f"\n{Colors.BOLD}{Colors.GREEN}========================================================================================{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}   📋 СПИСОК РАБОТАЮЩИХ МОДЕЛЕЙ (Всего найдено: {len(working_models)}){Colors.RESET}")
    print(f"{Colors.GREEN}----------------------------------------------------------------------------------------{Colors.RESET}")
    print(f" {Colors.BOLD}№   | Идентификатор модели                        | Задержка | Тип / Роль{Colors.RESET}")
    print(f"-----+---------------------------------------------+----------+-------------------------")

    for idx, item in enumerate(working_models, 1):
        m_id, latency = item[0], item[1]
        resolved = item[2] if len(item) > 2 else ""
        lower = m_id.lower()
        if "pro" in lower or "high" in lower or "coder" in lower or "r1" in lower or "opus" in lower or "sonnet" in lower:
            tag = f"{Colors.MAGENTA}[🧠 Smart / Code]{Colors.RESET}"
        elif "flash" in lower or "haiku" in lower or "mini" in lower or "lite" in lower:
            tag = f"{Colors.CYAN}[⚡ Fast / Light]{Colors.RESET}"
        elif m_id.startswith("auto/") or m_id.startswith("combo/"):
            res_str = f" -> {resolved}" if resolved else ""
            tag = f"{Colors.YELLOW}[🔄 Auto Route{res_str}]{Colors.RESET}"
        else:
            tag = f"{Colors.GRAY}[Базовая]{Colors.RESET}"

        print(f" {idx:<3} | {m_id:<43} | {latency:>6}ms | {tag}")

    print(f"{Colors.GREEN}========================================================================================{Colors.RESET}\n")


def explain_auto_routes():
    """Разъясняет особенности моделей auto/best-free и как они устроены."""
    print(f"{Colors.BOLD}{Colors.YELLOW}ℹ️  ОБРАТИТЕ ВНИМАНИЕ НА МОДЕЛИ СЕМЕЙСТВА 'auto/*' (например, auto/best-free):{Colors.RESET}")
    print(f" • {Colors.CYAN}auto/best-free{Colors.RESET} — это виртуальный маршрут OmniRoute. Он динамически")
    print(f"   перенаправляет запрос на самый быстрый и стабильный бесплатный провайдер")
    print(f"   (сейчас OmniRoute резолвит его в {Colors.GREEN}gemini-3.1-flash-lite{Colors.RESET} от Google/Gemini).")
    print(f" • {Colors.BOLD}Можно ли их настроить?{Colors.RESET}")
    print(f"   1) Внутри самого OmniRoute настраиваются комбо-маршруты:")
    print(f"      {Colors.GRAY}omniroute combo create <имя> --models model1,model2 --strategy priority{Colors.RESET}")
    print(f"   2) А прямо в меню ниже вы можете выбрать конкретную проверенную модель (например,")
    print(f"      {Colors.GREEN}agy/gemini-3.8-flash-high{Colors.RESET}), чтобы OmniRoute вызывал именно её без сюрпризов.\n")


def interactive_model_selection(working_models, default_smart, default_light):
    """Интерактивное меню выбора моделей."""
    print_working_models_table(working_models)
    explain_auto_routes()

    print(f"{Colors.BOLD}{Colors.CYAN}🎯 ВЫБОР ОСНОВНЫХ МОДЕЛЕЙ ДЛЯ CLAUDE CODE:{Colors.RESET}")
    print(f"   Рекомендуемая связка:")
    print(f"   🧠 Smart Model (для кодинга, логики и архитектуры): {Colors.GREEN}{default_smart}{Colors.RESET}")
    print(f"   ⚡ Light Model (быстрая, для поиска и мелких правок): {Colors.CYAN}{default_light}{Colors.RESET}\n")

    print(f"   {Colors.BOLD}[Enter]{Colors.RESET} — применить рекомендуемую связку ({default_smart} + {default_light})")
    print(f"   {Colors.BOLD}[1-{len(working_models)}]{Colors.RESET} — ввести номер модели для Smart (основной)")
    print(f"   {Colors.BOLD}[m]{Colors.RESET}     — выбрать вручную по номерам обе модели\n")

    model_map = {idx: m[0] for idx, m in enumerate(working_models, 1)}

    try:
        choice = input(f"{Colors.BOLD}Ваш выбор [Enter / номер / m]: {Colors.RESET}").strip()
    except (EOFError, KeyboardInterrupt):
        choice = ""

    smart_model = default_smart
    light_model = default_light

    if not choice:
        pass  # Рекомендуемые
    elif choice.isdigit() and int(choice) in model_map:
        smart_model = model_map[int(choice)]
        print(f"   ✅ Smart модель: {Colors.GREEN}{smart_model}{Colors.RESET}")
        try:
            l_in = input(f"   Номер для Light модели [Enter для {default_light}]: ").strip()
            if l_in.isdigit() and int(l_in) in model_map:
                light_model = model_map[int(l_in)]
        except Exception:
            pass
    elif choice.lower() in ("m", "manual", "ь"):
        try:
            s_in = input(f"   Введите номер для Smart модели (основная): ").strip()
            if s_in.isdigit() and int(s_in) in model_map:
                smart_model = model_map[int(s_in)]
            l_in = input(f"   Введите номер для Light модели (быстрая): ").strip()
            if l_in.isdigit() and int(l_in) in model_map:
                light_model = model_map[int(l_in)]
        except Exception:
            pass
    else:
        for m in working_models:
            if choice.lower() in m[0].lower():
                smart_model = m[0]
                break

    smart_pool = [smart_model] + [m[0] for m in working_models if m[0] != smart_model]
    light_pool = [light_model] + [m[0] for m in working_models if m[0] != light_model]

    print(f"\n{Colors.BOLD}{Colors.GREEN}========================================================================{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}🚀 ВЫБРАННЫЕ МОДЕЛИ СРАЗУ СТАЛИ ОСНОВНЫМИ:{Colors.RESET}")
    print(f"   🧠 Smart (основная) : {Colors.GREEN}{smart_model}{Colors.RESET}")
    print(f"   ⚡ Light (быстрая)  : {Colors.CYAN}{light_model}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}========================================================================{Colors.RESET}\n")

    return smart_model, light_model, smart_pool, light_pool


def write_claude_launch_scripts(proxy_url, api_key, smart_model, light_model):
    """Создает/обновляет готовые файлы запуска Claude Code во втором окне."""
    # 1. run_claude.sh (macOS / Linux)
    script_sh = "run_claude.sh"
    sh_content = f"""#!/bin/bash
DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
cd "$DIR"

ACTIVE_SMART="{smart_model}"
ACTIVE_LIGHT="{light_model}"
ACTIVE_URL="{proxy_url}"

# Подтягиваем актуальную модель из active_pool.json если файл обновлялся
if [ -f "$DIR/active_pool.json" ]; then
    D_SMART=$(python3 -c "import json; print(json.load(open('$DIR/active_pool.json')).get('smart_model',''))" 2>/dev/null)
    D_LIGHT=$(python3 -c "import json; print(json.load(open('$DIR/active_pool.json')).get('light_model',''))" 2>/dev/null)
    D_URL=$(python3 -c "import json; print(json.load(open('$DIR/active_pool.json')).get('proxy_url',''))" 2>/dev/null)
    [ -n "$D_SMART" ] && ACTIVE_SMART="$D_SMART"
    [ -n "$D_LIGHT" ] && ACTIVE_LIGHT="$D_LIGHT"
    [ -n "$D_URL" ] && ACTIVE_URL="$D_URL"
fi

export ANTHROPIC_BASE_URL="$ACTIVE_URL"
export ANTHROPIC_AUTH_TOKEN="{api_key}"
export ANTHROPIC_API_KEY="{api_key}"
export ANTHROPIC_MODEL="$ACTIVE_SMART"
export ANTHROPIC_SMALL_MODEL="$ACTIVE_LIGHT"
export ANTHROPIC_SMALL_FAST_MODEL="$ACTIVE_LIGHT"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ACTIVE_SMART"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ACTIVE_SMART"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ACTIVE_LIGHT"

echo ""
echo "=========================================================================="
echo "🚀 Claude Code запущен через OmniRoute Failover Proxy"
echo "🧠 Smart-модель : $ACTIVE_SMART"
echo "⚡ Light-модель : $ACTIVE_LIGHT"
echo "🔗 Прокси       : $ACTIVE_URL"
echo "=========================================================================="
echo ""

# Флаг --model передается явно, чтобы Claude Code не брал старую закешированную модель
exec claude --model "$ACTIVE_SMART" "$@"
"""
    with open(script_sh, "w", encoding="utf-8") as f:
        f.write(sh_content)
    try:
        os.chmod(script_sh, 0o755)
    except Exception:
        pass

    # 2. run_claude.bat (Windows)
    script_bat = "run_claude.bat"
    bat_content = f"""@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

set "ACTIVE_SMART={smart_model}"
set "ACTIVE_LIGHT={light_model}"
set "ACTIVE_URL={proxy_url}"

if exist "%~dp0active_pool.json" (
    for /f "delims=" %%i in ('python -c "import json; print(json.load(open('active_pool.json')).get('smart_model',''))" 2^>nul') do if not "%%i"=="" set "ACTIVE_SMART=%%i"
    for /f "delims=" %%i in ('python -c "import json; print(json.load(open('active_pool.json')).get('light_model',''))" 2^>nul') do if not "%%i"=="" set "ACTIVE_LIGHT=%%i"
    for /f "delims=" %%i in ('python -c "import json; print(json.load(open('active_pool.json')).get('proxy_url',''))" 2^>nul') do if not "%%i"=="" set "ACTIVE_URL=%%i"
)

set ANTHROPIC_BASE_URL=!ACTIVE_URL!
set ANTHROPIC_AUTH_TOKEN={api_key}
set ANTHROPIC_API_KEY={api_key}
set ANTHROPIC_MODEL=!ACTIVE_SMART!
set ANTHROPIC_SMALL_MODEL=!ACTIVE_LIGHT!
set ANTHROPIC_SMALL_FAST_MODEL=!ACTIVE_LIGHT!
set ANTHROPIC_DEFAULT_OPUS_MODEL=!ACTIVE_SMART!
set ANTHROPIC_DEFAULT_SONNET_MODEL=!ACTIVE_SMART!
set ANTHROPIC_DEFAULT_HAIKU_MODEL=!ACTIVE_LIGHT!

echo.
echo ==========================================================================
echo 🚀 Claude Code zapuschen cherez OmniRoute Failover Proxy
echo 🧠 Aktivnaya Smart-model : !ACTIVE_SMART!
echo ⚡ Aktivnaya Light-model : !ACTIVE_LIGHT!
echo 🔗 Proxy-adres          : !ACTIVE_URL!
echo ==========================================================================
echo.

claude --model "!ACTIVE_SMART!" %*
"""
    with open(script_bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(bat_content)

    # 3. run_claude.ps1 (PowerShell)
    script_ps1 = "run_claude.ps1"
    ps1_content = f"""$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$activeSmart = "{smart_model}"
$activeLight = "{light_model}"
$activeUrl = "{proxy_url}"

if (Test-Path "$scriptDir\\active_pool.json") {{
    try {{
        $data = Get-Content "$scriptDir\\active_pool.json" -Raw | ConvertFrom-Json
        if ($data.smart_model) {{ $activeSmart = $data.smart_model }}
        if ($data.light_model) {{ $activeLight = $data.light_model }}
        if ($data.proxy_url) {{ $activeUrl = $data.proxy_url }}
    }} catch {{}}
}}

$env:ANTHROPIC_BASE_URL = $activeUrl
$env:ANTHROPIC_AUTH_TOKEN = "{api_key}"
$env:ANTHROPIC_API_KEY = "{api_key}"
$env:ANTHROPIC_MODEL = $activeSmart
$env:ANTHROPIC_SMALL_MODEL = $activeLight
$env:ANTHROPIC_SMALL_FAST_MODEL = $activeLight
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = $activeSmart
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $activeSmart
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = $activeLight

Write-Host "`n==========================================================================" -ForegroundColor Cyan
Write-Host "🚀 Claude Code запущен через OmniRoute Failover Proxy" -ForegroundColor Green
Write-Host "🧠 Smart-модель : $activeSmart" -ForegroundColor Yellow
Write-Host "⚡ Light-модель : $activeLight" -ForegroundColor Yellow
Write-Host "🔗 Прокси       : $activeUrl" -ForegroundColor Gray
Write-Host "==========================================================================`n" -ForegroundColor Cyan

claude --model "$activeSmart" @args
"""
    with open(script_ps1, "w", encoding="utf-8") as f:
        f.write(ps1_content)

    # 4. Синхронизируем глобальные настройки Claude Code (~/.claude/settings.json)
    sync_claude_settings(proxy_url, api_key, smart_model, light_model)


def sync_claude_settings(proxy_url, api_key, smart_model, light_model):
    """
    КРИТИЧЕСКИ ВАЖНО: Claude Code считывает ~/.claude/settings.json при каждом запуске!
    Если там прописан http://localhost:20128 или старая модель, Claude Code игнорирует всё
    и идёт напрямую в обход нашего прокси.
    Эта функция гарантирует, что Claude Code направляет все запросы именно в наш прокси.
    """
    settings_paths = []
    home = os.path.expanduser("~")
    settings_paths.append(os.path.join(home, ".claude", "settings.json"))

    if os.name == "nt" or "USERPROFILE" in os.environ:
        win_home = os.environ.get("USERPROFILE", home)
        settings_paths.append(os.path.join(win_home, ".claude", "settings.json"))

    for path in set(settings_paths):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = {}
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}

            if "env" not in data or not isinstance(data["env"], dict):
                data["env"] = {}

            data["env"]["ANTHROPIC_BASE_URL"] = proxy_url
            if api_key:
                data["env"]["ANTHROPIC_AUTH_TOKEN"] = api_key
                data["env"]["ANTHROPIC_API_KEY"] = api_key
            data["env"]["ANTHROPIC_MODEL"] = smart_model
            data["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = smart_model
            data["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = smart_model
            data["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = light_model
            data["env"]["ANTHROPIC_SMALL_FAST_MODEL"] = light_model
            data["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "1000000"
            data["env"]["CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"] = "1"
            data["model"] = smart_model

            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def select_best_model_combination(working_models):
    if not working_models:
        return None, None, [], []

    smart_candidates = []
    light_candidates = []

    for item in working_models:
        model_id, latency = item[0], item[1]
        lower = model_id.lower()
        is_fast = any(kw in lower for kw in FAST_KEYWORDS)
        is_smart = any(kw in lower for kw in SMART_KEYWORDS)

        if is_fast and not is_smart:
            light_candidates.append((model_id, latency))
        elif is_smart:
            smart_candidates.append((model_id, latency))
        else:
            if latency < 3000:
                light_candidates.append((model_id, latency))
            else:
                smart_candidates.append((model_id, latency))

    light_candidates.sort(key=lambda x: x[1])
    smart_candidates.sort(key=lambda x: x[1])

    smart_pool = [m[0] for m in smart_candidates]
    light_pool = [m[0] for m in light_candidates]

    if not smart_pool and light_pool:
        smart_pool = light_pool.copy()
    if not light_pool and smart_pool:
        light_pool = smart_pool.copy()

    best_smart = smart_pool[0] if smart_pool else "auto/best-free"
    best_light = light_pool[0] if light_pool else "auto/best-free"

    return best_smart, best_light, smart_pool, light_pool


def find_free_port(start_port=9099):
    for port in range(start_port, start_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start_port


def get_active_proxy_port(default_port=9099):
    if os.path.exists("active_pool.json"):
        try:
            with open("active_pool.json", "r", encoding="utf-8") as f:
                d = json.load(f)
                return d.get("proxy_port", default_port)
        except Exception:
            pass
    return default_port


def cmd_switch(role, model, proxy_port=None):
    if proxy_port is None:
        proxy_port = get_active_proxy_port()
    url = f"http://127.0.0.1:{proxy_port}/admin/switch?role={urllib.parse.quote(role)}&model={urllib.parse.quote(model)}"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            print(f"{Colors.GREEN}✅ {r.read().decode('utf-8')}{Colors.RESET}")
    except Exception as e:
        print(f"{Colors.RED}❌ Не удалось отправить команду прокси на порту {proxy_port}: {e}{Colors.RESET}")


def cmd_status(proxy_port=None):
    if proxy_port is None:
        proxy_port = get_active_proxy_port()
    url = f"http://127.0.0.1:{proxy_port}/admin/status"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
            print(f"\n{Colors.BOLD}{Colors.CYAN}--- ТЕКУЩИЙ СТАТУС ПУЛА МОДЕЛЕЙ (Порт {proxy_port}) ---{Colors.RESET}")
            print(f"🧠 Smart Model: {Colors.GREEN}{data.get('smart_model')}{Colors.RESET}")
            print(f"⚡ Light Model: {Colors.CYAN}{data.get('light_model')}{Colors.RESET}")
            print(f"📋 Smart Pool:  {', '.join(data.get('smart_pool', []))}")
            print(f"📋 Light Pool:  {', '.join(data.get('light_pool', []))}")
            st = data.get("stats", {})
            print(f"\n{Colors.YELLOW}📊 Статистика сессии:{Colors.RESET}")
            print(f"   Запросов: {st.get('total_requests')}")
            print(f"   Авто-переключений (Failovers): {st.get('failovers_count')}")
            print(f"   Входных токенов:  {format_tokens(st.get('total_input_tokens', 0))}")
            print(f"   Выходных токенов: {format_tokens(st.get('total_output_tokens', 0))}\n")
    except Exception as e:
        print(f"{Colors.RED}❌ Прокси не отвечает на порту {proxy_port}: {e}{Colors.RESET}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "switch":
        if len(sys.argv) >= 4:
            cmd_switch(sys.argv[2], sys.argv[3])
        else:
            print("Использование: python omni_claude.py switch [smart|light] [имя_модели]")
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        cmd_status()
        sys.exit(0)

    import argparse
    parser = argparse.ArgumentParser(description="OmniRoute + Claude Code Smart Orchestrator & Auto-Failover Proxy")
    parser.add_argument("--omni-url", default="", help="URL OmniRoute (по умолчанию автоопределение)")
    parser.add_argument("--api-key", default=os.environ.get("OMNIROUTE_API_KEY", os.environ.get("ANTHROPIC_AUTH_TOKEN", DEFAULT_API_KEY)), help="OmniRoute API Key")
    parser.add_argument("--proxy-port", type=int, default=9099, help="Порт локального прокси")
    parser.add_argument("--skip-check", action="store_true", help="Пропустить тестирование моделей")
    parser.add_argument("--test-only", action="store_true", help="Только протестировать модели и выйти")
    parser.add_argument("--all", action="store_true", help="Протестировать абсолютно все 500+ моделей OmniRoute")
    args, unknown_claude_args = parser.parse_known_args()

    print(f"\n{Colors.BOLD}{Colors.MAGENTA}==================================================={Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.MAGENTA}       OmniRoute + Claude Code Smart Proxy          {Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.MAGENTA}==================================================={Colors.RESET}\n")

    omni_base_url = find_omniroute_base_url(args.omni_url, args.api_key)
    print(f"{Colors.CYAN}🔗 OmniRoute URL:{Colors.RESET} {omni_base_url}")

    # 1. Получаем список моделей
    all_models = fetch_all_models(omni_base_url, args.api_key, test_all=args.all)
    if not all_models:
        all_models = [
            "auto/best-free",
            "gemini-3.8-flash-medium",
            "agy/gemini-3.8-flash-high",
            "agy/gemini-3.5-flash-lite",
            "unc/turboderp/Qwen3.8-27B-exl3"
        ]

    # 2. Тестируем модели
    if not args.skip_check:
        working_models = benchmark_and_filter_models(omni_base_url, all_models, args.api_key)
    else:
        working_models = [(m, 1000, "") for m in all_models]

    if not working_models:
        print(f"\n{Colors.RED}❌ Ни одна модель не ответила успешно!{Colors.RESET}")
        print("Пожалуйста, проверьте статус провайдеров в веб-интерфейсе OmniRoute.")
        sys.exit(1)

    if args.test_only:
        print_working_models_table(working_models)
        explain_auto_routes()
        print(f"\n{Colors.GREEN}Тестирование завершено. Найдено рабочих моделей: {len(working_models)}{Colors.RESET}")
        sys.exit(0)

    # 3. Рекомендуемый расчет
    rec_smart, rec_light, _, _ = select_best_model_combination(working_models)

    # 4. Интерактивный выбор моделей пользователем
    best_smart, best_light, smart_pool, light_pool = interactive_model_selection(working_models, rec_smart, rec_light)

    # 5. Запуск Failover Proxy
    proxy_port = find_free_port(args.proxy_port)
    proxy_host = "127.0.0.1"
    proxy_url = f"http://{proxy_host}:{proxy_port}"

    # 6. Инициализация менеджера пулов (модели становятся основными немедленно)
    pool_mgr = ModelPoolManager(config_file="active_pool.json")
    pool_mgr.set_pools(best_smart, best_light, smart_pool, light_pool, proxy_port=proxy_port, proxy_url=proxy_url, api_key=args.api_key)

    handler_class = create_proxy_handler(omni_base_url, args.api_key, pool_mgr)

    try:
        server = ThreadedHTTPServer((proxy_host, proxy_port), handler_class)
    except Exception as e:
        print(f"{Colors.RED}Не удалось запустить прокси на {proxy_host}:{proxy_port}: {e}{Colors.RESET}")
        sys.exit(1)

    proxy_thread = threading.Thread(target=server.serve_forever, daemon=True)
    proxy_thread.start()

    # 7. Мгновенно генерируем/обновляем файлы запуска Claude Code
    write_claude_launch_scripts(proxy_url, args.api_key, best_smart, best_light)

    print(f"{Colors.BOLD}{Colors.GREEN}========================================================================{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}🛡️  Failover Прокси успешно запущен и слушает:{Colors.RESET} {Colors.CYAN}{proxy_url}{Colors.RESET}")
    print(f"{Colors.GRAY}💡 Конфиг пула (горячая смена): active_pool.json{Colors.RESET}")
    print(f"{Colors.GREEN}========================================================================{Colors.RESET}\n")

    print(f"{Colors.BOLD}{Colors.YELLOW}👉 ДЛЯ ЗАПУСКА CLAUDE CODE ОТКРОЙТЕ ВТОРОЕ ОКНО ТЕРМИНАЛА:{Colors.RESET}")
    if sys.platform == "win32":
        print(f"   Дважды кликните по: {Colors.CYAN}run_claude.bat{Colors.RESET}")
        print(f"   или в PowerShell: {Colors.CYAN}.\\run_claude.ps1{Colors.RESET}\n")
    else:
        print(f"   Выполните команду: {Colors.CYAN}./run_claude.sh{Colors.RESET}\n")

    print(f"{Colors.GRAY}💡 В этом окне можно вводить команды во время работы:{Colors.RESET}")
    print(f"   [s] + Enter — текущий статус моделей и статистика токенов")
    print(f"   [c] + Enter — сменить модель на лету")
    print(f"   [r] + Enter — обновить скрипты запуска для перезапуска Claude")
    print(f"   [q] + Enter — остановить прокси\n")

    print(f"{Colors.BOLD}{Colors.YELLOW}--- ЖИВОЙ МОНИТОРИНГ ЗАПРОСОВ (Tokens / Latency / Status) ---{Colors.RESET}")
    print(f"{Colors.GRAY}Время    | Модель                         | Токены (Вход↑ Выход↓) | Задержка{Colors.RESET}")
    print(f"{Colors.GRAY}---------+--------------------------------+-----------------------+---------{Colors.RESET}")

    def console_listener():
        while True:
            try:
                line = sys.stdin.readline().strip()
                if not line:
                    continue
                cmd = line.lower()
                if cmd in ("s", "status"):
                    cmd_status(proxy_port)
                elif cmd in ("c", "change", "switch"):
                    print_working_models_table(working_models)
                    s_in = input(f"Введите номер или имя новой Smart-модели: ").strip()
                    model_map = {idx: m[0] for idx, m in enumerate(working_models, 1)}
                    new_smart = model_map.get(int(s_in), s_in) if s_in.isdigit() else s_in
                    if new_smart:
                        old_s = pool_mgr.smart_model
                        pool_mgr.smart_model = new_smart
                        pool_mgr.save_to_file()
                        write_claude_launch_scripts(proxy_url, args.api_key, pool_mgr.smart_model, pool_mgr.light_model)
                        print(f"{Colors.GREEN}⚡ Smart модель на лету переключена: {old_s} -> {new_smart}{Colors.RESET}")
                        print(f"{Colors.YELLOW}ℹ️  Все последующие запросы Claude Code теперь направляются на '{new_smart}'.{Colors.RESET}")
                        print(f"{Colors.CYAN}💡 Совет: Чтобы обновить имя модели в строке статуса Claude Code,")
                        print(f"   перезапустите Claude Code в соседнем окне (./run_claude.sh) или введите в Claude команду: /model {new_smart}{Colors.RESET}")
                elif cmd in ("r", "restart"):
                    write_claude_launch_scripts(proxy_url, args.api_key, pool_mgr.smart_model, pool_mgr.light_model)
                    print(f"{Colors.GREEN}🔄 Файлы запуска обновлены с текущими моделями ({pool_mgr.smart_model} / {pool_mgr.light_model})!{Colors.RESET}")
                    print(f"   Чтобы перезапустить Claude Code, просто перезапустите ./run_claude.sh (или run_claude.bat) во втором окне.")
                    print(f"   Чтобы перезапустить Claude Code, просто перезапустите ./run_claude.sh (или run_claude.bat) во втором окне.")
                elif cmd in ("q", "quit", "exit"):
                    print(f"\n{Colors.YELLOW}Остановка по команде пользователя...{Colors.RESET}")
                    server.shutdown()
                    break
            except Exception:
                break

    listener_thread = threading.Thread(target=console_listener, daemon=True)
    listener_thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Остановка прокси...{Colors.RESET}")
    finally:
        server.shutdown()
        print(f"\n{Colors.BOLD}{Colors.MAGENTA}=== Сводка сессии ==={Colors.RESET}")
        print(f"  Всего запросов: {pool_mgr.stats['total_requests']}")
        print(f"  Авто-замен (failovers): {pool_mgr.stats['failovers_count']}")
        print(f"  Всего входных токенов: {format_tokens(pool_mgr.stats['total_input_tokens'])}")
        print(f"  Всего выходных токенов: {format_tokens(pool_mgr.stats['total_output_tokens'])}")
        print(f"{Colors.GREEN}Завершено.{Colors.RESET}")


if __name__ == "__main__":
    main()
