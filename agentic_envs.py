import numpy as np
import pyautogui
import gymnasium as gym
from gymnasium.spaces import Dict, Box, Discrete
from mss import mss
from typing import Optional

from executor import MacExecutor



class ComputerUseEnv(gym.Env):
    def __init__(self, new_size=(1280, 720), verbose=False, monitor_index=1, fps=20, obs_prev_n_actions=10):
        super().__init__()
        self.executor = MacExecutor()

        self.verbose = verbose

        self.monitor_index = monitor_index
        self.sct = mss()
        try:
            self.monitor = self.sct.monitors[self.monitor_index]
        except Exception:
            self.monitor = self.sct.monitors[1]

        self.screen_size = pyautogui.size()
        self.screen_size_scalers = (self.screen_size.width / new_size[0], self.screen_size.height / new_size[1])
        self.observation_space = Box(low=0, high=255, shape=(3, new_size[0], new_size[1]), dtype=np.uint8)
        self.action_space = Dict({
            "move_mouse": Box(low=np.array([0, 0]), high=np.array(new_size)),
            "use_action": Discrete(self.executor.n_discrete)
        })

        self.reset()

    def _print_info(self, action):
        if self.verbose:
            print(f"Used action: {str(action)}")

    def _get_obs(self, prev_img=None, prompt="", prev_n_actions=[]):
        img = np.array(self.sct.grab(self.monitor))[:, :, :3]
        if prev_img is not None:
            batch = np.stack([prev_img, img], axis=0)  # (B(2), H, W, C(3))
        else:
            batch = np.expand_dims(img, axis=0)

        obs = {
            "frames": batch,
            "prompt": prompt,
            "prev_n_actions": prev_n_actions
        }
        return obs

    def reset(self, seed: Optional[int] = None, options: Optional[dict] = None):
        super().reset(seed=seed)
        self.executor.release_all()
        observation = self._get_obs()
        info = {}
        return observation, info

    def step(self, action):
        pass
