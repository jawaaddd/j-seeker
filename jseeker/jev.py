"""Client for TypeSafe's Jev decision model."""

import time

import requests

URL = "https://api.typesafe.ai/v1/systemone"


class Jev:
    def __init__(self, api_key: str, model: str):
        self.model = model
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"
        # The docs disagree on whether choice options go in "criteria" (option -> description)
        # or "options" (a list); try the former and fall back to the latter once.
        self.options_as_list = False

    def ask(self, state, questions: dict) -> dict:
        """Ask the given questions about state and return the answers, keyed like questions."""
        payload = {"model": self.model, "state": state, "questions": self._encode(questions)}
        for attempt in range(4):
            response = self.session.post(URL, json=payload, timeout=60)
            if response.status_code in (400, 422) and not self.options_as_list and _has_choice(questions):
                self.options_as_list = True
                payload["questions"] = self._encode(questions)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()["answers"]
        response.raise_for_status()
        return {}

    def _encode(self, questions: dict) -> dict:
        if not self.options_as_list:
            return questions
        return {
            name: {**{k: v for k, v in q.items() if k != "criteria"}, "options": list(q["criteria"])}
            if q["type"] == "choice" else q
            for name, q in questions.items()
        }


def choice(instructions: str, options: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def noul(instructions: str) -> dict:
    return {"type": "noul", "instructions": instructions}


def _has_choice(questions: dict) -> bool:
    return any(q["type"] == "choice" for q in questions.values())
