from dataclasses import dataclass
from typing import Dict, Tuple, Optional, Callable
import pyautogui


# pyautogui.FAILSAFE = True
# pyautogui.PAUSE = 0


@dataclass
class ActionSpec:
    mouse_delta: Tuple[int, int]
    key_id: int
    text: Optional[str] = None


class MacExecutor:
    def __init__(self):
        self.held_keys = set()

        self._actions: Dict[int, Callable[[Optional[str]], None]] = {
            # --- мышь ---
            0: lambda _: pyautogui.click(button='left'),
            1: lambda _: pyautogui.click(button='right'),
            2: lambda _: pyautogui.doubleClick(),

            # --- модификаторы с toggle ---
            3: lambda _: self.toggle_key('command'),
            4: lambda _: self.toggle_key('option'),
            5: lambda _: self.toggle_key('ctrl'),
            6: lambda _: self.toggle_key('shift'),

            # --- простые клавиши ---
            7: lambda _: pyautogui.press('space'),
            8: lambda _: pyautogui.press('enter'),
            9: lambda _: pyautogui.press('tab'),
            10: lambda _: pyautogui.press('esc'),

            # --- комбинации ---
            11: lambda _: pyautogui.hotkey('command', 'c'),  # copy
            12: lambda _: pyautogui.hotkey('command', 'v'),  # paste

            # --- печать текста ---
            13: lambda t: pyautogui.write(t or ""),          # write_text

            # --- стрелки ---
            14: lambda _: pyautogui.press('up'),
            15: lambda _: pyautogui.press('down'),
            16: lambda _: pyautogui.press('left'),
            17: lambda _: pyautogui.press('right'),

            # --- ничего не делать ---
            18: lambda _: None,
        }

    @property
    def n_discrete(self) -> int:
        return len(self._actions)

    # Переключатель удержания: первый вызов — keyDown, второй — keyUp
    def toggle_key(self, key: str):
        if key not in self.held_keys:
            pyautogui.keyDown(key, _pause=False)
            self.held_keys.add(key)
        else:
            pyautogui.keyUp(key, _pause=False)
            self.held_keys.remove(key)

    def apply(self, spec: ActionSpec):
        dx, dy = spec.mouse_delta
        if dx or dy:
            pyautogui.moveRel(dx, dy, _pause=False)

        action_fn = self._actions.get(spec.key_id)
        if action_fn is not None:
            action_fn(spec.text)

    def release_all(self):
        for key in list(self.held_keys):
            pyautogui.keyUp(key, _pause=False)
        self.held_keys.clear()

