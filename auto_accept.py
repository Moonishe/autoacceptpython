#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-Accept for Dota 2 (Win32, чистый stdlib + ctypes).

Принцип: мониторим цвет пикселя в точке кнопки Accept (экранные координаты).
Как только пиксель совпадает с эталонным цветом кнопки -> клик SendInput.
Не требует лога, инжектов, модификации игры. Запускать вручную перед поиском матча.

Команды:
  python auto_accept.py calibrate  — записать координаты+цвет кнопки под курсором
  python auto_accept.py run        — начать мониторинг (по умолчанию)
  python auto_accept.py status     — показать текущий конфиг

Стоп во время run: Ctrl+Shift+Q (глобальный хоткей) или закрыть консоль.
"""

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

# Корректная кириллица в консоли Windows (PowerShell 5.1 -> UTF-8)
for _stream in (sys.stdout, sys.stderr):
    _rec = getattr(_stream, "reconfigure", None)
    if _rec is not None:
        try:
            _rec(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass

# ---------------------------------------------------------------------------
# Win32 setup
# ---------------------------------------------------------------------------
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# SendInput structures
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_input",)
    _fields_ = [("type", wintypes.DWORD), ("_input", _INPUT_UNION)]


# Hotkey constants
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
WM_HOTKEY = 0x0312
HOTKEY_ID = 9001

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "auto_accept_config.json"
)
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_accept.log")

DEFAULTS = {
    "tolerance": 35,  # max |dR|+|dG|+|dB| = tolerance*3
    "interval_ms": 350,  # как часто проверять пиксель
    "debounce_ms": 8000,  # пауза после клика (защита от повторных)
    "require_dota_focus": True,  # кликать только если Dota в фокусе
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_pixel(dc, x: int, y: int):
    """Вернуть (r,g,b) пикселя экрана в координатах (x,y)."""
    color = gdi32.GetPixel(dc, x, y)  # COLORREF = 0x00BBGGRR
    if color == 0xFFFFFFFF:  # CLR_INVALID (координата вне экрана)
        return None
    return (color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF)


def get_cursor_pos():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def get_foreground_window_title() -> str:
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def click(x: int, y: int) -> None:
    """Клик левой кнопкой в экранных координатах через SendInput."""
    user32.SetCursorPos(x, y)
    time.sleep(0.02)
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dx = 0
    inp.mi.dy = 0
    inp.mi.mouseData = 0
    inp.mi.time = 0
    inp.mi.dwExtraInfo = None
    inp.mi.dwFlags = MOUSEEVENTF_LEFTDOWN
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    time.sleep(0.03)
    inp.mi.dwFlags = MOUSEEVENTF_LEFTUP
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def color_close(px, target, tolerance: int) -> bool:
    if px is None:
        return False
    return (
        abs(px[0] - target[0]) + abs(px[1] - target[1]) + abs(px[2] - target[2])
    ) <= tolerance * 3


# ---------------------------------------------------------------------------
# Hotkey thread (Ctrl+Shift+Q -> стоп)
# ---------------------------------------------------------------------------
_stop_event = threading.Event()


def _hotkey_loop() -> None:
    ok = user32.RegisterHotKey(
        None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT, 0x51
    )  # 0x51 = 'Q'
    if not ok:
        log("WARN: RegisterHotKey не сработал — стоп только по закрытию консоли.")
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
            _stop_event.set()
            break
    user32.UnregisterHotKey(None, HOTKEY_ID)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_calibrate() -> int:
    print("=== Калибровка кнопки Accept ===")
    print(
        "1) В Dota 2: найди матч ДО появления кнопки Accept (или поставь курсор туда,"
    )
    print("   где она появляется). Наведи курсор точно на ЦЕНТР будущей кнопки.")
    print("2) Не двигай курсор. Через 5 секунд запишу координаты + цвет пикселя.")
    print()
    for i in range(5, 0, -1):
        print(f"\r  Осталось: {i} сек...  ", end="", flush=True)
        time.sleep(1)
    print("\r  Готово.                   ")

    x, y = get_cursor_pos()
    dc = user32.GetDC(0)
    px = get_pixel(dc, x, y)
    user32.ReleaseDC(0, dc)
    if px is None:
        print("ОШИБКА: не удалось прочитать пиксель (координаты вне экрана?).")
        return 1

    cfg = {
        "x": x,
        "y": y,
        "color": list(px),
        "tolerance": DEFAULTS["tolerance"],
        "interval_ms": DEFAULTS["interval_ms"],
        "debounce_ms": DEFAULTS["debounce_ms"],
        "require_dota_focus": DEFAULTS["require_dota_focus"],
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    print(f"Координаты: x={x}, y={y}")
    print(f"Цвет кнопки: RGB{px}  (hex #{px[0]:02X}{px[1]:02X}{px[2]:02X})")
    print(f"Конфиг сохранён: {CONFIG_PATH}")
    print()
    print("ВАЖНО: цвет записан с ТЕКУЩЕГО экрана. Если кнопка сейчас НЕ видна —")
    print("записался цвет фона. Перекалибруй, когда кнопка Accept реально на экране.")
    return 0


def cmd_status() -> int:
    if not os.path.exists(CONFIG_PATH):
        print(f"Конфиг не найден: {CONFIG_PATH}")
        print("Сначала запусти: python auto_accept.py calibrate")
        return 1
    # utf-8-sig съедает BOM, если конфиг правили в Notepad/PowerShell (Set-Content -Encoding UTF8)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ОШИБКА: конфиг повреждён ({CONFIG_PATH}): {e}")
        print("Удали его и запусти заново: python auto_accept.py calibrate")
        return 1
    print(f"Конфиг: {CONFIG_PATH}")
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    # обязательные поля
    missing = [k for k in ("x", "y", "color") if k not in cfg]
    if missing:
        print(f"\nОШИБКА: в конфиге нет обязательных полей: {missing}")
        print("Удали конфиг и запусти: python auto_accept.py calibrate")
        return 1
    if not (isinstance(cfg["color"], (list, tuple)) and len(cfg["color"]) == 3):
        print("\nОШИБКА: поле 'color' должно быть списком из 3 чисел [r,g,b].")
        return 1
    r, g, b = cfg["color"]
    print(
        f"\nКнопка: ({cfg['x']},{cfg['y']})  RGB({r},{g},{b})  #{r:02X}{g:02X}{b:02X}"
    )
    print(
        f"Проверка раз в {cfg.get('interval_ms', DEFAULTS['interval_ms'])}мс, "
        f"debounce {cfg.get('debounce_ms', DEFAULTS['debounce_ms'])}мс, "
        f"tolerance {cfg.get('tolerance', DEFAULTS['tolerance'])}, "
        f"dota_focus={cfg.get('require_dota_focus', True)}"
    )
    return 0


def cmd_run() -> int:
    if not os.path.exists(CONFIG_PATH):
        print("Конфиг не найден. Сначала: python auto_accept.py calibrate")
        return 1
    # utf-8-sig съедает BOM, если конфиг правили в Notepad/PowerShell (Set-Content -Encoding UTF8)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"ОШИБКА: конфиг повреждён ({CONFIG_PATH}): {e}")
        print("Удали его и запусти заново: python auto_accept.py calibrate")
        return 1
    # обязательные поля
    missing = [k for k in ("x", "y", "color") if k not in cfg]
    if missing:
        print(f"ОШИБКА: в конфиге нет обязательных полей: {missing}")
        print("Удали конфиг и запусти: python auto_accept.py calibrate")
        return 1
    if not (isinstance(cfg["color"], (list, tuple)) and len(cfg["color"]) == 3):
        print("ОШИБКА: поле 'color' должно быть списком из 3 чисел [r,g,b].")
        return 1

    x, y = cfg["x"], cfg["y"]
    target = tuple(cfg["color"])
    tol = cfg.get("tolerance", DEFAULTS["tolerance"])
    interval = cfg.get("interval_ms", DEFAULTS["interval_ms"]) / 1000.0
    debounce = cfg.get("debounce_ms", DEFAULTS["debounce_ms"]) / 1000.0
    require_focus = cfg.get("require_dota_focus", True)

    # Запуск хоткея стопа
    t = threading.Thread(target=_hotkey_loop, daemon=True)
    t.start()

    last_click = 0.0
    log(f"Мониторинг запущен. Кнопка ({x},{y}) RGB{target} tol={tol}.")
    log("Стоп: Ctrl+Shift+Q или закрыть окно.")
    print("(мониторинг идёт, логи в auto_accept.log)\n", flush=True)

    dc = None
    try:
        dc = user32.GetDC(0)
        if not dc:
            log("ОШИБКА: GetDC(0) вернул 0 — не могу читать пиксели экрана.")
            return 1
        while not _stop_event.is_set():
            px = get_pixel(dc, x, y)
            if color_close(px, target, tol):
                now = time.monotonic()
                if now - last_click > debounce:
                    title = get_foreground_window_title()
                    if require_focus and title != "Dota 2":
                        # Dota не в фокусе — пропускаем клик, но логируем
                        time.sleep(interval)
                        continue
                    click(x, y)
                    last_click = now
                    log(f"ACCEPT! клик ({x},{y}) — пиксель RGB{px}, окно='{title}'")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if dc:
            user32.ReleaseDC(0, dc)
        log("Мониторинг остановлен.")
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Auto-Accept кнопки Dota 2 (Win32, без инжектов)."
    )
    p.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["calibrate", "run", "status"],
        help="calibrate — записать точку/цвет кнопки; run — мониторинг (по умолчанию); status — показать конфиг",
    )
    args = p.parse_args()
    if args.command == "calibrate":
        return cmd_calibrate()
    if args.command == "status":
        return cmd_status()
    return cmd_run()


if __name__ == "__main__":
    sys.exit(main())
