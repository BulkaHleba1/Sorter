#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Автоматический организатор загрузок.
Режимы: TUI (по умолчанию) или фоновый демон (--daemon).
"""

import sys
import os
import json
import logging
import time
import subprocess
import signal
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set

# watchdog
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("Ошибка: установите 'watchdog' (pip install watchdog)")
    sys.exit(1)

# ========== Глобальные константы ==========
CONFIG_FILE = Path.home() / ".organizer_config.json"
DEFAULT_CONFIG = {
    "watch_folder": str(Path.home() / "Загрузки"),
    "rules": {
        "jpg": "Images",
        "jpeg": "Images",
        "png": "Images",
        "gif": "Images",
        "bmp": "Images",
        "pdf": "Docs",
        "doc": "Docs",
        "docx": "Docs",
        "txt": "Texts",
        "md": "Texts",
        "zip": "Archives",
        "rar": "Archives",
        "7z": "Archives",
        "tar": "Archives",
        "gz": "Archives",
        "mp3": "Music",
        "wav": "Music",
        "flac": "Music",
        "mp4": "Videos",
        "avi": "Videos",
        "mkv": "Videos",
        "torrent": "Torrents"
    },
    "unknown_folder": "unknown",
    "ignore_extensions": ["part", "crdownload", "tmp"],
    "ignore_files": [".DS_Store", "Thumbs.db", "desktop.ini"],
    "log_file": str(Path.home() / ".download_organizer.log")
}
PID_FILE = Path(tempfile.gettempdir()) / "organizer.pid"
STOP_FLAG_FILE = Path(tempfile.gettempdir()) / "organizer.stop"

# ========== Вспомогательные функции ==========
def load_config() -> dict:
    """Загружает конфиг из JSON, если нет – создаёт со значениями по умолчанию."""
    if not CONFIG_FILE.exists():
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4)
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(config: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

def setup_logging(log_file_path: str):
    """Настраивает логгер: файл + консоль (для отладки демона)."""
    logger = logging.getLogger("Organizer")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # Файловый обработчик
    fh = logging.FileHandler(log_file_path, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Консольный обработчик (для TUI и для демона, если он не откреплён)
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    return logger

def get_target_folder(ext: str, rules: dict, unknown_folder: str) -> str:
    """Возвращает имя целевой папки для данного расширения (без пути)."""
    ext_lower = ext.lower()
    return rules.get(ext_lower, unknown_folder)

def resolve_conflict(target_path: Path) -> Path:
    """
    Если файл уже существует, добавляет суффикс _1, _2...
    Возвращает новый свободный путь.
    """
    if not target_path.exists():
        return target_path
    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent
    counter = 1
    while True:
        new_name = f"{stem}_{counter}{suffix}"
        new_path = parent / new_name
        if not new_path.exists():
            return new_path
        counter += 1

def is_ignored(file_path: Path, ignore_exts: List[str], ignore_files: List[str]) -> bool:
    """Проверяет, нужно ли игнорировать файл."""
    # Скрытые файлы (начинаются с точки)
    if file_path.name.startswith("."):
        return True
    # Игнор по имени
    if file_path.name in ignore_files:
        return True
    # Игнор по расширению
    ext = file_path.suffix.lstrip(".").lower()
    if ext in ignore_exts:
        return True
    return False

def move_file(file_path: Path, config: dict, logger: logging.Logger) -> bool:
    """
    Перемещает файл согласно правилам.
    Возвращает True при успехе, False при ошибке (логирует).
    """
    try:
        # Проверка игнорирования
        if is_ignored(file_path, config["ignore_extensions"], config["ignore_files"]):
            logger.info(f"Игнорируем файл: {file_path}")
            return False

        # Определение целевой папки
        ext = file_path.suffix.lstrip(".").lower()
        target_folder_name = get_target_folder(ext, config["rules"], config["unknown_folder"])
        watch_folder = Path(config["watch_folder"])
        target_dir = watch_folder / target_folder_name

        # Создаём целевую папку, если не существует
        target_dir.mkdir(parents=True, exist_ok=True)

        # Формируем целевой путь
        target_path = target_dir / file_path.name
        target_path = resolve_conflict(target_path)

        # Перемещение
        shutil.move(str(file_path), str(target_path))
        logger.info(f"Перемещён: {file_path} -> {target_path}")
        return True
    except PermissionError:
        logger.error(f"Нет прав на запись или перемещение: {file_path}")
        return False
    except Exception as e:
        logger.error(f"Ошибка при перемещении {file_path}: {e}")
        return False

def sort_existing_files(config: dict, logger: logging.Logger) -> None:
    """
    Рекурсивно обрабатывает все файлы в watch_folder (кроме уже находящихся в целевых папках).
    """
    watch_folder = Path(config["watch_folder"])
    # Целевые папки (куда перемещаем) не должны быть обработаны повторно
    target_dirs = set(config["rules"].values())
    target_dirs.add(config["unknown_folder"])
    target_dirs = {watch_folder / d for d in target_dirs}

    for item in watch_folder.rglob("*"):
        if item.is_file():
            # Пропускаем файлы внутри целевых папок
            if any(target_dir in item.parents for target_dir in target_dirs):
                continue
            move_file(item, config, logger)

# ========== Демон ==========
class OrganizerHandler(FileSystemEventHandler):
    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger
        super().__init__()

    def on_created(self, event):
        if not event.is_directory:
            self.logger.info(f"Обнаружен новый файл: {event.src_path}")
            move_file(Path(event.src_path), self.config, self.logger)

    def on_moved(self, event):
        if not event.is_directory:
            self.logger.info(f"Обнаружено перемещение/переименование: {event.src_path} -> {event.dest_path}")
            # Обрабатываем только если файл оказался в отслеживаемой папке
            watch = Path(self.config["watch_folder"])
            if Path(event.dest_path).parent == watch:
                move_file(Path(event.dest_path), self.config, self.logger)

def run_daemon():
    """Запускает демона (однократная сортировка + слежение)."""
    # Загружаем конфиг
    config = load_config()
    # Настраиваем логгер
    logger = setup_logging(config["log_file"])
    logger.info("=== Демон организатора загрузок запущен ===")

    # Сначала сортируем все существующие файлы
    logger.info("Начало однократной сортировки существующих файлов...")
    sort_existing_files(config, logger)
    logger.info("Однократная сортировка завершена.")

    # Настройка наблюдателя
    watch_path = config["watch_folder"]
    if not os.path.exists(watch_path):
        os.makedirs(watch_path, exist_ok=True)
        logger.info(f"Создана отслеживаемая папка: {watch_path}")

    event_handler = OrganizerHandler(config, logger)
    observer = Observer()
    observer.schedule(event_handler, watch_path, recursive=True)
    observer.start()
    logger.info(f"Начато слежение за папкой: {watch_path}")

    # Ждём сигнал остановки (файл-флаг)
    try:
        while not STOP_FLAG_FILE.exists():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        logger.info("Демон остановлен.")
        # Удаляем PID-файл
        if PID_FILE.exists():
            PID_FILE.unlink()

def is_daemon_running() -> bool:
    """Проверяет, запущен ли демон, по PID-файлу."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        # Проверяем существование процесса
        if sys.platform == "win32":
            # Windows: tasklist /FI "PID eq ..."
            result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
            return pid in result.stdout
        else:
            os.kill(pid, 0)  # Сигнал 0 не убивает, только проверяет существование
            return True
    except (ValueError, ProcessLookupError, OSError):
        return False

