#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OmniRoute + Claude Code & OpenClaw Doctor and Auto-Healer.
Диагностирует все компоненты системы и умеет автоматически устранять неполадки:
- Проверка Интернета и DNS
- Проверка службы OmniRoute и API-ключа
- Проверка Умного Прокси-оркестратора
- Проверка и авто-починка системных конфигов Claude Code / OpenClaw
- Проверка доступности моделей и выявление 429 Rate Limits
- Авто-исправление в 1 клик (GUI + CLI)
"""

import sys
import os
import json
import time
import socket
import urllib.request
import urllib.error
import urllib.parse
import subprocess
import threading

class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

if os.name == "nt":
    os.system("")  # Включаем поддержку ANSI escape-кодов в conhost Windows


class DoctorEngine:
    def __init__(self, target_tool="claude"):
        self.target_tool = target_tool  # "claude" или "openclaw"
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.omni_url = "http://127.0.0.1:20128"
        self.api_key = "sk-9e61b4bb8a128d44-5c456d-dc8deecf"
        self.default_proxy_port = 9099
        self.active_proxy_port = self.detect_active_proxy_port()
        self.active_proxy_url = f"http://127.0.0.1:{self.active_proxy_port}"

    def detect_active_proxy_port(self):
        pool_file = os.path.join(self.script_dir, "active_pool.json")
        if os.path.exists(pool_file):
            try:
                with open(pool_file, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    if d.get("proxy_port"):
                        return int(d["proxy_port"])
            except Exception:
                pass
        # Проверяем порты 9099 и 9100
        for p in (9099, 9100):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{p}/health")
                with urllib.request.urlopen(req, timeout=1) as r:
                    if r.status == 200:
                        return p
            except Exception:
                continue
        return self.default_proxy_port

    def get_claude_settings_path(self):
        home = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        return os.path.join(home, ".claude", "settings.json")

    # --- 1. Проверка интернета ---
    def check_internet(self):
        # Быстрый DNS-сокет к 1.1.1.1
        t0 = time.time()
        try:
            socket.setdefaulttimeout(3.0)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("1.1.1.1", 53))
            s.close()
            ms = int((time.time() - t0) * 1000)
            return {"ok": True, "ms": ms, "msg": f"Связь в норме ({ms}ms)"}
        except Exception as e:
            # Пробуем HTTP к google.com
            try:
                urllib.request.urlopen("https://www.google.com", timeout=3.0)
                ms = int((time.time() - t0) * 1000)
                return {"ok": True, "ms": ms, "msg": f"Связь в норме ({ms}ms)"}
            except Exception as e2:
                return {
                    "ok": False,
                    "ms": 0,
                    "msg": "Нет интернет-соединения или заблокирован DNS",
                    "error": str(e2),
                    "fixable": False
                }

    # --- 2. Проверка OmniRoute ---
    def check_omniroute(self):
        t0 = time.time()
        try:
            req = urllib.request.Request(f"{self.omni_url}/health")
            with urllib.request.urlopen(req, timeout=3.0) as r:
                ms = int((time.time() - t0) * 1000)
                return {"ok": True, "status": r.status, "ms": ms, "msg": f"OmniRoute работает на {self.omni_url} ({ms}ms)"}
        except urllib.error.HTTPError as e:
            ms = int((time.time() - t0) * 1000)
            return {"ok": True, "status": e.code, "ms": ms, "msg": f"OmniRoute отвечает (HTTP {e.code})"}
        except Exception as e:
            return {
                "ok": False,
                "msg": f"OmniRoute не отвечает на {self.omni_url}",
                "error": str(e),
                "fixable": True,
                "fix_type": "start_omniroute"
            }

    # --- 3. Проверка авторизации OmniRoute ---
    def check_omni_auth(self):
        try:
            req = urllib.request.Request(
                f"{self.omni_url}/v1/models",
                headers={"Authorization": f"Bearer {self.api_key}", "x-api-key": self.api_key}
            )
            with urllib.request.urlopen(req, timeout=4.0) as r:
                data = json.loads(r.read().decode("utf-8"))
                count = len(data.get("data", []))
                return {"ok": True, "msg": f"Авторизация успешна (доступно {count} моделей в каталоге)"}
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return {"ok": False, "msg": "API-ключ не принят (HTTP 401 Unauthorized)", "fixable": False}
            return {"ok": True, "msg": f"Эндпоинт моделей ответил (HTTP {e.code})"}
        except Exception as e:
            return {"ok": False, "msg": f"Ошибка проверки каталога моделей: {e}", "fixable": False}

    # --- 4. Проверка Failover-прокси ---
    def check_proxy(self):
        t0 = time.time()
        port = self.active_proxy_port
        url = f"http://127.0.0.1:{port}"
        try:
            req = urllib.request.Request(f"{url}/health")
            with urllib.request.urlopen(req, timeout=2.0) as r:
                ms = int((time.time() - t0) * 1000)
                # Читаем статус моделей
                status_req = urllib.request.Request(f"{url}/admin/status")
                with urllib.request.urlopen(status_req, timeout=2.0) as sr:
                    d = json.loads(sr.read().decode("utf-8"))
                    smart = d.get("smart_model", d.get("primary_model", "unknown"))
                    light = d.get("light_model", "auto")
                return {
                    "ok": True,
                    "port": port,
                    "smart_model": smart,
                    "light_model": light,
                    "msg": f"Прокси активен на порту {port} (Smart: {smart}, Light: {light})"
                }
        except Exception as e:
            return {
                "ok": False,
                "port": port,
                "msg": f"Прокси-оркестратор не отвечает на порту {port}",
                "error": str(e),
                "fixable": True,
                "fix_type": "restart_proxy"
            }

    # --- 5. Проверка конфига Claude Code ---
    def check_claude_config(self):
        settings_file = self.get_claude_settings_path()
        if not os.path.exists(settings_file):
            return {
                "ok": False,
                "path": settings_file,
                "msg": f"Файл настроек {settings_file} не найден",
                "fixable": True,
                "fix_type": "fix_claude_settings"
            }
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            env_vars = data.get("env", {})
            base_url = env_vars.get("ANTHROPIC_BASE_URL", "")

            # Проверяем, смотрит ли URL в наш прокси
            expected_prefix = f"http://127.0.0.1:{self.active_proxy_port}"
            alt_prefix = f"http://localhost:{self.active_proxy_port}"

            if not base_url:
                return {
                    "ok": False,
                    "path": settings_file,
                    "msg": "В settings.json отсутствует параметр ANTHROPIC_BASE_URL",
                    "fixable": True,
                    "fix_type": "fix_claude_settings"
                }

            if "20128" in base_url:
                return {
                    "ok": False,
                    "path": settings_file,
                    "current_url": base_url,
                    "msg": f"КРИТИЧЕСКИЙ РАССИНХРОН: Claude Code подключен напрямую к OmniRoute ({base_url}) в обход прокси!",
                    "fixable": True,
                    "fix_type": "fix_claude_settings"
                }

            if expected_prefix not in base_url and alt_prefix not in base_url:
                return {
                    "ok": False,
                    "path": settings_file,
                    "current_url": base_url,
                    "msg": f"Предупреждение: ANTHROPIC_BASE_URL ({base_url}) не совпадает с портом прокси ({expected_prefix})",
                    "fixable": True,
                    "fix_type": "fix_claude_settings"
                }

            return {
                "ok": True,
                "path": settings_file,
                "base_url": base_url,
                "model": env_vars.get("ANTHROPIC_MODEL", "не задана"),
                "msg": f"Конфигурация в норме: ANTHROPIC_BASE_URL -> {base_url}"
            }
        except Exception as e:
            return {
                "ok": False,
                "path": settings_file,
                "msg": f"Ошибка чтения {settings_file}: {e}",
                "fixable": True,
                "fix_type": "fix_claude_settings"
            }

    # --- 6. Проверка доступности ключевых моделей ---
    def check_upstreams(self):
        samples = [
            ("Google Gemini", "gemini-3.8-flash-medium"),
            ("xAI Grok", "grok-cli/grok-4.7-low"),
            ("DuckDuckGo", "ddgw/gpt-5.4-mini"),
            ("Auto Route", "auto/best-free")
        ]
        results = []
        for name, m_id in samples:
            t0 = time.time()
            headers = {
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
                "Authorization": f"Bearer {self.api_key}",
                "x-api-key": self.api_key
            }
            payload = json.dumps({
                "model": m_id,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}]
            }).encode("utf-8")
            try:
                req = urllib.request.Request(f"{self.omni_url}/v1/messages", data=payload, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=4.5) as r:
                    ms = int((time.time() - t0) * 1000)
                    results.append({"name": name, "model": m_id, "ok": True, "ms": ms, "status": "OK"})
            except urllib.error.HTTPError as e:
                ms = int((time.time() - t0) * 1000)
                if e.code == 429:
                    results.append({"name": name, "model": m_id, "ok": False, "ms": ms, "status": "RATE LIMIT (429)", "rate_limit": True})
                else:
                    results.append({"name": name, "model": m_id, "ok": False, "ms": ms, "status": f"HTTP {e.code}"})
            except Exception as e:
                results.append({"name": name, "model": m_id, "ok": False, "ms": 0, "status": "Таймаут/Сбой"})
        return results

    # --- Полный прогон диагностики ---
    def run_full_diagnosis(self):
        report = {}
        report["internet"] = self.check_internet()
        report["omniroute"] = self.check_omniroute()
        if report["omniroute"]["ok"]:
            report["auth"] = self.check_omni_auth()
            report["upstreams"] = self.check_upstreams()
        else:
            report["auth"] = {"ok": False, "msg": "Пропущено (OmniRoute не запущен)"}
            report["upstreams"] = []

        report["proxy"] = self.check_proxy()
        if self.target_tool == "claude":
            report["config"] = self.check_claude_config()

        # Формируем список проблем и необходимых исправлений
        issues = []
        fixes = []

        if not report["internet"]["ok"]:
            issues.append(("CRITICAL", report["internet"]["msg"]))

        if not report["omniroute"]["ok"]:
            issues.append(("CRITICAL", report["omniroute"]["msg"]))
            fixes.append("start_omniroute")

        if not report["proxy"]["ok"]:
            issues.append(("WARNING", report["proxy"]["msg"]))
            fixes.append("restart_proxy")

        if self.target_tool == "claude" and not report.get("config", {}).get("ok", True):
            issues.append(("CRITICAL", report["config"]["msg"]))
            fixes.append("fix_claude_settings")

        # Проверяем, не застрял ли Gemini в 429
        gemini_item = next((u for u in report.get("upstreams", []) if u["name"] == "Google Gemini"), None)
        if gemini_item and not gemini_item["ok"] and gemini_item.get("rate_limit"):
            issues.append(("NOTICE", "У Google Gemini активен Rate Limit (429). Рекомендуется авто-смена на Grok."))
            fixes.append("switch_to_grok")

        report["issues"] = issues
        report["fixes"] = list(dict.fromkeys(fixes))
        return report


class HealerEngine:
    def __init__(self, doctor_engine):
        self.doc = doctor_engine

    def fix_claude_settings(self):
        """Перезаписывает settings.json правильными параметрами нашего прокси."""
        settings_path = self.doc.get_claude_settings_path()
        target_url = f"http://127.0.0.1:{self.doc.active_proxy_port}"
        try:
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            data = {}
            if os.path.exists(settings_path):
                try:
                    with open(settings_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}

            if "env" not in data or not isinstance(data["env"], dict):
                data["env"] = {}

            # Считываем активные модели из active_pool.json если есть
            smart_m = "grok-cli/grok-4.7-high"
            light_m = "gemini/gemini-3.1-flash-lite"
            pool_file = os.path.join(self.doc.script_dir, "active_pool.json")
            if os.path.exists(pool_file):
                try:
                    with open(pool_file, "r", encoding="utf-8") as pf:
                        pd = json.load(pf)
                        smart_m = pd.get("smart_model", smart_m)
                        light_m = pd.get("light_model", light_m)
                except Exception:
                    pass

            data["env"]["ANTHROPIC_BASE_URL"] = target_url
            data["env"]["ANTHROPIC_AUTH_TOKEN"] = self.doc.api_key
            data["env"]["ANTHROPIC_API_KEY"] = self.doc.api_key
            data["env"]["ANTHROPIC_MODEL"] = smart_m
            data["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = smart_m
            data["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = smart_m
            data["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = light_m
            data["env"]["ANTHROPIC_SMALL_FAST_MODEL"] = light_m
            data["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "1000000"
            data["env"]["CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT"] = "1"
            data["model"] = smart_m

            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            return True, f"Настройки Claude Code обновлены: ANTHROPIC_BASE_URL -> {target_url}"
        except Exception as e:
            return False, f"Не удалось обновить settings.json: {e}"

    def fix_kill_stale_proxies(self):
        """Освобождает порты 9099 и 9100 от зависших процессов."""
        try:
            if os.name == "nt":
                # Windows
                cmd = f'powershell -Command "Get-NetTCPConnection -LocalPort 9099,9100 -ErrorAction SilentlyContinue | ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"'
                subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                # Mac / Linux
                subprocess.run("kill -9 $(lsof -t -i :9099 -i :9100 2>/dev/null) 2>/dev/null", shell=True)
            time.sleep(0.5)
            return True, "Зависшие процессы прокси успешно завершены, порты освобождены"
        except Exception as e:
            return False, f"Ошибка освобождения портов: {e}"

    def fix_switch_model(self, target_model="grok-cli/grok-4.7-high"):
        """Переключает модель на лету через API прокси."""
        try:
            url = f"http://127.0.0.1:{self.doc.active_proxy_port}/admin/switch?role=smart&model={urllib.parse.quote(target_model)}"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2.0) as r:
                return True, f"Smart-модель успешно переключена на {target_model}"
        except Exception as e:
            return False, f"Не удалось отправить команду переключения: {e}"

    def fix_start_omniroute(self):
        """Пытается запустить OmniRoute в фоновом режиме."""
        try:
            if os.name == "nt":
                subprocess.Popen(["cmd.exe", "/c", "start", "omniroute", "serve"], shell=True)
            else:
                subprocess.Popen(["omniroute", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2.0)
            return True, "Команда запуска OmniRoute отправлена в фоновом режиме"
        except Exception as e:
            return False, f"Не удалось запустить omniroute: {e}"

    def apply_all_fixes(self, fixes):
        results = []
        for fix in fixes:
            if fix == "fix_claude_settings":
                ok, msg = self.fix_claude_settings()
                results.append((ok, msg))
            elif fix == "restart_proxy":
                ok, msg = self.fix_kill_stale_proxies()
                results.append((ok, msg))
            elif fix == "switch_to_grok":
                ok, msg = self.fix_switch_model("grok-cli/grok-4.7-high")
                results.append((ok, msg))
            elif fix == "start_omniroute":
                ok, msg = self.fix_start_omniroute()
                results.append((ok, msg))
        return results


# =====================================================================
# CLI РЕЖИМ (Консольный доктор с интерактивным или авто-исправлением)
# =====================================================================
def run_cli(auto_fix=False):
    doc = DoctorEngine()
    healer = HealerEngine(doc)

    print(f"\n{Colors.BOLD}{Colors.CYAN}======================================================================{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}           🔍 OmniRoute + Claude Code Doctor & Auto-Healer            {Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}======================================================================{Colors.RESET}\n")
    print(f"Запуск полной системной диагностики...\n")

    report = doc.run_full_diagnosis()

    # 1. Интернет
    net = report["internet"]
    icon = f"{Colors.GREEN}[  OK  ]{Colors.RESET}" if net["ok"] else f"{Colors.RED}[СБОЙ! ]{Colors.RESET}"
    print(f"[1/6] 🌐 Интернет и DNS-серверы ......... {icon} {net['msg']}")

    # 2. OmniRoute
    omni = report["omniroute"]
    icon = f"{Colors.GREEN}[  OK  ]{Colors.RESET}" if omni["ok"] else f"{Colors.RED}[СБОЙ! ]{Colors.RESET}"
    print(f"[2/6] ⚙️  Служба OmniRoute (порт 20128) .. {icon} {omni['msg']}")

    # 3. Авторизация
    auth = report["auth"]
    icon = f"{Colors.GREEN}[  OK  ]{Colors.RESET}" if auth["ok"] else f"{Colors.RED}[СБОЙ! ]{Colors.RESET}"
    print(f"[3/6] 🔑 Ключ авторизации в OmniRoute ... {icon} {auth['msg']}")

    # 4. Прокси
    proxy = report["proxy"]
    icon = f"{Colors.GREEN}[  OK  ]{Colors.RESET}" if proxy["ok"] else f"{Colors.YELLOW}[ВНИМАНИЕ]{Colors.RESET}"
    print(f"[4/6] 🔄 Failover-прокси ................ {icon} {proxy['msg']}")

    # 5. Конфиг Claude Code
    cfg = report.get("config", {})
    if cfg:
        icon = f"{Colors.GREEN}[  OK  ]{Colors.RESET}" if cfg["ok"] else f"{Colors.RED}[ОШИБКА]{Colors.RESET}"
        print(f"[5/6] 🎯 Конфигурация Claude Code ....... {icon} {cfg['msg']}")

    # 6. Апстримы
    upstreams = report.get("upstreams", [])
    if upstreams:
        print(f"[6/6] 📡 Доступность провайдеров:")
        for u in upstreams:
            st = u["status"]
            u_icon = f"{Colors.GREEN}✅{Colors.RESET}" if u["ok"] else f"{Colors.RED}❌{Colors.RESET}"
            extra = f"({u['ms']}ms)" if u["ms"] > 0 else ""
            print(f"      • {u['name']:<18} -> {u_icon} {st:<18} {Colors.GRAY}{extra}{Colors.RESET}")

    print(f"\n{Colors.BOLD}{Colors.CYAN}----------------------------------------------------------------------{Colors.RESET}")

    issues = report["issues"]
    fixes = report["fixes"]

    if not issues:
        print(f"{Colors.BOLD}{Colors.GREEN}🎉 ВСЕ СИСТЕМЫ РАБОТАЮТ ИДЕАЛЬНО! Проблем не обнаружено.{Colors.RESET}")
        print(f"   Claude Code готов к приёму запросов.\n")
        return

    print(f"{Colors.BOLD}{Colors.YELLOW}📋 НАЙДЕННЫЕ ПРОБЛЕМЫ ({len(issues)} шт.):{Colors.RESET}")
    for severity, msg in issues:
        color = Colors.RED if severity == "CRITICAL" else Colors.YELLOW
        print(f" • {color}[{severity}]{Colors.RESET} {msg}")

    print(f"{Colors.BOLD}{Colors.CYAN}----------------------------------------------------------------------{Colors.RESET}\n")

    if not fixes:
        print(f"{Colors.GRAY}Автоматических рецептов для этих проблем нет (проверьте интернет вручную).{Colors.RESET}\n")
        return

    should_fix = auto_fix
    if not should_fix:
        try:
            choice = input(f"{Colors.BOLD}{Colors.GREEN}👉 Доктор готов автоматически устранить проблемы. Починить сейчас? [Y/n]: {Colors.RESET}").strip().lower()
            should_fix = choice in ("", "y", "yes", "д", "да")
        except (KeyboardInterrupt, EOFError):
            should_fix = False

    if should_fix:
        print(f"\n{Colors.BOLD}{Colors.CYAN}🛠️  Применение автоматических исправлений...{Colors.RESET}\n")
        results = healer.apply_all_fixes(fixes)
        for ok, msg in results:
            icon = f"{Colors.GREEN}✅{Colors.RESET}" if ok else f"{Colors.RED}❌{Colors.RESET}"
            print(f"  {icon} {msg}")
        print(f"\n{Colors.BOLD}{Colors.GREEN}🎉 Готово! Перезапустите Claude Code, чтобы применить новые настройки.{Colors.RESET}\n")


# =====================================================================
# GUI РЕЖИМ (Нативное графическое окно Windows / Mac)
# =====================================================================
def run_gui():
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except ImportError:
        print("Tkinter недоступен. Переключаемся в консольный режим CLI...")
        run_cli()
        return

    doc = DoctorEngine()
    healer = HealerEngine(doc)

    root = tk.Tk()
    root.title("OmniRoute + Claude Code Doctor & Auto-Healer")
    root.geometry("720x620")
    root.minsize(680, 560)

    # Цветовая гамма (стильная темная тема)
    BG = "#181825"
    CARD_BG = "#24273a"
    TEXT = "#cad3f5"
    SUBTEXT = "#a5adcb"
    ACCENT_GREEN = "#a6da95"
    ACCENT_BLUE = "#8aadf4"
    ACCENT_RED = "#ed8796"
    ACCENT_YELLOW = "#eed49f"

    root.configure(bg=BG)

    # Заголовок
    header_frame = tk.Frame(root, bg=CARD_BG, pady=12, padx=16)
    header_frame.pack(fill="x", padx=14, pady=10)

    title_label = tk.Label(
        header_frame,
        text="🏥 OmniRoute & Claude Code Doctor",
        font=("Segoe UI", 16, "bold"),
        fg=ACCENT_BLUE,
        bg=CARD_BG
    )
    title_label.pack(anchor="w")

    subtitle = tk.Label(
        header_frame,
        text="Автоматическая диагностика и устранение неполадок в 1 клик",
        font=("Segoe UI", 10),
        fg=SUBTEXT,
        bg=CARD_BG
    )
    subtitle.pack(anchor="w", pady=(2, 0))

    # Карточки статусов
    cards_frame = tk.Frame(root, bg=BG)
    cards_frame.pack(fill="x", padx=14, pady=4)

    status_labels = {}
    check_items = [
        ("internet", "🌐 Интернет и сеть:"),
        ("omniroute", "⚙️ Служба OmniRoute (20128):"),
        ("proxy", "🔄 Failover-прокси (9099):"),
        ("config", "🎯 Конфиг Claude Code:")
    ]

    for key, label_text in check_items:
        row = tk.Frame(cards_frame, bg=CARD_BG, padx=12, pady=8)
        row.pack(fill="x", pady=3)

        lbl = tk.Label(row, text=label_text, font=("Segoe UI", 10, "bold"), fg=TEXT, bg=CARD_BG, width=28, anchor="w")
        lbl.pack(side="left")

        val = tk.Label(row, text="Ожидание проверки...", font=("Segoe UI", 10), fg=SUBTEXT, bg=CARD_BG, anchor="w")
        val.pack(side="left", fill="x", expand=True)

        status_labels[key] = val

    # Текстовое поле лога и диагноза
    log_frame = tk.Frame(root, bg=CARD_BG, padx=8, pady=8)
    log_frame.pack(fill="both", expand=True, padx=14, pady=8)

    log_label = tk.Label(log_frame, text="📋 Подробный отчёт диагностики:", font=("Segoe UI", 10, "bold"), fg=TEXT, bg=CARD_BG)
    log_label.pack(anchor="w", pady=(0, 4))

    log_text = tk.Text(log_frame, bg="#1e2030", fg="#cad3f5", font=("Consolas", 9), relief="flat", wrap="word")
    log_text.pack(fill="both", expand=True)

    current_report = {"fixes": []}

    def append_log(txt, tag=None):
        log_text.insert("end", txt + "\n", tag)
        log_text.see("end")

    def run_check_thread():
        log_text.delete("1.0", "end")
        append_log("🚀 Запуск системной проверки...\n")
        for key in status_labels:
            status_labels[key].config(text="Проверяется...", fg=ACCENT_YELLOW)
        root.update()

        rep = doc.run_full_diagnosis()
        current_report["fixes"] = rep["fixes"]

        # 1. Интернет
        if rep["internet"]["ok"]:
            status_labels["internet"].config(text=f"OK ({rep['internet']['ms']}ms)", fg=ACCENT_GREEN)
        else:
            status_labels["internet"].config(text="СБОЙ", fg=ACCENT_RED)
        append_log(f"• Интернет: {rep['internet']['msg']}")

        # 2. OmniRoute
        if rep["omniroute"]["ok"]:
            status_labels["omniroute"].config(text="Работает", fg=ACCENT_GREEN)
        else:
            status_labels["omniroute"].config(text="Не отвечает", fg=ACCENT_RED)
        append_log(f"• OmniRoute: {rep['omniroute']['msg']}")

        # 3. Прокси
        if rep["proxy"]["ok"]:
            status_labels["proxy"].config(text=f"Порт {rep['proxy']['port']} активен", fg=ACCENT_GREEN)
        else:
            status_labels["proxy"].config(text="Не запущен", fg=ACCENT_YELLOW)
        append_log(f"• Прокси: {rep['proxy']['msg']}")

        # 4. Конфиг Claude Code
        cfg = rep.get("config", {})
        if cfg.get("ok"):
            status_labels["config"].config(text="Синхронизирован", fg=ACCENT_GREEN)
        else:
            status_labels["config"].config(text="Требует исправления!", fg=ACCENT_RED)
        append_log(f"• Конфиг Claude: {cfg.get('msg', 'N/A')}")

        # Апстримы
        append_log("\n📡 Проверка апстрим-моделей:")
        for u in rep.get("upstreams", []):
            st = u["status"]
            append_log(f"   - {u['name']}: {st} ({u['ms']}ms)")

        # Итог
        issues = rep["issues"]
        append_log("\n---------------------------------------------------------")
        if not issues:
            append_log("🎉 ВСЕ СИСТЕМЫ В НОРМЕ! Claude Code готов к работе.")
            btn_fix.config(state="disabled")
        else:
            append_log(f"⚠️ ОБНАРУЖЕНО ПРОБЛЕМ: {len(issues)}")
            for sev, msg in issues:
                append_log(f"   [{sev}] {msg}")
            if rep["fixes"]:
                btn_fix.config(state="normal")
                append_log("\n👉 Нажмите кнопку '🛠️ Починить всё', чтобы устранить неполадки.")

    def on_check():
        threading.Thread(target=run_check_thread, daemon=True).start()

    def on_fix():
        fixes = current_report.get("fixes", [])
        if not fixes:
            messagebox.showinfo("Доктор", "Проблем для авто-исправления не обнаружено!")
            return
        append_log("\n🛠️ ПРИМЕНЕНИЕ АВТОМАТИЧЕСКИХ ИСПРАВЛЕНИЙ...")
        results = healer.apply_all_fixes(fixes)
        for ok, msg in results:
            icon = "✅" if ok else "❌"
            append_log(f"   {icon} {msg}")
        append_log("\n🎉 Исправления завершены! Запуск повторной проверки...")
        on_check()

    def on_launch_claude():
        try:
            if os.name == "nt":
                bat_path = os.path.join(doc.script_dir, "run_claude.bat")
                subprocess.Popen(["cmd.exe", "/c", "start", bat_path], shell=True)
            else:
                sh_path = os.path.join(doc.script_dir, "run_claude.sh")
                subprocess.Popen(["bash", sh_path])
            messagebox.showinfo("Запуск", "Claude Code запущен во втором окне!")
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось запустить Claude Code: {e}")

    # Кнопки действий
    btn_frame = tk.Frame(root, bg=BG, pady=10)
    btn_frame.pack(fill="x", padx=14)

    btn_check = tk.Button(
        btn_frame,
        text="🔍 Проверить заново",
        font=("Segoe UI", 10, "bold"),
        bg=CARD_BG,
        fg=TEXT,
        padx=14,
        pady=6,
        relief="groove",
        command=on_check
    )
    btn_check.pack(side="left", padx=4)

    btn_fix = tk.Button(
        btn_frame,
        text="🛠️ Починить всё (Auto-Fix)",
        font=("Segoe UI", 10, "bold"),
        bg="#2d5a27",
        fg="#ffffff",
        padx=16,
        pady=6,
        relief="groove",
        command=on_fix
    )
    btn_fix.pack(side="left", padx=6)

    btn_claude = tk.Button(
        btn_frame,
        text="🚀 Запустить Claude Code",
        font=("Segoe UI", 10, "bold"),
        bg="#204060",
        fg="#ffffff",
        padx=14,
        pady=6,
        relief="groove",
        command=on_launch_claude
    )
    btn_claude.pack(side="right", padx=4)

    # Запускаем первую проверку автоматически при старте
    root.after(300, on_check)
    root.mainloop()


if __name__ == "__main__":
    if "--gui" in sys.argv:
        run_gui()
    elif "--fix" in sys.argv or "-f" in sys.argv:
        run_cli(auto_fix=True)
    else:
        # По умолчанию если передан аргумент GUI или запущено без аргументов в Windows
        if len(sys.argv) > 1 and sys.argv[1] == "gui":
            run_gui()
        else:
            run_cli(auto_fix=False)
