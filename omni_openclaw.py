#!/usr/bin/env python3
"""
OmniRoute + OpenClaw Smart Orchestrator & Auto-Failover Proxy
Для Windows 10/11 (а также macOS и Linux).

Возможности:
1. Автоматический поиск OmniRoute (порт 20128, 8000, 3000...).
2. Параллельный Health-Check моделей (проверка через OpenAI /v1/chat/completions и Anthropic).
3. Наглядная нумерованная таблица ВСЕХ работающих моделей с задержкой и ролями.
4. Разъяснение работы виртуальных маршрутов auto/best-free и auto/*.
5. Интерактивный выбор пользователем: какую модель назначить Primary (основной) для OpenClaw.
6. МГНОВЕННАЯ АКТИВАЦИЯ:
   - Применяется в локальном Failover Прокси на лету.
   - Автоматически обновляет ~/.openclaw/openclaw.json (%USERPROFILE%/.openclaw/openclaw.json).
   - Обновляет run_openclaw.bat / run_openclaw.ps1.
7. Локальный Transparent Failover Proxy:
   - Перехватывает вызовы /v1/chat/completions (OpenAI формат, используемый OpenClaw).
   - Если активная модель отдает 429 Rate Limit, 500, 502 или таймаут — НА ЛЕТУ подставляет следующую модель из резервного пула.
8. Живой мониторинг в реальном времени (окно 1):
   HH:MM:SS | model-name | InTokens↑ OutTokens↓ | Latency(ms)
9. Горячие команды в консоли (c — сменить модель, s — статус, r — перезапуск/обновление, q — выход).
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

# Включение ANSI цветов на Windows консоли
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
    def __init__(self, config_file="active_pool.json"):
        self.config_file = config_file
        self.lock = threading.Lock()
        self.primary_model = ""
        self.fallback_pool = []
        self.failed_models = {}
        self.stats = {
            "total_requests": 0,
            "failed_requests": 0,
            "failovers_count": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
        }

    def set_pool(self, primary_model, fallback_pool):
        with self.lock:
            self.primary_model = primary_model
            self.fallback_pool = fallback_pool
            self.save_to_file()

    def save_to_file(self):
        try:
            data = {
                "primary_model": self.primary_model,
                "fallback_pool": self.fallback_pool,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "Вы можете изменить primary_model прямо в этом файле — прокси подхватит изменения на лету!"
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
                if data.get("primary_model") and data["primary_model"] != self.primary_model:
                    print(f"{Colors.YELLOW}⚡ [Hot-Swap] Смена активной модели OpenClaw: {self.primary_model} -> {data['primary_model']}{Colors.RESET}")
                    self.primary_model = data["primary_model"]
                if "fallback_pool" in data:
                    self.fallback_pool = data["fallback_pool"]
        except Exception:
            pass

    def get_current_model(self):
        self.load_from_file_if_modified()
        with self.lock:
            return self.primary_model or (self.fallback_pool[0] if self.fallback_pool else "auto/best-free")

    def report_failure_and_get_fallback(self, failed_model):
        with self.lock:
            self.failed_models[failed_model] = time.time()
            self.stats["failovers_count"] += 1

            candidates = [m for m in self.fallback_pool if m != failed_model]
            if candidates:
                next_model = candidates[0]
                self.primary_model = next_model
                self.save_to_file()
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


def create_openclaw_proxy_handler(omni_base_url, omni_api_key, pool_mgr):
    class OpenClawProxyHandler(BaseHTTPRequestHandler):
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
            if clean_path in ("/v1/chat/completions", "/chat/completions", "/v1/messages", "/messages", "/v1/complete"):
                self.handle_chat_request()
            else:
                self.forward_request("POST")

        def handle_admin_status(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            data = {
                "primary_model": pool_mgr.primary_model,
                "fallback_pool": pool_mgr.fallback_pool,
                "stats": pool_mgr.stats
            }
            self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

        def handle_switch_command(self):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            model = params.get("model", [""])[0]
            if model:
                pool_mgr.primary_model = model
                pool_mgr.save_to_file()
                msg = f"OK: OpenClaw primary model switched to {model}"
            else:
                msg = "Error: model param required"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode("utf-8"))

        def handle_models_request(self):
            models_list = [
                {"id": pool_mgr.primary_model, "object": "model", "owned_by": "omniroute"},
                {"id": "combo/agy-kiro-zero-cost", "object": "model", "owned_by": "omniroute"},
                {"id": "auto/best-free", "object": "model", "owned_by": "omniroute"},
            ]
            for m in pool_mgr.fallback_pool[:12]:
                models_list.append({"id": m, "object": "model", "owned_by": "omniroute"})

            models_data = {"object": "list", "data": models_list}
            body = json.dumps(models_data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def handle_chat_request(self):
            start_time = time.time()
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)

            try:
                body_json = json.loads(raw_body.decode("utf-8"))
            except Exception:
                body_json = {}

            req_model = body_json.get("model", "")
            if req_model.startswith("omniroute/"):
                req_model = req_model[len("omniroute/"):]

            target_model = pool_mgr.get_current_model()
            is_stream = body_json.get("stream", False)

            max_attempts = 3
            current_target = target_model
            last_err = ""

            for attempt in range(max_attempts):
                if not current_target:
                    break

                body_json["model"] = current_target
                new_body = json.dumps(body_json).encode("utf-8")

                try:
                    resp, status, headers, in_tok, out_tok = self.send_to_upstream(
                        self.command, self.path, new_body, is_stream
                    )

                    if status < 400:
                        elapsed_ms = int((time.time() - start_time) * 1000)
                        now_str = time.strftime("%H:%M:%S")

                        tok_str = f"{format_tokens(in_tok)}↑ {format_tokens(out_tok)}↓"
                        print(f"{Colors.GRAY}{now_str}{Colors.RESET} | {Colors.CYAN}{current_target:<32}{Colors.RESET} | {Colors.GREEN}{tok_str:>13}{Colors.RESET} | {Colors.YELLOW}{elapsed_ms:>6}ms{Colors.RESET}")

                        with pool_mgr.lock:
                            pool_mgr.stats["total_requests"] += 1
                            pool_mgr.stats["total_input_tokens"] += in_tok
                            pool_mgr.stats["total_output_tokens"] += out_tok
                        return

                    else:
                        last_err = f"HTTP {status}"
                        fallback = pool_mgr.report_failure_and_get_fallback(current_target)
                        now_str = time.strftime("%H:%M:%S")
                        print(f"{Colors.RED}⚠️ [{now_str}] Модель '{current_target}' вернула {status}! Замена на лету на '{fallback}'...{Colors.RESET}")
                        if not fallback:
                            break
                        current_target = fallback

                except Exception as e:
                    last_err = str(e)
                    fallback = pool_mgr.report_failure_and_get_fallback(current_target)
                    now_str = time.strftime("%H:%M:%S")
                    print(f"{Colors.RED}⚠️ [{now_str}] Ошибка связи с '{current_target}': {e}! Замена на лету на '{fallback}'...{Colors.RESET}")
                    if not fallback:
                        break
                    current_target = fallback

            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            err_payload = json.dumps({
                "error": {
                    "message": f"OmniRoute failover exhausted. Last error: {last_err}",
                    "type": "omniroute_error"
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
                upstream_resp = urllib.request.urlopen(req, timeout=120)
                status = upstream_resp.status
                headers = dict(upstream_resp.headers)
            except urllib.error.HTTPError as e:
                return None, e.code, dict(e.headers), 0, 0
            except Exception as e:
                raise e

            self.send_response(status)
            for k, v in headers.items():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)

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
                self.send_header("Connection", "close")
                self.end_headers()

                buffer = ""
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
                                if "usage" in d and d["usage"]:
                                    in_tokens = d["usage"].get("prompt_tokens", in_tokens)
                                    out_tokens = d["usage"].get("completion_tokens", out_tokens)
                            except Exception:
                                pass

                if out_tokens == 0:
                    out_tokens = max(1, len(buffer) // 4)

            else:
                content = upstream_resp.read()
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

                try:
                    resp_json = json.loads(content.decode("utf-8"))
                    usage = resp_json.get("usage", {})
                    if usage:
                        in_tokens = usage.get("prompt_tokens", in_tokens)
                        out_tokens = usage.get("completion_tokens", out_tokens)
                except Exception:
                    out_tokens = max(1, len(content) // 4)

            return True, status, headers, in_tokens, out_tokens

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

    return OpenClawProxyHandler


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
    sorted_m = sorted(chat_models, key=lambda x: (
        0 if any(k in x.lower() for k in ('free', 'flash', 'mini', 'lite', 'low', 'chat', 'fast')) else 1,
        len(x)
    ))
    return sorted_m[:2]


def probe_canary_model(omni_base_url, model_id, api_key=""):
    """Быстрый зондирующий запрос к модели-канарейке."""
    headers = {
        "Content-Type": "application/json",
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
            f"{omni_base_url.rstrip('/')}/v1/chat/completions",
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
                req_anth = urllib.request.Request(
                    f"{omni_base_url.rstrip('/')}/v1/messages",
                    data=payload,
                    headers={**headers, "anthropic-version": "2023-06-01"},
                    method="POST"
                )
                with urllib.request.urlopen(req_anth, timeout=5.0) as r2:
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
    и динамически отсекает неактивные/сломанные провайдеры за секунды без всякого хардкода.
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

    if any(p[0] == "auto" for p in alive_providers):
        for v in ["combo/agy-kiro-zero-cost", "auto/best-free", "gemini-3.8-flash-medium"]:
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
            for virtual_m in ["combo/agy-kiro-zero-cost", "auto/best-free", "gemini-3.8-flash-medium"]:
                if virtual_m not in models:
                    models.insert(0, virtual_m)

            if test_all:
                return sorted(list(set(models)))

            return dynamic_canary_provider_scan(omni_base_url, models, api_key)
    except Exception as e:
        print(f"{Colors.RED}❌ Не удалось получить список моделей из {url}: {e}{Colors.RESET}")
        return []


def test_single_model(omni_base_url, model_id, api_key=""):
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01"
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    openai_payload = json.dumps({
        "model": model_id,
        "max_tokens": 5,
        "messages": [{"role": "user", "content": "ping"}]
    }).encode("utf-8")

    t0 = time.time()
    try:
        req = urllib.request.Request(
            f"{omni_base_url.rstrip('/')}/v1/chat/completions",
            data=openai_payload,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            latency = int((time.time() - t0) * 1000)
            if r.status == 200:
                resolved = r.headers.get("x-omniroute-model", "")
                return (model_id, True, latency, "", resolved)
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            anthropic_payload = json.dumps({
                "model": model_id,
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "ping"}]
            }).encode("utf-8")
            try:
                t0_ant = time.time()
                req_ant = urllib.request.Request(
                    f"{omni_base_url.rstrip('/')}/v1/messages",
                    data=anthropic_payload,
                    headers=headers,
                    method="POST"
                )
                with urllib.request.urlopen(req_ant, timeout=10) as r_ant:
                    latency = int((time.time() - t0_ant) * 1000)
                    if r_ant.status == 200:
                        resolved = r_ant.headers.get("x-omniroute-model", "")
                        return (model_id, True, latency, "", resolved)
            except Exception as ex:
                return (model_id, False, 0, f"HTTP {getattr(ex, 'code', 'Error')}", "")
        err_msg = f"HTTP {e.code}"
        return (model_id, False, 0, err_msg, "")
    except Exception:
        return (model_id, False, 0, "Таймаут/Сбой", "")


def benchmark_and_filter_models(omni_base_url, models, api_key=""):
    print(f"\n{Colors.BOLD}{Colors.CYAN}🚀 Запуск проверки моделей для OpenClaw ({len(models)} шт.)...{Colors.RESET}\n")
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

    working_models.sort(key=lambda x: x[1])
    return working_models


def print_openclaw_models_table(working_models):
    """Выводит нумерованную таблицу всех работающих моделей."""
    print(f"\n{Colors.BOLD}{Colors.GREEN}========================================================================================{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}   📋 СПИСОК РАБОТАЮЩИХ МОДЕЛЕЙ ДЛЯ OPENCLAW (Всего: {len(working_models)}){Colors.RESET}")
    print(f"{Colors.GREEN}----------------------------------------------------------------------------------------{Colors.RESET}")
    print(f" {Colors.BOLD}№   | Идентификатор модели                        | Задержка | Тип / Роль{Colors.RESET}")
    print(f"-----+---------------------------------------------+----------+-------------------------")

    for idx, item in enumerate(working_models, 1):
        m_id, latency = item[0], item[1]
        resolved = item[2] if len(item) > 2 else ""
        lower = m_id.lower()
        if "pro" in lower or "high" in lower or "coder" in lower or "r1" in lower or "sonnet" in lower:
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


def explain_openclaw_auto_routes():
    """Разъясняет особенности моделей auto/best-free и комбо для OpenClaw."""
    print(f"{Colors.BOLD}{Colors.YELLOW}ℹ️  ОБРАТИТЕ ВНИМАНИЕ НА МОДЕЛИ СЕМЕЙСТВА 'auto/*' (например, auto/best-free):{Colors.RESET}")
    print(f" • {Colors.CYAN}auto/best-free{Colors.RESET} — это виртуальный маршрут OmniRoute, который на лету")
    print(f"   перенаправляет запрос на самый стабильный бесплатный провайдер (сейчас {Colors.GREEN}gemini-3.1-flash-lite{Colors.RESET}).")
    print(f" • {Colors.BOLD}Можно ли их настроить?{Colors.RESET}")
    print(f"   1) В OmniRoute можно создать комбо-маршрут со своей цепочкой моделей и приоритетом.")
    print(f"   2) Либо прямо в меню ниже выбрать конкретную проверенную модель (например,")
    print(f"      {Colors.GREEN}agy/gemini-3.8-flash-high{Colors.RESET}), чтобы OpenClaw работал именно с ней без сюрпризов.\n")


def select_best_openclaw_combination(working_models):
    if not working_models:
        return "auto/best-free", []

    smart_candidates = []
    fast_candidates = []

    for item in working_models:
        model_id, latency = item[0], item[1]
        lower = model_id.lower()
        if any(kw in lower for kw in SMART_KEYWORDS):
            smart_candidates.append((model_id, latency))
        elif any(kw in lower for kw in FAST_KEYWORDS):
            fast_candidates.append((model_id, latency))
        else:
            smart_candidates.append((model_id, latency))

    smart_candidates.sort(key=lambda x: x[1])
    fast_candidates.sort(key=lambda x: x[1])

    primary = smart_candidates[0][0] if smart_candidates else working_models[0][0]
    fallbacks = [m[0] for m in smart_candidates if m[0] != primary] + [m[0] for m in fast_candidates if m[0] != primary]
    if "auto/best-free" not in fallbacks:
        fallbacks.append("auto/best-free")

    return primary, fallbacks


def interactive_openclaw_selection(working_models, default_primary, default_fallbacks):
    """Интерактивное меню выбора моделей для OpenClaw."""
    print_openclaw_models_table(working_models)
    explain_openclaw_auto_routes()

    print(f"{Colors.BOLD}{Colors.CYAN}🎯 ВЫБОР ОСНОВНОЙ МОДЕЛИ ДЛЯ OPENCLAW:{Colors.RESET}")
    print(f"   Рекомендуемая основная модель (Primary): {Colors.GREEN}{default_primary}{Colors.RESET}")
    print(f"   Цепочка резерва (Fallbacks): {Colors.GRAY}{', '.join(default_fallbacks[:6])}{Colors.RESET}\n")

    print(f"   {Colors.BOLD}[Enter]{Colors.RESET} — применить рекомендуемую модель ({default_primary})")
    print(f"   {Colors.BOLD}[1-{len(working_models)}]{Colors.RESET} — ввести номер модели из списка, чтобы сделать ее ОСНОВНОЙ\n")

    model_map = {idx: m[0] for idx, m in enumerate(working_models, 1)}

    try:
        choice = input(f"{Colors.BOLD}Ваш выбор [Enter / номер]: {Colors.RESET}").strip()
    except (EOFError, KeyboardInterrupt):
        choice = ""

    primary_model = default_primary

    if not choice:
        pass
    elif choice.isdigit() and int(choice) in model_map:
        primary_model = model_map[int(choice)]
    else:
        for m in working_models:
            if choice.lower() in m[0].lower():
                primary_model = m[0]
                break

    fallback_pool = [m[0] for m in working_models if m[0] != primary_model]
    if "auto/best-free" not in fallback_pool and primary_model != "auto/best-free":
        fallback_pool.append("auto/best-free")

    print(f"\n{Colors.BOLD}{Colors.GREEN}========================================================================{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}🚀 ВЫБРАННАЯ МОДЕЛЬ СРАЗУ СТАЛА ОСНОВНОЙ ДЛЯ OPENCLAW:{Colors.RESET}")
    print(f"   🎯 Primary Model: {Colors.GREEN}{primary_model}{Colors.RESET}")
    print(f"   📋 Fallback Pool : {Colors.GRAY}{', '.join(fallback_pool[:6])}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}========================================================================{Colors.RESET}\n")

    return primary_model, fallback_pool


def update_openclaw_config(proxy_url, api_key, primary_model, fallback_pool):
    """Мгновенно обновляет ~/.openclaw/openclaw.json (%USERPROFILE%/.openclaw/openclaw.json)."""
    home = os.environ.get("USERPROFILE") if sys.platform == "win32" else os.path.expanduser("~")
    config_dir = os.path.join(home, ".openclaw")
    config_file = os.path.join(config_dir, "openclaw.json")

    try:
        os.makedirs(config_dir, exist_ok=True)
    except Exception:
        pass

    existing_config = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                existing_config = json.load(f)
        except Exception:
            existing_config = {}

    models_def = [
        {"id": primary_model, "name": f"OmniRoute Primary ({primary_model})"},
        {"id": "combo/agy-kiro-zero-cost", "name": "OmniRoute Zero Cost Loop"},
        {"id": "auto/best-free", "name": "OmniRoute Auto Best Free"}
    ]
    for fb in fallback_pool[:8]:
        models_def.append({"id": fb, "name": f"Fallback ({fb})"})

    provider_config = {
        "baseUrl": f"{proxy_url.rstrip('/')}/v1",
        "apiKey": api_key,
        "api": "openai-completions",
        "models": models_def
    }

    if "models" not in existing_config:
        existing_config["models"] = {"mode": "merge", "providers": {}}
    if "providers" not in existing_config["models"]:
        existing_config["models"]["providers"] = {}

    existing_config["models"]["providers"]["omniroute"] = provider_config

    fb_prefixed = [f"omniroute/{m}" for m in fallback_pool[:6]]
    existing_config["agents"] = {
        "defaults": {
            "model": {
                "primary": f"omniroute/{primary_model}",
                "fallbacks": fb_prefixed
            }
        }
    }

    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(existing_config, f, indent=2, ensure_ascii=False)
        print(f"{Colors.GREEN}✅ Конфигурация OpenClaw мгновенно обновлена в:{Colors.RESET} {config_file}")
        return True
    except Exception as e:
        print(f"{Colors.YELLOW}⚠️ Не удалось напрямую записать {config_file}: {e}{Colors.RESET}")
        return False


def write_openclaw_launch_scripts():
    """Создает готовые скрипты запуска OpenClaw во втором окне."""
    script_bat = "run_openclaw.bat"
    with open(script_bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("@echo off\r\n")
        f.write("chcp 65001 >nul\r\n")
        f.write("echo Запуск OpenClaw через OmniRoute Smart Failover Proxy...\r\n")
        f.write("openclaw chat %*\r\n")

    script_ps1 = "run_openclaw.ps1"
    with open(script_ps1, "w", encoding="utf-8") as f:
        f.write("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n")
        f.write("Write-Host 'Запуск OpenClaw через OmniRoute Smart Failover Proxy...' -ForegroundColor Cyan\n")
        f.write("openclaw chat @args\n")


def find_free_port(start_port=9099):
    for port in range(start_port, start_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start_port


def cmd_switch(model, proxy_port=9099):
    url = f"http://127.0.0.1:{proxy_port}/admin/switch?model={urllib.parse.quote(model)}"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            print(f"{Colors.GREEN}✅ {r.read().decode('utf-8')}{Colors.RESET}")
    except Exception as e:
        print(f"{Colors.RED}❌ Не удалось отправить команду прокси: {e}{Colors.RESET}")


def cmd_status(proxy_port=9099):
    url = f"http://127.0.0.1:{proxy_port}/admin/status"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
            print(f"\n{Colors.BOLD}{Colors.CYAN}--- ТЕКУЩИЙ СТАТУС МОДЕЛЕЙ OPENCLAW ---{Colors.RESET}")
            print(f"🎯 Primary Model:  {Colors.GREEN}{data.get('primary_model')}{Colors.RESET}")
            print(f"📋 Fallback Pool:  {', '.join(data.get('fallback_pool', []))}")
            st = data.get("stats", {})
            print(f"\n{Colors.YELLOW}📊 Статистика сессии OpenClaw:{Colors.RESET}")
            print(f"   Запросов: {st.get('total_requests')}")
            print(f"   Авто-переключений (Failovers): {st.get('failovers_count')}")
            print(f"   Входных токенов:  {format_tokens(st.get('total_input_tokens', 0))}")
            print(f"   Выходных токенов: {format_tokens(st.get('total_output_tokens', 0))}\n")
    except Exception as e:
        print(f"{Colors.RED}❌ Прокси не отвечает на порту {proxy_port}: {e}{Colors.RESET}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "switch":
        if len(sys.argv) >= 3:
            cmd_switch(sys.argv[2])
        else:
            print("Использование: python omni_openclaw.py switch [имя_модели]")
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        cmd_status()
        sys.exit(0)

    import argparse
    parser = argparse.ArgumentParser(description="OmniRoute + OpenClaw Smart Orchestrator & Auto-Failover Proxy")
    parser.add_argument("--omni-url", default="", help="URL OmniRoute (по умолчанию автоопределение)")
    parser.add_argument("--api-key", default=os.environ.get("OMNIROUTE_API_KEY", DEFAULT_API_KEY), help="OmniRoute API Key")
    parser.add_argument("--proxy-port", type=int, default=9099, help="Порт локального прокси")
    parser.add_argument("--skip-check", action="store_true", help="Пропустить тестирование моделей")
    parser.add_argument("--test-only", action="store_true", help="Только протестировать модели и выйти")
    args = parser.parse_args()

    print(f"\n{Colors.BOLD}{Colors.MAGENTA}==================================================={Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.MAGENTA}       OmniRoute + OpenClaw Smart Proxy             {Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.MAGENTA}==================================================={Colors.RESET}\n")

    omni_base_url = find_omniroute_base_url(args.omni_url, args.api_key)
    print(f"{Colors.CYAN}🔗 OmniRoute URL:{Colors.RESET} {omni_base_url}")

    # 1. Получаем список моделей
    all_models = fetch_all_models(omni_base_url, args.api_key)
    if not all_models:
        all_models = [
            "combo/agy-kiro-zero-cost",
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
        print_openclaw_models_table(working_models)
        explain_openclaw_auto_routes()
        print(f"\n{Colors.GREEN}Тестирование завершено. Найдено рабочих моделей: {len(working_models)}{Colors.RESET}")
        sys.exit(0)

    # 3. Рекомендуемый выбор
    rec_primary, rec_fallbacks = select_best_openclaw_combination(working_models)

    # 4. Интерактивный выбор модели пользователем
    primary_model, fallback_pool = interactive_openclaw_selection(working_models, rec_primary, rec_fallbacks)

    # 5. Инициализация менеджера пулов (модель сразу становится основной)
    pool_mgr = ModelPoolManager(config_file="active_pool.json")
    pool_mgr.set_pool(primary_model, fallback_pool)

    # 6. Запуск Failover Proxy
    proxy_port = find_free_port(args.proxy_port)
    proxy_host = "127.0.0.1"
    handler_class = create_openclaw_proxy_handler(omni_base_url, args.api_key, pool_mgr)

    try:
        server = ThreadedHTTPServer((proxy_host, proxy_port), handler_class)
    except Exception as e:
        print(f"{Colors.RED}Не удалось запустить прокси на {proxy_host}:{proxy_port}: {e}{Colors.RESET}")
        sys.exit(1)

    proxy_thread = threading.Thread(target=server.serve_forever, daemon=True)
    proxy_thread.start()

    proxy_url = f"http://{proxy_host}:{proxy_port}"

    # 7. Мгновенно обновляем конфигурацию OpenClaw и скрипты запуска
    update_openclaw_config(proxy_url, args.api_key, primary_model, fallback_pool)
    write_openclaw_launch_scripts()

    print(f"{Colors.BOLD}{Colors.GREEN}========================================================================{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.GREEN}🛡️  Failover Прокси для OpenClaw активен:{Colors.RESET} {Colors.CYAN}{proxy_url}{Colors.RESET}")
    print(f"{Colors.GRAY}💡 Конфиг пула (горячая смена): active_pool.json{Colors.RESET}")
    print(f"{Colors.GREEN}========================================================================{Colors.RESET}\n")

    print(f"{Colors.BOLD}{Colors.YELLOW}👉 ДЛЯ ЗАПУСКА OPENCLAW ОТКРОЙТЕ ВТОРОЕ ОКНО ТЕРМИНАЛА:{Colors.RESET}")
    print(f"   Дважды кликните по: {Colors.CYAN}run_openclaw.bat{Colors.RESET}")
    print(f"   или в консоли: {Colors.CYAN}openclaw chat{Colors.RESET}  (или openclaw tui)\n")

    print(f"{Colors.GRAY}💡 В этом окне можно вводить команды во время работы:{Colors.RESET}")
    print(f"   [s] + Enter — статус и расход токенов")
    print(f"   [c] + Enter — сменить модель OpenClaw на лету")
    print(f"   [r] + Enter — обновить openclaw.json для перезапуска сессии")
    print(f"   [q] + Enter — остановить прокси\n")

    print(f"{Colors.BOLD}{Colors.YELLOW}--- ЖИВОЙ МОНИТОРИНГ ЗАПРОСОВ OPENCLAW (Tokens / Latency / Status) ---{Colors.RESET}")
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
                    print_openclaw_models_table(working_models)
                    s_in = input(f"Введите номер или имя новой модели для OpenClaw: ").strip()
                    model_map = {idx: m[0] for idx, m in enumerate(working_models, 1)}
                    new_primary = model_map.get(int(s_in), s_in) if s_in.isdigit() else s_in
                    if new_primary:
                        pool_mgr.primary_model = new_primary
                        pool_mgr.save_to_file()
                        update_openclaw_config(proxy_url, args.api_key, pool_mgr.primary_model, pool_mgr.fallback_pool)
                        print(f"{Colors.GREEN}⚡ Модель OpenClaw на лету переключена на: {new_primary}{Colors.RESET}")
                elif cmd in ("r", "restart"):
                    update_openclaw_config(proxy_url, args.api_key, pool_mgr.primary_model, pool_mgr.fallback_pool)
                    print(f"{Colors.GREEN}🔄 Конфигурация openclaw.json обновлена с моделью {pool_mgr.primary_model}!{Colors.RESET}")
                    print(f"   Чтобы перезапустить OpenClaw, просто перезапустите run_openclaw.bat во втором окне.")
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
        print(f"\n{Colors.BOLD}{Colors.MAGENTA}=== Сводка сессии OpenClaw ==={Colors.RESET}")
        print(f"  Всего запросов: {pool_mgr.stats['total_requests']}")
        print(f"  Авто-замен (failovers): {pool_mgr.stats['failovers_count']}")
        print(f"  Всего входных токенов: {format_tokens(pool_mgr.stats['total_input_tokens'])}")
        print(f"  Всего выходных токенов: {format_tokens(pool_mgr.stats['total_output_tokens'])}")
        print(f"{Colors.GREEN}Завершено.{Colors.RESET}")


if __name__ == "__main__":
    main()