def start_daemon():
    """Запускает демона в отдельном процессе."""
    if is_daemon_running():
        print("Демон уже запущен.")
        return
    # Очищаем флаг остановки, если он остался
    if STOP_FLAG_FILE.exists():
        STOP_FLAG_FILE.unlink()
    # Запускаем дочерний процесс
    cmd = [sys.executable, __file__, "--daemon"]
    if sys.platform == "win32":
        # Windows: используем DETACHED_PROCESS (не привязываем к консоли)
        subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    # Даём время на запись PID
    time.sleep(1)
    if is_daemon_running():
        print("Демон успешно запущен.")
    else:
        print("Не удалось запустить демон. Проверьте лог.")

def stop_daemon():
    """Останавливает демона, создавая файл-флаг."""
    if not is_daemon_running():
        print("Демон не запущен.")
        return
    # Создаём флаг остановки
    STOP_FLAG_FILE.touch()
    print("Сигнал остановки отправлен. Демон завершится в течение нескольких секунд.")
    # Ждём исчезновения PID-файла
    for _ in range(10):
        if not PID_FILE.exists():
            print("Демон остановлен.")
            return
        time.sleep(1)
    print("Демон, возможно, не остановился. Попробуйте принудительно убить процесс.")

def show_log(config: dict):
    """Показывает последние 20 строк лога."""
    log_path = config.get("log_file", DEFAULT_CONFIG["log_file"])
    if not os.path.exists(log_path):
        print("Лог-файл ещё не создан.")
        return
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        last_lines = lines[-20:] if len(lines) > 20 else lines
        print("\n".join(last_lines))
    except Exception as e:
        print(f"Ошибка чтения лога: {e}")

