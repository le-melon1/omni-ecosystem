# Omni Ecosystem Sync & Conversion Hub

Этот репозиторий содержит инструменты для синхронизации проектов, управления мультимодельными прокси (OmniRoute / OpenClaw) и конвертации аудио-проектов между Logic Pro и Ableton Live.

---

## 📂 Структура проекта
- `omni_claude.py` / `omni_openclaw.py` — умные прокси-оркестраторы с авто-фаиловером.
- `scripts/` — вспомогательные скрипты автоматизации и конвертации.
- `active_pool.json` — конфигурация пула моделей с горячей сменой.

---

## 🔄 Как работать с Git (Синхронизация Mac ⇄ Windows)

### 1. Первичная настройка (на любом из компьютеров):
```bash
git init
git remote add origin https://github.com/ВАШ_НИК/omni-ecosystem.git
git branch -M main
git add .
git commit -m "init: initial project commit"
git push -u origin main
```

### 2. Ежедневный рабочий процесс:
* **Перед началом работы** (подтягиваем свежие изменения с другого ПК):
  ```bash
  git pull origin main
  ```
* **После внесения правок** (сохраняем и отправляем в облако):
  ```bash
  git add .
  git commit -m "update: описание ваших изменений"
  git push origin main
  ```
