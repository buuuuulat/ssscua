import os
from openai import OpenAI


class Brain:
    def __init__(self):
        self.client = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com")
        self.initial_prompt = None
