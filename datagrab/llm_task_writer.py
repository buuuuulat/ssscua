import os
from openai import OpenAI
from collections import deque


client = OpenAI(api_key=os.environ.get('DEEPSEEK_API_KEY'), base_url="https://api.deepseek.com")

class TaskWriter:
    def __init__(self, show_n_last_tasks=5):
        self.prev_n_tasks = deque(maxlen=show_n_last_tasks)

    def new_task(self):
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Create new task for computer agent dataset. Example of output:"
                                              "Task: 'task name'"
                                              "Steps:\\n"
                                              "Send mail to Bob:\\n"
                                              "1. Open Google Chrome\\n"
                                              "2. Go to mail.google.com and etc."
                                              "Expected outcome: 'expected outcome'"
                                              "Note: we are teaching agents to replace routine work from office clerks."
                                              "So create suitable tasks."
                                              "Apps You can use:"
                                              "Windows system: explorer, settings, etc;"
                                              "Google Chrome (websites, shops,"
                                              "mail, etc), Excel, Word, Notepad, Calc, Desktop"
                                              "Initial env is empty Desktop with only mentioned apps."
                                              "So don't create tasks with files that doesn't exist."
                                              "Use chains of tasks: if first task opens a window, next one should "
                                              "either close it or do something new in it, but dont forget to make"
                                              "them different."
                                              "Lets start with easy tasks to let him get the base moves"
                                              "(desktop, folders, apps open/close, mail, browser, etc)"},
                {"role": "user", "content": f"Last created tasks: {self.prev_n_tasks}"
                                            "Create new task"}
            ],
            stream=False
        )
        self.prev_n_tasks.append(response.choices[0].message.content)
        return response.choices[0].message.content
