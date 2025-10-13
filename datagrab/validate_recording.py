#!/usr/bin/env python3
"""
Validate Recording
------------------
Проверяет согласованность записи датасета:
- кадры (frame) монотонны по времени и идут подряд с id = 1..N
- события (event) привязаны к ПРЕДЫДУЩЕМУ кадру: frame_id=k ⇒ time_s(event) ≤ time_s(frame k+1)
- для k>1 ожидаем time_s(event) ≥ time_s(frame k) (исключение: ранние события ДО первого кадра допускаются и будут помечены как предупреждение)
- все пути к кадрам из CSV существуют; размеры всех кадров одинаковые
- валидируем целостность dx,dy для мыши: dx,dy == разница с предыдущей зафиксированной позицией (по CSV)
- считаем статистику: события/кадр, частоты типов событий, средний интервал между кадрами vs meta.fps

Пример запуска:
    python validate_recording.py --rec-dir ./dataset/rec_1 --check-images --sample-frames 0

Зависимости:
    pip install pillow
(если не хотите проверять изображения — можно запускать без Pillow и без флага --check-images)
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from collections import defaultdict, Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from PIL import Image  # type: ignore
except Exception:  # Pillow optional
    Image = None  # type: ignore


@dataclass
class FrameRow:
    fid: int
    t: float
    path: str


@dataclass
class EventRow:
    fid: int
    t: float
    etype: str
    payload: dict


def load_meta(meta_path: Path) -> dict:
    if not meta_path.exists():
        return {}
    with meta_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_csv(csv_path: Path) -> Tuple[List[FrameRow], List[EventRow]]:
    frames: List[FrameRow] = []
    events: List[EventRow] = []
    with csv_path.open("r", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            rtype = row.get("row_type", "").strip()
            fid = int(row.get("frame_id", 0) or 0)
            t = float(row.get("time_s", 0.0) or 0.0)
            if rtype == "frame":
                frames.append(FrameRow(fid=fid, t=t, path=row.get("frame_path", "")))
            elif rtype == "event":
                etype = row.get("event_type", "").strip()
                payload = {
                    "x": row.get("x"),
                    "y": row.get("y"),
                    "dx": row.get("dx"),
                    "dy": row.get("dy"),
                    "key": row.get("key"),
                    "key_code": row.get("key_code"),
                    "mouse_button": row.get("mouse_button"),
                    "action": row.get("action"),
                    "scroll_dx": row.get("scroll_dx"),
                    "scroll_dy": row.get("scroll_dy"),
                    "modifiers": row.get("modifiers"),
                }
                events.append(EventRow(fid=fid, t=t, etype=etype, payload=payload))
    frames.sort(key=lambda r: r.fid)
    events.sort(key=lambda r: (r.fid, r.t))
    return frames, events


def check_frames(frames: List[FrameRow]) -> List[str]:
    errs: List[str] = []
    if not frames:
        errs.append("[F01] В CSV нет ни одного кадра (row_type=frame)")
        return errs

    # id подряд 1..N
    for i, fr in enumerate(frames, start=1):
        if fr.fid != i:
            errs.append(f"[F02] Непрерывность frame_id нарушена: ожидали {i}, увидели {fr.fid}")
            break

    # время монотонно и возрастает
    for prev, cur in zip(frames, frames[1:]):
        if cur.t < prev.t - 1e-6:
            errs.append(f"[F03] Невозрастающее время кадров: frame {prev.fid} @ {prev.t:.6f} > frame {cur.fid} @ {cur.t:.6f}")
            break

    return errs


def check_events_vs_frames(frames: List[FrameRow], events: List[EventRow]) -> Tuple[List[str], List[str]]:
    errs: List[str] = []
    warns: List[str] = []

    if not frames:
        errs.append("[E00] Нельзя проверить события без кадров")
        return errs, warns

    frame_times = {fr.fid: fr.t for fr in frames}
    last_fid = frames[-1].fid

    # верхняя граница для событий: event(fid=k).t ≤ t(frame k+1)
    for ev in events:
        if ev.fid < 1 or ev.fid > last_fid:
            errs.append(f"[E01] event с недопустимым frame_id={ev.fid} (последний кадр {last_fid})")
            continue
        # верхняя граница
        if ev.fid < last_fid:
            t_next = frame_times[ev.fid + 1]
            if ev.t > t_next + 1e-6:
                errs.append(
                    f"[E02] event @ {ev.t:.6f} с frame_id={ev.fid} ПОСЛЕ времени следующего кадра (t_next={t_next:.6f})"
                )
        # нижняя граница (кроме frame 1 допускаем предупреждение)
        t_cur = frame_times[ev.fid]
        if ev.fid > 1 and ev.t < t_cur - 1e-6:
            errs.append(
                f"[E03] event @ {ev.t:.6f} с frame_id={ev.fid} РАНЬШЕ времени своего кадра (t_frame={t_cur:.6f})"
            )
        if ev.fid == 1 and ev.t < t_cur - 1e-6:
            warns.append(
                f"[W10] Ранний event до первого кадра: t_event={ev.t:.6f} < t_frame1={t_cur:.6f} (это допустимо, если действия были до первого снимка)"
            )

    return errs, warns


def check_mouse_deltas(events: List[EventRow]) -> Tuple[List[str], int]:
    errs: List[str] = []
    n_checked = 0
    last_pos: Optional[Tuple[int, int]] = None
    for ev in events:
        if not ev.etype.startswith("mouse"):
            continue
        try:
            x = int(ev.payload.get("x")) if ev.payload.get("x") not in (None, "") else None
            y = int(ev.payload.get("y")) if ev.payload.get("y") not in (None, "") else None
            dx = int(ev.payload.get("dx")) if ev.payload.get("dx") not in (None, "") else None
            dy = int(ev.payload.get("dy")) if ev.payload.get("dy") not in (None, "") else None
        except Exception:
            continue
        if x is None or y is None or dx is None or dy is None:
            continue
        expected_dx = 0 if last_pos is None else x - last_pos[0]
        expected_dy = 0 if last_pos is None else y - last_pos[1]
        if dx != expected_dx or dy != expected_dy:
            errs.append(
                f"[M01] Несовпадение dx,dy на t={ev.t:.6f}: было ({dx},{dy}), ожидали ({expected_dx},{expected_dy}) при переходе {last_pos}→({x},{y})"
            )
        last_pos = (x, y)
        n_checked += 1
    return errs, n_checked


def check_images_exist_and_shape(rec_dir: Path, frames: List[FrameRow], sample: int = 0) -> Tuple[List[str], Optional[Tuple[int,int]]]:
    errs: List[str] = []
    # проверка существования
    for fr in frames:
        fpath = rec_dir / fr.path
        if not fpath.exists():
            errs.append(f"[I01] Файл кадра не найден: {fr.path}")
    if Image is None or sample == 0:
        return errs, None

    # проверим форму на первых `sample` и последних `sample` кадрах (или на всех, если sample<0)
    idxs = list(range(len(frames)))
    if sample > 0 and len(frames) > 2*sample:
        idxs = list(range(sample)) + list(range(len(frames)-sample, len(frames)))

    ref_size: Optional[Tuple[int,int]] = None
    for i in idxs:
        fr = frames[i]
        fpath = rec_dir / fr.path
        try:
            with Image.open(fpath) as im:
                size = im.size  # (w,h)
        except Exception as e:
            errs.append(f"[I02] Ошибка чтения изображения {fr.path}: {e}")
            continue
        if ref_size is None:
            ref_size = size
        elif size != ref_size:
            errs.append(f"[I03] Непостоянный размер кадров: {fr.path} имеет {size}, ожидали {ref_size}")
    return errs, ref_size


def summarize(frames: List[FrameRow], events: List[EventRow], meta: dict) -> str:
    N = len(frames)
    M = len(events)
    if N == 0:
        return "Нет кадров — сводка недоступна"

    # интервал между кадрами
    gaps = [b.t - a.t for a,b in zip(frames, frames[1:])]
    avg_gap = sum(gaps)/len(gaps) if gaps else float('nan')
    fps_est = (1.0/avg_gap) if gaps and avg_gap>0 else float('nan')

    # события по кадрам
    per_frame = Counter([ev.fid for ev in events])
    min_e = min(per_frame.values()) if per_frame else 0
    max_e = max(per_frame.values()) if per_frame else 0
    avg_e = (sum(per_frame.values())/N) if per_frame else 0.0

    # события по типам
    types = Counter([ev.etype for ev in events])
    types_str = ", ".join([f"{k}:{v}" for k,v in types.most_common()]) if types else "—"

    fps_target = meta.get("fps_target")

    lines = [
        f"Кадров: {N}",
        f"Событий: {M}",
        f"Средний интервал между кадрами: {avg_gap:.4f} c (оценка FPS ≈ {fps_est:.2f}{' / целевое ' + str(fps_target) if fps_target else ''})",
        f"Событий на кадр: min={min_e}, max={max_e}, avg={avg_e:.2f}",
        f"Распределение типов событий: {types_str}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Validate a PC screen recording dataset")
    ap.add_argument("--rec-dir", required=True, help="Путь к папке записи (например, ./dataset/rec_1)")
    ap.add_argument("--check-images", action="store_true", help="Проверять существование и размеры изображений (нужна Pillow)")
    ap.add_argument("--sample-frames", type=int, default=8, help="Сколько кадров проверять по краям (0 — не проверять; <0 — все)")
    args = ap.parse_args()

    rec_dir = Path(args.rec_dir)
    csv_path = rec_dir / "events.csv"
    meta_path = rec_dir / "meta.json"

    if not csv_path.exists():
        print(f"Не найден CSV: {csv_path}")
        return

    meta = load_meta(meta_path)
    frames, events = load_csv(csv_path)

    all_errs: List[str] = []
    all_warns: List[str] = []

    # 1) кадры
    all_errs += check_frames(frames)

    # 2) события vs кадры
    errs, warns = check_events_vs_frames(frames, events)
    all_errs += errs
    all_warns += warns

    # 3) dx,dy согласованность
    m_errs, n_m = check_mouse_deltas(events)
    all_errs += m_errs

    # 4) изображения
    if args.check_images:
        img_errs, ref_size = check_images_exist_and_shape(rec_dir, frames, sample=args.sample_frames)
        all_errs += img_errs
        if ref_size:
            print(f"Базовый размер кадров: {ref_size[0]}x{ref_size[1]}")

    # Сводка
    print("==== СВОДКА ====")
    print(summarize(frames, events, meta))
    if n_m:
        print(f"Проверено мышиных событий (dx,dy): {n_m}")

    # Предупреждения и ошибки
    if all_warns:
        print("\n---- ПРЕДУПРЕЖДЕНИЯ ----")
        for w in all_warns[:50]:
            print(w)
        if len(all_warns) > 50:
            print(f"... и ещё {len(all_warns)-50} предупреждений")

    if all_errs:
        print("\n**** ОШИБКИ НАЙДЕНЫ ****")
        for e in all_errs[:200]:
            print(e)
        if len(all_errs) > 200:
            print(f"... и ещё {len(all_errs)-200} ошибок")
        exit(2)
    else:
        print("\nОК: нарушений не обнаружено")


if __name__ == "__main__":
    main()
