#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Dict, Optional, Tuple

from mss import mss
from PIL import Image
from pynput import keyboard, mouse


@dataclass
class Event:
    ts: float
    etype: str
    payload: Dict


class SafeDeque:
    def __init__(self):
        self._dq: Deque[Event] = deque()
        self._lock = threading.Lock()

    def append(self, item: Event):
        with self._lock:
            self._dq.append(item)

    def pop_all_upto(self, ts_cutoff: float) -> list[Event]:
        out = []
        with self._lock:
            while self._dq and self._dq[0].ts <= ts_cutoff:
                out.append(self._dq.popleft())
        return out

    def drain_all(self) -> list[Event]:
        with self._lock:
            out = list(self._dq)
            self._dq.clear()
        return out


class Recorder:
    def __init__(
        self,
        dataset_root: str = "./dataset",
        rec_id: Optional[str] = None,
        task_text: str = "",
        fps: int = 20,
        monitor_index: int = 1,
        stop_key: str = "F10",
        dev: bool = False,
        max_duration: Optional[float] = None,
        operator: str = "",
    ):
        self.dataset_root = dataset_root
        self.rec_id = rec_id or f"rec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.task_text = task_text
        self.fps = max(1, int(fps))
        self.dt = 1.0 / self.fps
        self.monitor_index = monitor_index
        self.stop_key_name = (stop_key or "F10").upper()
        self.dev = dev
        self.max_duration = max_duration
        self.operator = operator.strip()

        self.rec_dir = os.path.join(self.dataset_root, self.rec_id)
        self.frames_dir = os.path.join(self.rec_dir, "frames")
        self.csv_path = os.path.join(self.rec_dir, "events.csv")
        self.meta_path = os.path.join(self.rec_dir, "meta.json")
        self.task_path = os.path.join(self.rec_dir, "task.txt")
        self.stop_flag_path = os.path.join(self.rec_dir, ".stop")  # ← ФЛАГ ОСТАНОВКИ (файлом)
        os.makedirs(self.frames_dir, exist_ok=True)

        self.events_q = SafeDeque()
        self._start_perf: Optional[float] = None
        self._stop_flag = threading.Event()
        self._dev_last_report = 0.0
        self._frames_captured = 0
        self._events_written = 0
        self._last_recorded_mouse_pos: Optional[Tuple[int, int]] = None
        self._pressed_mods = set()

        self._stop_key_obj = self._parse_stop_key(self.stop_key_name)

    # ---------- utils ----------
    def _now_rel(self) -> float:
        return time.perf_counter() - (self._start_perf or 0.0)

    def _parse_stop_key(self, name: str):
        name = (name or "").strip().upper()
        base = {
            "ESC": keyboard.Key.esc,
            "ENTER": keyboard.Key.enter,
            "RETURN": keyboard.Key.enter,
            "SPACE": keyboard.Key.space,
        }
        if name in base:
            return base[name]
        if name.startswith("F") and name[1:].isdigit():
            try:
                n = int(name[1:])
                if 1 <= n <= 24:
                    attr = f"f{n}"
                    if hasattr(keyboard.Key, attr):
                        return getattr(keyboard.Key, attr)
            except Exception:
                pass
        if len(name) == 1:
            try:
                return keyboard.KeyCode.from_char(name.lower())
            except Exception:
                pass
        return keyboard.Key.f10

    def _normalize_key(self, key) -> Tuple[str, Optional[int]]:
        try:
            if isinstance(key, keyboard.KeyCode):
                return key.char if key.char is not None else str(key), key.vk
            return key.name, None
        except Exception:
            return str(key), None

    def _collect_mods(self) -> str:
        if not self._pressed_mods:
            return ""
        order = ["cmd", "ctrl", "alt", "shift"]
        return "+".join([m for m in order if m in self._pressed_mods])

    # ---------- listeners ----------
    def _kb_on_press(self, key):
        name, vk = self._normalize_key(key)
        if name in {"shift", "shift_l", "shift_r"}:
            self._pressed_mods.add("shift")
        elif name in {"ctrl", "ctrl_l", "ctrl_r"}:
            self._pressed_mods.add("ctrl")
        elif name in {"alt", "alt_l", "alt_r", "option"}:
            self._pressed_mods.add("alt")
        elif name in {"cmd", "cmd_l", "cmd_r", "super"}:
            self._pressed_mods.add("cmd")

        self.events_q.append(Event(self._now_rel(), "key_down", {"key": name, "key_code": vk, "modifiers": self._collect_mods()}))
        if key == self._stop_key_obj:
            self._stop_flag.set()

    def _kb_on_release(self, key):
        name, vk = self._normalize_key(key)
        if name in {"shift", "shift_l", "shift_r"}:
            self._pressed_mods.discard("shift")
        elif name in {"ctrl", "ctrl_l", "ctrl_r"}:
            self._pressed_mods.discard("ctrl")
        elif name in {"alt", "alt_l", "alt_r", "option"}:
            self._pressed_mods.discard("alt")
        elif name in {"cmd", "cmd_l", "cmd_r", "super"}:
            self._pressed_mods.discard("cmd")
        self.events_q.append(Event(self._now_rel(), "key_up", {"key": name, "key_code": vk, "modifiers": self._collect_mods()}))

    def _ms_on_move(self, x, y):
        self.events_q.append(Event(self._now_rel(), "mouse_move", {"x": int(x), "y": int(y)}))

    def _ms_on_click(self, x, y, button, pressed):
        self.events_q.append(Event(self._now_rel(), "mouse_click", {
            "x": int(x), "y": int(y),
            "button": getattr(button, "name", str(button)),
            "action": "down" if pressed else "up",
        }))

    def _ms_on_scroll(self, x, y, dx, dy):
        self.events_q.append(Event(self._now_rel(), "mouse_scroll", {"x": int(x), "y": int(y), "scroll_dx": int(dx), "scroll_dy": int(dy)}))

    # ---------- io ----------
    def _write_meta(self, monitor: Dict):
        meta = {
            "rec_id": self.rec_id,
            "created_at": datetime.now().isoformat(),
            "platform": platform.platform(),
            "fps_target": self.fps,
            "monitor": monitor,
            "notes": "Все события между кадрами i и i+1 приписаны к кадру i.",
            "stop_key": self.stop_key_name,
            "operator": self.operator,
        }
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def _write_task(self):
        with open(self.task_path, "w", encoding="utf-8") as f:
            f.write((self.task_text or "(задача не указана)").strip() + "\n")

    def _open_csv(self):
        fieldnames = [
            "row_type", "frame_id", "time_s", "event_type", "frame_path",
            "x", "y", "dx", "dy", "key", "key_code", "mouse_button", "action", "scroll_dx", "scroll_dy", "modifiers",
        ]
        f = open(self.csv_path, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        return f, writer

    def _dev_report(self, now_rel: float):
        if not self.dev:
            return
        if now_rel - self._dev_last_report >= 1.0:
            fps = self._frames_captured / max(1e-6, now_rel)
            print(f"[DEV] t={now_rel:6.2f}s | frames={self._frames_captured} (~{fps:5.1f} fps) | events={self._events_written}")
            self._dev_last_report = now_rel

    # ---------- record ----------
    def record(self):
        # удалить старый .stop, если остался
        try:
            if os.path.exists(self.stop_flag_path):
                os.remove(self.stop_flag_path)
        except Exception:
            pass

        kb_listener = keyboard.Listener(on_press=self._kb_on_press, on_release=self._kb_on_release)
        ms_listener = mouse.Listener(on_move=self._ms_on_move, on_click=self._ms_on_click, on_scroll=self._ms_on_scroll)
        kb_listener.start()
        ms_listener.start()

        with mss() as sct:
            try:
                monitor = sct.monitors[self.monitor_index]
            except Exception:
                monitor = sct.monitors[1]

            self._write_meta(monitor)
            self._write_task()
            csv_file, writer = self._open_csv()
            try:
                self._start_perf = time.perf_counter()
                next_capture = self._start_perf
                frame_id = 0
                self._last_recorded_mouse_pos = None

                # Первый кадр
                img = sct.grab(monitor)
                im = Image.frombytes("RGB", img.size, img.rgb)
                frame_id = 1
                frame_path = os.path.join(self.frames_dir, f"{frame_id:06d}.png")
                im.save(frame_path, format="PNG", optimize=False, compress_level=1)
                t_rel = self._now_rel()
                writer.writerow({
                    "row_type": "frame", "frame_id": frame_id, "time_s": round(t_rel, 6),
                    "event_type": "frame", "frame_path": os.path.relpath(frame_path, self.rec_dir),
                })
                csv_file.flush()
                self._frames_captured += 1
                next_capture = self._start_perf + self.dt

                # Цикл
                while not self._stop_flag.is_set():
                    # аварийный стоп-файл
                    if os.path.exists(self.stop_flag_path):
                        self._stop_flag.set()
                        break

                    while True:
                        now_abs = time.perf_counter()
                        remaining = next_capture - now_abs
                        if remaining <= 0:
                            break
                        time.sleep(min(remaining, 0.005))

                    img = sct.grab(monitor)
                    t_boundary_rel = self._now_rel()
                    im = Image.frombytes("RGB", img.size, img.rgb)
                    next_frame_id = frame_id + 1

                    for ev in self.events_q.pop_all_upto(t_boundary_rel):
                        row = {
                            "row_type": "event", "frame_id": frame_id,
                            "time_s": round(ev.ts, 6), "event_type": ev.etype,
                        }
                        if ev.etype.startswith("mouse"):
                            x = int(ev.payload.get("x", -1))
                            y = int(ev.payload.get("y", -1))
                            if self._last_recorded_mouse_pos is None:
                                dx = dy = 0
                            else:
                                dx = x - self._last_recorded_mouse_pos[0]
                                dy = y - self._last_recorded_mouse_pos[1]
                            self._last_recorded_mouse_pos = (x, y)
                            row.update({"x": x, "y": y, "dx": dx, "dy": dy,
                                        "mouse_button": ev.payload.get("button"),
                                        "action": ev.payload.get("action"),
                                        "scroll_dx": ev.payload.get("scroll_dx"),
                                        "scroll_dy": ev.payload.get("scroll_dy")})
                        elif ev.etype.startswith("key"):
                            row.update({"key": ev.payload.get("key"),
                                        "key_code": ev.payload.get("key_code"),
                                        "modifiers": ev.payload.get("modifiers")})
                        writer.writerow(row)
                        csv_file.flush()
                        self._events_written += 1

                    frame_path = os.path.join(self.frames_dir, f"{next_frame_id:06d}.png")
                    im.save(frame_path, format="PNG", optimize=False, compress_level=1)
                    writer.writerow({
                        "row_type": "frame", "frame_id": next_frame_id,
                        "time_s": round(t_boundary_rel, 6), "event_type": "frame",
                        "frame_path": os.path.relpath(frame_path, self.rec_dir),
                    })
                    csv_file.flush()
                    self._frames_captured += 1

                    frame_id = next_frame_id
                    next_capture += self.dt
                    self._dev_report(self._now_rel())

                    if self.max_duration is not None and self._now_rel() >= self.max_duration:
                        self._stop_flag.set()

                # добираем «хвост» событий
                for ev in self.events_q.drain_all():
                    row = {
                        "row_type": "event", "frame_id": frame_id,
                        "time_s": round(ev.ts, 6), "event_type": ev.etype,
                    }
                    if ev.etype.startswith("mouse"):
                        x = int(ev.payload.get("x", -1))
                        y = int(ev.payload.get("y", -1))
                        if self._last_recorded_mouse_pos is None:
                            dx = dy = 0
                        else:
                            dx = x - self._last_recorded_mouse_pos[0]
                            dy = y - self._last_recorded_mouse_pos[1]
                        self._last_recorded_mouse_pos = (x, y)
                        row.update({"x": x, "y": y, "dx": dx, "dy": dy,
                                    "mouse_button": ev.payload.get("button"),
                                    "action": ev.payload.get("action"),
                                    "scroll_dx": ev.payload.get("scroll_dx"),
                                    "scroll_dy": ev.payload.get("scroll_dy")})
                    elif ev.etype.startswith("key"):
                        row.update({"key": ev.payload.get("key"),
                                    "key_code": ev.payload.get("key_code"),
                                    "modifiers": ev.payload.get("modifiers")})
                    writer.writerow(row)
                    csv_file.flush()
                    self._events_written += 1
            finally:
                csv_file.close()

        kb_listener.stop()
        ms_listener.stop()
        try:
            if os.path.exists(self.stop_flag_path):
                os.remove(self.stop_flag_path)
        except Exception:
            pass

        if self.dev:
            print(f"[DEV] Finished. frames={self._frames_captured}, events={self._events_written}")


def parse_args():
    p = argparse.ArgumentParser(description="PC Screen Dataset Recorder")
    p.add_argument("--dataset-root", type=str, default="./dataset")
    p.add_argument("--rec-id", type=str, default=None)
    p.add_argument("--task", type=str, default="")
    p.add_argument("--fps", type=int, default=20)
    p.add_argument("--monitor", type=int, default=1)
    p.add_argument("--stop-key", type=str, default="F10")
    p.add_argument("--dev", action="store_true")
    p.add_argument("--max-duration", type=float, default=None)
    p.add_argument("--operator", type=str, default="", help="Имя сборщика (запишется в meta.json)")
    return p.parse_args()


def main():
    args = parse_args()
    rec = Recorder(
        dataset_root=args.dataset_root,
        rec_id=args.rec_id,
        task_text=args.task,
        fps=args.fps,
        monitor_index=args.monitor,
        stop_key=args.stop_key,
        dev=args.dev,
        max_duration=args.max_duration,
        operator=args.operator,
    )

    def _graceful_stop(signum, frame):
        try:
            rec._stop_flag.set()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _graceful_stop)
    signal.signal(signal.SIGTERM, _graceful_stop)

    try:
        rec.record()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
