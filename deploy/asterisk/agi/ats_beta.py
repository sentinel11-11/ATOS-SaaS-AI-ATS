#!/usr/bin/python3
"""Local AGI: Russian TTS, DTMF decision tree, bounded result. No shell or API credentials."""

import base64
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

MEDIA = Path("/var/lib/ats-beta-media")
SOUNDS = Path("/var/lib/asterisk/sounds/ats-beta")


class AGI:
    def __init__(self, stdin=None, stdout=None):
        self.input = stdin or sys.stdin
        self.output = stdout or sys.stdout

    def start(self):
        for _ in range(100):
            line = self.input.readline(8193)
            if not line.strip():
                return
        raise ValueError("AGI environment limit")

    def command(self, text):
        if "\n" in text or "\r" in text:
            raise ValueError("AGI command injection")
        self.output.write(text + "\n")
        self.output.flush()
        line = self.input.readline(8193)
        match = re.fullmatch(r"200 result=(-?\d+)(?: \((.*)\))?\s*", line)
        if not match:
            raise RuntimeError("AGI command rejected")
        return int(match[1]), match[2] or ""

    def variable(self, name):
        return self.command("GET VARIABLE " + name)[1]

    def set(self, name, value):
        if not re.fullmatch(r"[A-Z_]+", name) or any(c in value for c in '\n\r"'):
            raise ValueError("Invalid AGI variable")
        self.command(f'SET VARIABLE {name} "{value}"')


def speech(text, directory, number):
    text = str(text).replace("\x00", "")[:2000]
    raw = directory / f"{number}-source.wav"
    target = directory / f"{number}.wav"
    subprocess.run(
        ["espeak-ng", "-v", "ru", "-s", "150", "-w", str(raw), "--stdin"],
        input=text.encode(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=20,
    )
    subprocess.run(
        ["sox", str(raw), "-r", "8000", "-c", "1", "-b", "16", str(target)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=10,
    )
    return str(target.with_suffix(""))


def execute(agi, payload, directory, renderer=speech):
    transcript = []
    counter = 0
    name = str(payload.get("name", ""))[:120]

    def say(text, question=False):
        nonlocal counter
        counter += 1
        text = str(text).replace("{name}", name)
        transcript.append({"speaker": "agent", "text": text[:120]})
        sound = renderer(text, directory, counter)
        if question:
            result, _ = agi.command(f'GET DATA "{sound}" 6000 1')
            if result < 0:
                raise RuntimeError("Channel ended")
            transcript.append({"speaker": "contact", "text": str(result)})
            return str(result)
        result, _ = agi.command(f'STREAM FILE "{sound}" ""')
        if result < 0:
            raise RuntimeError("Channel ended")
        return ""

    qualified = False
    if payload.get("mode") != "agent":
        say(payload.get("text") or "Здравствуйте! Это информационный звонок.")
    else:
        scenario = payload.get("scenario") or {}
        say(scenario.get("greeting") or "Здравствуйте!")
        if scenario.get("intro"):
            say(scenario["intro"])
        questions = scenario.get("questions") or []
        indices = {q["id"]: index for index, q in enumerate(questions)}
        qualified = bool(scenario.get("default_qualified"))
        position, steps = 0, 0
        while position < len(questions):
            steps += 1
            if steps > 60:
                raise ValueError("Scenario cycle limit")
            q = questions[position]
            answer = say(q["text"], question=True)
            choice = (q.get("choices") or {}).get(answer)
            if choice is None:
                qualified = False
                break
            if choice.get("qualified") is not None:
                qualified = bool(choice["qualified"])
            target = choice.get("next", "continue")
            if target == "end":
                break
            position = indices[target] if target != "continue" else position + 1
        say(
            scenario.get("qualified_text")
            if qualified
            else scenario.get("not_qualified_text") or "Спасибо, до свидания."
        )
        if scenario.get("goodbye"):
            say(scenario["goodbye"])
    return {"qualified": qualified, "transcript": transcript[:10]}


def main():
    agi = AGI()
    try:
        agi.start()
        ident = str(UUID(agi.variable("ATS_ATTEMPT_ID")))
        path = MEDIA / (ident + ".json")
        if path.is_symlink() or path.stat().st_size > 128 * 1024:
            raise ValueError("Invalid media payload")
        payload = json.loads(path.read_text())
        with tempfile.TemporaryDirectory(prefix="call-", dir=SOUNDS) as directory:
            result = execute(agi, payload, Path(directory))
        encoded = base64.b64encode(json.dumps(result, ensure_ascii=False).encode()).decode()
        if len(encoded) > 6000:
            raise ValueError("Media result limit")
        agi.set(
            "ATS_TRANSFER", "1" if payload.get("mode") == "operator" or result["qualified"] else "0"
        )
        agi.set("ATS_RESULT_B64", encoded)
        agi.set("ATS_RESULT", "completed")
    except Exception:
        # No text, phone numbers, credentials or traceback enter Asterisk logs.
        agi.set("ATS_RESULT", "failed")


if __name__ == "__main__":
    main()
