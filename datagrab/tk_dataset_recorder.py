#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tk Dataset Recorder — простая панель для сборщика.

Готов к упаковке PyInstaller (находит рекордер рядом с exe/app).
Нет остановки по Esc. Окно сворачивается при старте, разворачивается после стопа.
Меню «Датасет»: экспорт в ZIP и очистка. Индикатор размера папки.

ENV (опционально):
  DATASET_ROOT=./dataset
  OPERATOR_NAME="Имя сборщика"
  STOP_KEY=""                 # пусто → не передавать хоткей в рекордер
  HIDE_ON_START=1             # 1 — сворачивать окно при старте
  RECORD_START_DELAY_MS=250
  RECORDER_BIN=/abs/path/to/datagrabber_69(.exe)  # если надо указать явно
"""
from __future__ import annotations

import json, os, platform, signal, subprocess, sys, threading, time, shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# -------- задачи --------
@dataclass
class Task:
    task_id: str
    text: str

class TaskProvider:
    def get_next_task(self) -> Optional[Task]: raise NotImplementedError
    def submit_result(self, task_id: str, rec_id: str, meta: Dict[str, Any]) -> None: raise NotImplementedError

class LocalListTaskProvider(TaskProvider):
    def __init__(self, tasks: List[str]):
        self.tasks = [t for t in tasks if str(t).strip()]; self.idx = 0
    def get_next_task(self) -> Optional[Task]:
        if self.idx >= len(self.tasks): return None
        t = self.tasks[self.idx]; self.idx += 1
        return Task(task_id=f"local_{self.idx}", text=t)
    def submit_result(self, task_id: str, rec_id: str, meta: Dict[str, Any]) -> None: return

# -------- GUI --------
class App(tk.Tk):
    def __init__(self, provider: TaskProvider):
        super().__init__()
        self.title("Dataset Recorder")
        self.geometry("540x360"); self.minsize(500, 330)

        self.provider = provider
        self.current_task: Optional[Task] = None
        self.rec_proc: Optional[subprocess.Popen] = None
        self.recording = False
        self.rec_start_time: Optional[float] = None
        self._timer_job = None

        self.dataset_root_dir = Path(os.environ.get("DATASET_ROOT", "./dataset")).resolve()
        self.operator = self._detect_operator()
        self._stop_key = os.environ.get("STOP_KEY", "").strip()
        if self._stop_key.upper() == "ESC":  # принудительно запрещаем Esc
            self._stop_key = ""
        self.auto_minimize = os.environ.get("HIDE_ON_START", "1") != "0"
        self.start_delay_ms = int(os.environ.get("RECORD_START_DELAY_MS", "250"))

        self._init_styles()
        self._build_ui()
        self._bind_hotkeys()

        self._size_thread_running = False; self._size_job = None
        self._schedule_size_tick()

        self._fetch_and_show_next_task()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- стили ----
    def _init_styles(self):
        self.style = ttk.Style(self)
        try: self.style.theme_use("clam")
        except Exception: pass
        self.style.configure("Start.TButton", background="#22c55e", foreground="white",
                             padding=8, font=("SF Pro Text", 12, "bold"))
        self.style.map("Start.TButton", background=[("active","#16a34a"),("disabled","#86efac")])
        self.style.configure("Stop.TButton", background="#ef4444", foreground="white",
                             padding=8, font=("SF Pro Text", 12, "bold"))
        self.style.map("Stop.TButton", background=[("active","#dc2626"),("disabled","#fca5a5")])

    # ---- UI ----
    def _build_ui(self):
        box = ttk.LabelFrame(self, text="Задание")
        box.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10,6))
        self.task_text = tk.Text(box, height=6, wrap=tk.WORD)
        self.task_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.task_text.configure(state=tk.DISABLED)

        status = ttk.Frame(self); status.pack(fill=tk.X, padx=10, pady=(4,6))
        self.status_var = tk.StringVar(value="Готово"); ttk.Label(status, textvariable=self.status_var).pack(side=tk.LEFT)
        self.timer_var = tk.StringVar(value="00:00")
        ttk.Label(status, text=" | ").pack(side=tk.LEFT); ttk.Label(status, text="Время:").pack(side=tk.LEFT)
        ttk.Label(status, textvariable=self.timer_var).pack(side=tk.LEFT)

        right = ttk.Frame(status); right.pack(side=tk.RIGHT)
        self.dataset_size_var = tk.StringVar(value="Размер: —")
        ttk.Label(right, textvariable=self.dataset_size_var).pack(side=tk.LEFT, padx=(0,8))
        ds_menu_btn = tk.Menubutton(right, text="Датасет ▾")
        ds_menu = tk.Menu(ds_menu_btn, tearoff=0)
        ds_menu.add_command(label="Экспорт в ZIP…", command=self._export_zip)
        ds_menu.add_command(label="Очистить…", command=self._clear_dataset)
        ds_menu_btn.config(menu=ds_menu); ds_menu_btn.pack(side=tk.LEFT)

        btns = ttk.Frame(self); btns.pack(fill=tk.X, padx=10, pady=(2,10))
        self.btn_start = ttk.Button(btns, text="Начать запись", style="Start.TButton",
                                    command=self.on_start, takefocus=True)
        self.btn_start.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self.btn_finish = ttk.Button(btns, text="Завершить и отправить", style="Stop.TButton",
                                     command=self.on_finish, takefocus=True)
        self.btn_finish.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(8,0))
        self.btn_finish.state(["disabled"])

    # ---- хоткеи ----
    def _bind_hotkeys(self):
        # Esc не используем. Оставим (опционально) Cmd+. на macOS:
        if platform.system() == "Darwin":
            self.bind_all("<Command-period>", lambda e: self.on_finish())

    # ---- задачи ----
    def _fetch_and_show_next_task(self):
        self.current_task = self.provider.get_next_task()
        self._set_task_text(self.current_task.text if self.current_task else "Задачи закончились. Спасибо!")
        self.btn_start.state(["!disabled"] if self.current_task else ["disabled"])

    def _set_task_text(self, text: str):
        self.task_text.configure(state=tk.NORMAL); self.task_text.delete("1.0", tk.END)
        self.task_text.insert(tk.END, text); self.task_text.configure(state=tk.DISABLED)

    # ---- старт ----
    def on_start(self):
        if self.rec_proc is not None or not self.current_task: return

        rec_bin = self._find_recorder_binary()
        if not rec_bin:
            messagebox.showerror("Рекордер не найден",
                "Не найден ни datagrabber_69(.exe), ни datagrabber_69 рядом, ни datagrabber_69.py")
            return

        rec_id = f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        rec_dir = self.dataset_root_dir / rec_id

        # Сформировать команду (поддерживаем как бинарь, так и .py)
        if rec_bin.suffix.lower() == ".py":
            cmd = [sys.executable, str(rec_bin)]
        else:
            cmd = [str(rec_bin)]
        cmd += ["--rec-id", rec_id, "--task", self.current_task.text]

        if self._stop_key:  # по умолчанию пусто → не передаём
            cmd += ["--stop-key", self._stop_key]
        if self.operator:
            cmd += ["--operator", self.operator]

        if platform.system() == "Darwin":
            print("[INFO] macOS: если запись не стартует, дайте права в Privacy & Security → "
                  "Screen Recording / Input Monitoring / Accessibility")

        if self.auto_minimize:
            try: self.iconify(); self.update_idletasks(); time.sleep(self.start_delay_ms/1000.0)
            except Exception: pass

        try:
            self.rec_proc = subprocess.Popen(cmd, start_new_session=True)
        except Exception as e:
            try: self.deiconify(); self.lift(); self.focus_force()
            except Exception: pass
            messagebox.showerror("Не удалось запустить запись", str(e)); return

        self.recording = True
        self._current_rec = {"rec_id": rec_id, "rec_dir": str(rec_dir)}
        self.rec_start_time = time.time(); self._tick_timer()
        self.btn_start.state(["disabled"]); self.btn_finish.state(["!disabled"])
        self.btn_finish.focus_set()
        self.status_var.set(f"Запись идёт → {rec_id}")

        def watcher():
            if self.rec_proc is None: return
            rc = self.rec_proc.wait()
            self.after(0, lambda: self._on_recorder_stopped(rc, rec_id))
        threading.Thread(target=watcher, daemon=True).start()

    # ---- стоп ----
    def on_finish(self):
        if self.rec_proc is None:
            self.status_var.set("Нет активной записи"); return

        self.status_var.set("Завершаю запись…")

        # аварийный .stop
        try:
            rec_dir = Path(self._current_rec.get("rec_dir","")) if hasattr(self,"_current_rec") else None
            if rec_dir: rec_dir.mkdir(parents=True, exist_ok=True); (rec_dir / ".stop").touch()
        except Exception: pass

        # сигналы в группу
        try:
            import os; pgid = os.getpgid(self.rec_proc.pid)
        except Exception:
            pgid = None

        def kill_group(sig):
            try:
                if pgid is not None: os.killpg(pgid, sig)
                else: self.rec_proc.send_signal(sig)
            except Exception: pass

        try:
            if self.rec_proc.poll() is None:
                kill_group(signal.SIGINT)
                for _ in range(30):
                    if self.rec_proc.poll() is not None: break
                    time.sleep(0.1)
            if self.rec_proc.poll() is None:
                kill_group(signal.SIGTERM)
                for _ in range(30):
                    if self.rec_proc.poll() is not None: break
                    time.sleep(0.1)
            if self.rec_proc.poll() is None:
                kill_group(signal.SIGKILL)
        except Exception:
            pass

    # ---- экспорт/очистка ----
    def _export_zip(self):
        default_name = f"dataset_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        save_path = filedialog.asksaveasfilename(
            title="Экспорт датасета в ZIP", defaultextension=".zip",
            initialfile=default_name, filetypes=[("ZIP archive","*.zip")]
        )
        if not save_path: return
        def worker():
            try:
                self._set_dataset_actions_enabled(False); self.status_var.set("Экспортирую в ZIP…")
                base = Path(save_path); base_no_ext = str(base.with_suffix(""))
                shutil.make_archive(base_no_ext, "zip",
                    root_dir=str(self.dataset_root_dir.parent), base_dir=self.dataset_root_dir.name)
                self.after(0, lambda: messagebox.showinfo("Готово", f"Экспортировано:\n{save_path}"))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Ошибка экспорта", str(e)))
            finally:
                self.after(0, lambda: (self._set_dataset_actions_enabled(True), self.status_var.set("Готово")))
        threading.Thread(target=worker, daemon=True).start()

    def _clear_dataset(self):
        if self.rec_proc is not None and self.rec_proc.poll() is None:
            messagebox.showwarning("Идёт запись", "Нельзя очищать датасет во время записи."); return
        if not self.dataset_root_dir.exists():
            messagebox.showinfo("Пусто", "Папка датасета ещё не создана."); return
        ok = messagebox.askyesno("Очистить датасет?",
                                 f"Будут удалены ВСЕ записи в:\n{self.dataset_root_dir}\n\nПродолжить?",
                                 icon="warning")
        if not ok: return
        def worker():
            try:
                self._set_dataset_actions_enabled(False); self.status_var.set("Очищаю датасет…")
                for entry in self.dataset_root_dir.iterdir():
                    try:
                        if entry.is_dir(): shutil.rmtree(entry)
                        else: entry.unlink()
                    except Exception: pass
                self.after(0, lambda: messagebox.showinfo("Готово", "Датасет очищен."))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Ошибка очистки", str(e)))
            finally:
                self.after(0, lambda: (self._set_dataset_actions_enabled(True), self.status_var.set("Готово")))
        threading.Thread(target=worker, daemon=True).start()

    def _set_dataset_actions_enabled(self, enabled: bool):
        self.btn_start.state(["!disabled"] if (enabled and self.current_task and self.rec_proc is None) else ["disabled"])
        self.btn_finish.state(["!disabled"] if (enabled and self.rec_proc is not None) else ["disabled"])

    # ---- размер датасета ----
    def _schedule_size_tick(self): self._size_job = self.after(2000, self._size_tick)
    def _size_tick(self):
        if self._size_thread_running: return self._schedule_size_tick()
        def worker():
            self._size_thread_running = True
            try:
                size = self._dir_size_bytes(self.dataset_root_dir)
                self.after(0, lambda: self.dataset_size_var.set(f"Размер: {self._format_size(size)}"))
            finally:
                self._size_thread_running = False; self._schedule_size_tick()
        threading.Thread(target=worker, daemon=True).start()
    @staticmethod
    def _dir_size_bytes(path: Path) -> int:
        if not path.exists(): return 0
        total = 0; stack = [path]
        while stack:
            p = stack.pop()
            try:
                with os.scandir(p) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink(): continue
                            if entry.is_dir(follow_symlinks=False): stack.append(Path(entry.path))
                            else: total += entry.stat(follow_symlinks=False).st_size
                        except Exception: continue
            except Exception: continue
        return total
    @staticmethod
    def _format_size(n: int) -> str:
        units = ["B","KB","MB","GB","TB"]; i=0; val=float(n)
        while val>=1024 and i<len(units)-1: val/=1024.0; i+=1
        return f"{val:.1f} {units[i]}" if i>0 else f"{int(val)} {units[i]}"

    # ---- завершение ----
    def _on_recorder_stopped(self, returncode: Optional[int], rec_id: str):
        try: self.deiconify(); self.lift(); self.focus_force()
        except Exception: pass

        self.recording = False
        if self._timer_job is not None:
            try: self.after_cancel(self._timer_job)
            except Exception: pass
            self._timer_job = None
        self.timer_var.set("00:00"); self.btn_finish.state(["disabled"])

        if returncode not in (None, 0, -2):
            if returncode == -5:
                messagebox.showerror("Нет прав (macOS)",
                    "SIGTRAP: вероятно, нет прав Screen Recording/Input Monitoring/Accessibility.\n"
                    "System Settings → Privacy & Security → добавьте Terminal/PyCharm и перезапустите их.")
            else:
                messagebox.showerror("Ошибка записи", f"Дочерний процесс завершился с кодом: {returncode}")

        # submit
        try:
            rec_dir = self.dataset_root_dir / rec_id
            meta_path = rec_dir / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
            if self.current_task: self.provider.submit_result(self.current_task.task_id, rec_id, meta)
        except Exception: pass

        self._fetch_and_show_next_task()
        self.status_var.set("Готово")
        self.btn_start.state(["!disabled"] if self.current_task else ["disabled"])
        self.rec_proc = None

    # ---- поиск рекордера ----
    def _find_recorder_binary(self) -> Optional[Path]:
        # 0) явный путь из окружения
        env = os.environ.get("RECORDER_BIN", "").strip()
        if env:
            p = Path(env)
            if p.exists(): return p.resolve()

        # база: где лежит бинарь GUI
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent  # exe папка / .app/Contents/MacOS
        else:
            base = Path(__file__).resolve().parent

        # кандидаты: рядом с GUI
        cands: List[Path] = [
            base / "datagrabber_69.exe",
            base / "datagrabber_69",
        ]

        # macOS .app — мы и так в Contents/MacOS, но на всякий
        if platform.system() == "Darwin":
            cands.insert(0, Path(sys.executable).resolve().parent / "datagrabber_69")

        for c in cands:
            if c.exists():
                return c.resolve()

        # fallback: исходник рядом (режим разработки)
        py = base / "datagrabber_69.py"
        if not getattr(sys, "frozen", False) and py.exists():
            return py.resolve()

        return None

    # ---- утилиты ----
    def _tick_timer(self):
        if self.rec_start_time is None: return
        dt = int(time.time() - self.rec_start_time); mm, ss = divmod(dt, 60)
        self.timer_var.set(f"{mm:02d}:{ss:02d}")
        self._timer_job = self.after(1000, self._tick_timer)

    def _detect_operator(self) -> str:
        op = os.environ.get("OPERATOR_NAME","").strip()
        if op: return op
        try:
            import getpass; return getpass.getuser()
        except Exception: return ""

    def _on_close(self):
        try:
            if self.rec_proc and self.rec_proc.poll() is None:
                if hasattr(self,"_current_rec"):
                    try: (Path(self._current_rec["rec_dir"]) / ".stop").touch()
                    except Exception: pass
                try:
                    import os; pgid = os.getpgid(self.rec_proc.pid); os.killpg(pgid, signal.SIGINT)
                except Exception:
                    self.rec_proc.send_signal(signal.SIGINT)
        except Exception: pass
        # cancel timers
        if self._timer_job is not None:
            try: self.after_cancel(self._timer_job)
            except Exception: pass
            self._timer_job = None
        if self._size_job is not None:
            try: self.after_cancel(self._size_job)
            except Exception: pass
            self._size_job = None
        self.destroy()

def make_provider_from_env() -> TaskProvider:
    tasks_json = os.environ.get("TASKS_JSON","").strip()
    if tasks_json and Path(tasks_json).exists():
        tasks = json.loads(Path(tasks_json).read_text(encoding="utf-8"))
        if isinstance(tasks, list): return LocalListTaskProvider([str(t) for t in tasks])
    return LocalListTaskProvider([
        "Откройте браузер и найдите погоду в Амстердаме",
        "Создайте документ и сохраните его на рабочий стол",
        "Откройте почту и подготовьте черновик письма другу",
    ])

if __name__ == "__main__":
    app = App(make_provider_from_env()); app.mainloop()

# ToDo add LLM support