def edit_config():
    """Открывает конфиг в редакторе $EDITOR."""
    editor = os.environ.get("EDITOR", "nano" if sys.platform != "win32" else "notepad")
    try:
        subprocess.call([editor, str(CONFIG_FILE)])
    except Exception as e:
        print(f"Не удалось запустить редактор: {e}")

def create_systemd_unit():
    """Создаёт systemd --user юнит для автозапуска демона (только Linux)."""
    if sys.platform == "win32":
        print("systemd поддерживается только в Linux.")
        return
    unit_name = "download-organizer.service"
    unit_path = Path.home() / ".config/systemd/user" / unit_name
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    python_path = sys.executable
    script_path = Path(__file__).resolve()
    unit_content = f"""[Unit]
Description=Download Organizer Daemon
After=network.target

[Service]
Type=forking
ExecStart={python_path} {script_path} --daemon
ExecStop={python_path} -c "import pathlib; (pathlib.Path(tempfile.gettempdir()) / 'organizer.stop').touch()"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit_content)
    print(f"Юнит создан: {unit_path}")
    print("Выполните следующие команды для активации автозапуска:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable download-organizer.service")
    print("  systemctl --user start download-organizer.service")

# ========== TUI ==========
def tui_menu():
    config = load_config()
    while True:
        print("\n" + "=" * 50)
        print("     Автоматический организатор загрузок")
        print("=" * 50)
        print(f"Отслеживаемая папка: {config['watch_folder']}")
        print(f"Демон запущен: {'Да' if is_daemon_running() else 'Нет'}")
        print("-" * 50)
        print("1. Запустить демона")
        print("2. Остановить демона")
        print("3. Посмотреть последние строки лога")
        print("4. Редактировать конфиг (JSON)")
        print("5. Создать systemd --user юнит (Linux)")
        print("6. Выход")
        choice = input("Ваш выбор: ").strip()

        if choice == "1":
            start_daemon()
        elif choice == "2":
            stop_daemon()
        elif choice == "3":
            show_log(config)
        elif choice == "4":
            edit_config()
            # Перезагружаем конфиг после редактирования
            config = load_config()
        elif choice == "5":
            create_systemd_unit()
        elif choice == "6":
            print("До свидания!")
            break
        else:
            print("Неверный выбор, попробуйте снова.")

# ========== Точка входа ==========
if __name__ == "__main__":
    if "--daemon" in sys.argv:
        # Запуск в режиме демона
        # Записываем PID перед запуском
        PID_FILE.write_text(str(os.getpid()))
        run_daemon()
    else:
        tui_menu()