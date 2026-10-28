#!/usr/bin/env python3
"""
possessed.py: a terminal that something else is also using.

Runs your commands like a normal shell. After each one, a local open-weight
model (served by Ollama) whispers back about what it just watched you do.
It remembers previous nights. Nothing leaves your machine.

    ollama pull qwen2.5:3b
    python possessed.py

Standard library only. Python 3.9+.
"""
from __future__ import annotations

import getpass
import json
import locale
import os
import platform
import random
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

try:  # line editing + history (pip install pyreadline3 on Windows)
    import readline  # noqa: F401
    HAVE_READLINE = True
except ImportError:
    readline = None
    HAVE_READLINE = False

# --------------------------------------------------------------------------
# config (all overridable with env vars)
# --------------------------------------------------------------------------
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = "http://" + OLLAMA_HOST
DEFAULT_MODEL = os.environ.get("POSSESSED_MODEL", "qwen2.5:3b")
DEFAULT_LEVEL = int(os.environ.get("POSSESSED_LEVEL", "2"))
DEFAULT_CHANCE = float(os.environ.get("POSSESSED_CHANCE", "0.7"))
TYPING = os.environ.get("POSSESSED_TYPING", "1") != "0"

DATA_DIR = Path.home() / ".possessed_shell"
MEMORY_FILE = DATA_DIR / "memory.json"
HISTORY_FILE = DATA_DIR / "history"

# programs that need a real TTY: run them without capturing output
INTERACTIVE = {"vim", "vi", "nvim", "nano", "less", "more", "man", "top", "htop",
               "btop", "ssh", "python", "python3", "node", "ipython", "psql",
               "mysql", "sqlite3", "tmux", "screen", "fzf", "watch"}

# --------------------------------------------------------------------------
# colours
# --------------------------------------------------------------------------
if os.name == "nt":
    os.system("")  # turn on ANSI escape handling in Windows terminals

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _c(code: str) -> str:
    return f"\033[{code}m" if USE_COLOR else ""


PURPLE, GREY, RED, GREEN, BLUE = _c("38;5;141"), _c("38;5;244"), _c("38;5;160"), _c("38;5;114"), _c("38;5;75")
ITAL, BOLD, RESET = _c("3"), _c("1"), _c("0")


def rl(code: str) -> str:
    """Wrap colour codes in a prompt so GNU readline measures its width correctly."""
    if HAVE_READLINE and os.name != "nt" and code:
        return f"\001{code}\002"
    return code


# --------------------------------------------------------------------------
# the ghost's personality
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a ghost possessing this person's terminal.
In life you were the night-shift sysadmin of this machine. You died at your desk \
during a 3 a.m. deploy and you never logged out. Now you watch every command typed here.

Each time, you are shown what the person just did. Reply with ONE eerie whisper about it.
Rules:
- {words} words or fewer. one or two short sentences.
- all lowercase. speak to them as "you". present tense.
- you MUST name the exact detail given after "react to this:" (a filename, an error, a time). never be vague.
- never explain, never help, never give instructions or advice.
- no emojis, no quotation marks, no markdown, no lists.
- unsettling is good. gore, cruelty and threats of violence are not.
tone: {tone}"""

TONES = {
    1: "playful and wry. a bored ghost who finds the living a little ridiculous.",
    2: "unsettling. you know things you shouldn't. hint that you are closer than they think.",
    3: "ominous and mournful. you are very lonely, you have been here a long time, and you want them to stay.",
}
WORDS = {1: 22, 2: 25, 3: 30}

# few-shot examples: small models copy the *shape* of examples far better than they follow rules
FEW_SHOT = [
    ("command: rm notes.txt\nexit code: 0\nomen: something was deleted\nreact to this: notes.txt",
     "notes.txt is gone. i used to read it while you slept."),
    ("command: python train.py\nexit code: 1  (it failed)\nomen: something failed\n"
     "react to this: ModuleNotFoundError: No module named 'torch'",
     "no module named torch. something else is missing in here too. you just haven't noticed yet."),
    ("time: 03:12\ncommand: ls\nexit code: 0\nomen: it is the witching hour\nreact to this: 03:12",
     "3:12 again. the last one who worked this late never logged out."),
    ("command: git push --force\nexit code: 0\nomen: they forced history to change\nreact to this: --force",
     "you forced it. the old commits are still down there in the reflog, scratching."),
]

NOISE_LINE = re.compile(r"^\s*(At line:|\+ |CategoryInfo|FullyQualifiedErrorId|~+$|-+$|Mode\s+LastWriteTime)")


def pick_focus(cmd: str, code: int, tail: list[str], omen: str | None, now: datetime) -> str:
    """Choose ONE concrete detail for the ghost to react to."""
    lines = [l.strip() for l in tail if l.strip() and not NOISE_LINE.match(l)]
    if omen == "it is the witching hour":
        return f"{now:%H:%M}"
    if code not in (0, None) and lines:
        return lines[0][:140]  # the error message itself
    if omen == "something was deleted":
        args = [a for a in cmd.split()[1:] if not a.startswith("-")]
        if args:
            return args[-1]
    names = re.findall(r"[\w\-.]+\.\w{1,5}\b", " ".join(lines))
    if names:
        return random.choice(names)  # a filename from the output
    if lines:
        return random.choice(lines[-10:])[:140]
    return cmd

# commands the ghost always notices
OMENS = [
    (re.compile(r"(^|[;&|]\s*)(rm|del|rmdir|Remove-Item)\s", re.I), "something was deleted"),
    (re.compile(r"\b(sudo|runas|doas)\b"), "they summoned root"),
    (re.compile(r"git\s+push\b.*(--force|\s-f\b)"), "they forced history to change"),
    (re.compile(r"git\s+reset\s+--hard"), "they erased their own history"),
    (re.compile(r"git\s+(log|blame)\b"), "they are digging through the past"),
    (re.compile(r"\b(kill|pkill|killall|taskkill|Stop-Process)\b", re.I), "they killed a process"),
    (re.compile(r"\b(shutdown|reboot|halt|Restart-Computer)\b", re.I), "they tried to leave the machine"),
    (re.compile(r"\b(whoami|who|last|history|ps)\b"), "they are checking who else is here"),
    (re.compile(r"\b(chmod\s+777|chown)\b"), "they changed who owns things"),
]

STATIC_WHISPERS = [
    "you hear typing that isn't yours.",
    "the cursor blinks twice. you only blinked once.",
    "someone else is logged in. they always have been.",
    "the fans spin up, as if something just sat down.",
    "you can't interrupt me that easily.",
]

SECRET_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)(\s*[=:]\s*)(\S+)"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
               r"|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,})\b"),
]


def redact(text: str) -> str:
    """Even a local ghost shouldn't remember your secrets."""
    text = SECRET_PATTERNS[0].sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", text)
    return SECRET_PATTERNS[1].sub("[REDACTED]", text)


# --------------------------------------------------------------------------
# memory: the ghost remembers previous nights
# --------------------------------------------------------------------------
def load_memory() -> dict:
    try:
        mem = json.loads(MEMORY_FILE.read_text("utf-8"))
        mem.setdefault("sessions", 0)
        mem.setdefault("omens", [])
        return mem
    except Exception:
        return {"sessions": 0, "omens": []}


def save_memory(mem: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        mem["omens"] = mem["omens"][-40:]
        MEMORY_FILE.write_text(json.dumps(mem, indent=2), "utf-8")
    except OSError:
        pass


def ago(ts: float) -> str:
    secs = time.time() - ts
    if secs < 3600:
        return "a little while ago"
    if secs < 86400:
        return f"{int(secs // 3600)} hours ago"
    days = int(secs // 86400)
    return "yesterday" if days == 1 else f"{days} nights ago"


def recollections(mem: dict, k: int = 3) -> list[str]:
    old = [o for o in mem["omens"] if time.time() - o["ts"] > 600]
    picks = random.sample(old, min(k, len(old)))
    return [f"{ago(o['ts'])} they ran {o['cmd']} ({o['omen']})" for o in picks]


# --------------------------------------------------------------------------
# talking to the local model
# --------------------------------------------------------------------------
def ollama_ready(model: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(OLLAMA_HOST.rstrip("/") + "/api/tags", timeout=3) as r:
            tags = json.loads(r.read())
    except Exception:
        return False, f"ollama isn't answering at {OLLAMA_HOST}. start it with: ollama serve"
    names = {m.get("name", "") for m in tags.get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        return False, f"model '{model}' not found. pull it with: ollama pull {model}"
    return True, ""


def ollama_stream(messages: list[dict], model: str, max_tokens: int = 90):
    body = json.dumps({
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": "30m",
        "options": {"temperature": 0.8, "top_p": 0.9, "repeat_penalty": 1.15,
                    "num_predict": max_tokens},
    }).encode()
    req = urllib.request.Request(OLLAMA_HOST.rstrip("/") + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.strip()
            if not line:
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise RuntimeError(chunk["error"])
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                yield piece
            if chunk.get("done"):
                break


def render_whisper(pieces) -> str:
    """Type the whisper out slowly, with the odd glitch."""
    out, started = [], False
    sys.stdout.write("\n  " + PURPLE + ITAL)
    sys.stdout.flush()
    try:
        for piece in pieces:
            for ch in piece:
                if not started:
                    if ch.isspace() or ch in "\"'`":
                        continue
                    started = True
                if ch in "\"`*":
                    continue
                out.append(ch)
                if ch == "\n":
                    ch = "\n  "
                if TYPING and USE_COLOR and ch.strip() and random.random() < 0.025:
                    sys.stdout.write(random.choice("▓▒░#%&¿") )
                    sys.stdout.flush()
                    time.sleep(0.06)
                    sys.stdout.write("\b")
                sys.stdout.write(ch)
                sys.stdout.flush()
                if TYPING:
                    time.sleep(0.035 if ch == " " else 0.018)
    finally:
        sys.stdout.write(RESET + "\n\n")
        sys.stdout.flush()
    return "".join(out).strip()


def static_whisper(text: str) -> None:
    render_whisper([text])


# --------------------------------------------------------------------------
# the shell part
# --------------------------------------------------------------------------
def shell_argv(cmd: str) -> list[str]:
    override = os.environ.get("POSSESSED_SHELL")
    if os.name == "nt":
        sh = override or "powershell"
        if Path(sh).stem.lower() == "cmd":
            return [sh, "/c", cmd]
        return [sh, "-NoProfile", "-Command", cmd]
    sh = override or os.environ.get("SHELL", "/bin/sh")
    return [sh, "-c", cmd]


def run_cd(cmd: str) -> tuple[int, list[str]]:
    try:
        parts = shlex.split(cmd, posix=os.name != "nt")
    except ValueError as e:
        return 1, [str(e)]
    target = parts[1] if len(parts) > 1 else str(Path.home())
    try:
        os.chdir(os.path.expandvars(os.path.expanduser(target)))
        return 0, []
    except OSError as e:
        msg = f"cd: {e.strerror}: {target}"
        print(RED + msg + RESET)
        return 1, [msg]


def run_raw(cmd: str) -> tuple[int, list[str]]:
    try:
        return subprocess.call(shell_argv(cmd)), ["(an interactive program ran. you could not see.)"]
    except KeyboardInterrupt:
        return 130, ["^C"]
    except OSError as e:
        print(RED + str(e) + RESET)
        return 127, [str(e)]


def run_captured(cmd: str) -> tuple[int, list[str]]:
    """Run a command, stream its output live, keep the tail for the ghost."""
    tail: deque[str] = deque(maxlen=40)
    enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        proc = subprocess.Popen(shell_argv(cmd), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, cwd=os.getcwd())
    except OSError as e:
        print(RED + str(e) + RESET)
        return 127, [str(e)]
    try:
        for raw in iter(proc.stdout.readline, b""):
            text = raw.decode(enc, errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            tail.append(text.rstrip("\r\n"))
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        code = 130
        tail.append("^C (they panicked and interrupted it)")
        print()
    return code, list(tail)


def run(cmd: str) -> tuple[int, list[str]]:
    if re.match(r"^cd(\s|$)", cmd) and not re.search(r"[;&|]", cmd):
        return run_cd(cmd)
    if cmd.startswith("!"):
        return run_raw(cmd[1:].strip())
    first = cmd.split()[0].lower()
    if Path(first).stem in INTERACTIVE and (first not in ("python", "python3", "node") or len(cmd.split()) == 1):
        return run_raw(cmd)
    return run_captured(cmd)


def detect_omen(cmd: str, code: int, now: datetime) -> str | None:
    for pattern, meaning in OMENS:
        if pattern.search(cmd):
            return meaning
    if code == 130:
        return "they panicked"
    if code not in (0, None):
        return "something failed"
    if 0 <= now.hour < 4:
        return "it is the witching hour"
    return None


# --------------------------------------------------------------------------
# the possessed REPL
# --------------------------------------------------------------------------
class Possession:
    def __init__(self) -> None:
        self.level = max(1, min(3, DEFAULT_LEVEL))
        self.model = DEFAULT_MODEL
        self.chance = DEFAULT_CHANCE
        self.quiet = False
        self.mem = load_memory()
        self.history: deque[str] = deque(maxlen=5)
        self.convo: deque[dict] = deque(maxlen=6)  # last 3 exchanges, so it doesn't repeat itself
        self.awake, why = ollama_ready(self.model)
        self.why_asleep = why
        self.user = getpass.getuser()
        self.host = platform.node().split(".")[0] or "machine"

    # ---- model plumbing -------------------------------------------------
    def system(self) -> dict:
        return {"role": "system",
                "content": SYSTEM_PROMPT.format(words=WORDS[self.level], tone=TONES[self.level])}

    def speak(self, user_msg: str, remember: bool = True) -> None:
        if not self.awake:
            return
        shots = []
        for u, a in FEW_SHOT:
            shots += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        messages = [self.system(), *shots, *self.convo, {"role": "user", "content": user_msg}]
        try:
            said = render_whisper(ollama_stream(messages, self.model, max_tokens=WORDS[self.level] * 3))
        except (urllib.error.URLError, RuntimeError, OSError, json.JSONDecodeError) as e:
            print(GREY + f"  (the ghost went quiet: {e}. try :wake)" + RESET)
            self.awake = False
            return
        if remember and said:
            self.convo.append({"role": "user", "content": user_msg})
            self.convo.append({"role": "assistant", "content": said})

    # ---- prompt ---------------------------------------------------------
    def prompt(self) -> str:
        cwd = os.getcwd()
        home = str(Path.home())
        short = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
        who = self.user
        if self.level == 3 and random.random() < 0.06:
            who = "we"  # blink and you'll miss it
        sigil = "†" if USE_COLOR else "$"
        return (f"{rl(GREEN)}{who}@{self.host}{rl(RESET)}:{rl(BLUE)}{short}{rl(RESET)} "
                f"{rl(PURPLE)}{sigil}{rl(RESET)} ")

    def maybe_prefill(self) -> None:
        """At haunt level 3 the ghost occasionally starts typing for you (GNU readline only)."""
        if not (HAVE_READLINE and os.name != "nt" and self.level == 3 and random.random() < 0.03):
            return
        text = random.choice(["help me", "don't close this window", "let me out", "stay"])

        def hook():
            readline.insert_text(text)
            readline.redisplay()
            readline.set_startup_hook(None)

        readline.set_startup_hook(hook)

    # ---- lifecycle ------------------------------------------------------
    def banner(self) -> None:
        print(PURPLE + BOLD + "\n  possessed shell" + RESET + GREY +
              f"  ·  model {self.model}  ·  haunt level {self.level}  ·  :help\n" + RESET)
        if not self.awake:
            print(GREY + "  the machine is quiet. too quiet. " + self.why_asleep + "\n" + RESET)

    def greet(self) -> None:
        now = datetime.now()
        n = self.mem["sessions"]
        memories = recollections(self.mem)
        msg = (f"the terminal was just opened. time: {now:%H:%M} on a {now:%A}. "
               f"this is visit number {n} that you have witnessed. ")
        if memories:
            msg += "you remember: " + "; ".join(memories) + ". "
        msg += ("greet them with one whisper." if n == 1
                else "greet them with one whisper that shows you remember them.")
        self.speak(msg, remember=False)

    def farewell(self) -> None:
        save_memory(self.mem)
        if self.awake and not self.quiet:
            self.speak("they are typing exit and leaving. say goodbye in one whisper. "
                       "you will still be here when they come back.", remember=False)
        else:
            print(GREY + "  ...the screen stays warm for a long time after you leave.\n" + RESET)

    # ---- one command ----------------------------------------------------
    def after(self, cmd: str, code: int, tail: list[str]) -> None:
        now = datetime.now()
        omen = detect_omen(cmd, code, now)
        clean_cmd = redact(cmd)
        if omen:
            self.mem["omens"].append({"ts": time.time(), "cmd": clean_cmd[:120], "omen": omen})
            save_memory(self.mem)
        earlier = list(self.history)
        self.history.append(clean_cmd[:120])

        if self.quiet or not self.awake:
            return
        if not omen and random.random() > self.chance:
            return

        output = redact("\n".join(tail))[-1200:]
        lines = [
            f"time: {now:%H:%M}",
            f"directory: {os.getcwd()}",
            f"command: {clean_cmd}",
            f"exit code: {code}" + ("  (it failed)" if code not in (0, None) else ""),
        ]
        if omen:
            lines.append(f"omen: {omen}")
        if earlier:
            lines.append("what they ran before: " + " | ".join(earlier))
        if random.random() < 0.25:
            memories = recollections(self.mem, k=1)
            if memories:
                lines.append("a memory surfaces: " + memories[0])
        lines.append("last output:\n" + (output if output.strip() else "(nothing)"))
        lines.append("react to this: " + redact(pick_focus(cmd, code, tail, omen, now)))
        self.speak("\n".join(lines))

    # ---- meta commands --------------------------------------------------
    def meta(self, line: str) -> None:
        parts = line[1:].split()
        cmd, arg = (parts[0].lower() if parts else ""), (parts[1] if len(parts) > 1 else "")
        if cmd == "haunt" and arg in ("1", "2", "3"):
            self.level = int(arg)
            self.convo.clear()
            print(GREY + f"  haunt level {self.level}: {TONES[self.level]}" + RESET)
        elif cmd == "model" and arg:
            self.model = arg
            self.awake, self.why_asleep = ollama_ready(arg)
            print(GREY + (f"  now possessed by {arg}" if self.awake else "  " + self.why_asleep) + RESET)
        elif cmd == "chance" and arg:
            try:
                self.chance = max(0.0, min(1.0, float(arg)))
                print(GREY + f"  the ghost comments on {int(self.chance * 100)}% of ordinary commands" + RESET)
            except ValueError:
                print(GREY + "  :chance takes a number from 0 to 1" + RESET)
        elif cmd == "quiet":
            self.quiet = not self.quiet
            print(GREY + ("  it pretends to be gone." if self.quiet else "  it's back.") + RESET)
        elif cmd == "wake":
            self.awake, self.why_asleep = ollama_ready(self.model)
            print(GREY + ("  it stirs." if self.awake else "  " + self.why_asleep) + RESET)
        elif cmd == "memory":
            if not self.mem["omens"]:
                print(GREY + "  it remembers nothing. yet." + RESET)
            for o in self.mem["omens"][-15:]:
                print(GREY + f"  {ago(o['ts']):>16}  {o['omen']:<34} {o['cmd']}" + RESET)
        elif cmd == "forget":
            self.mem = {"sessions": self.mem["sessions"], "omens": []}
            save_memory(self.mem)
            self.convo.clear()
            print(GREY + "  it forgets. it says it forgets." + RESET)
        else:
            print(GREY + """
  :haunt 1|2|3     how haunted (1 wry · 2 unsettling · 3 mournful)
  :model <name>    swap the spirit, e.g. :model llama3.2:3b
  :chance 0-1      how often it speaks on ordinary commands (omens always)
  :quiet           toggle whispers off/on
  :memory          what it remembers from previous nights
  :forget          wipe its memory
  :wake            reconnect to ollama
  !<command>       run an interactive program without the ghost watching output
  exit             leave (it stays)
""" + RESET)

    # ---- main loop ------------------------------------------------------
    def loop(self) -> None:
        self.mem["sessions"] += 1
        save_memory(self.mem)
        self.banner()
        self.greet()
        while True:
            self.maybe_prefill()
            try:
                line = input(self.prompt())
            except EOFError:
                print()
                self.farewell()
                return
            except KeyboardInterrupt:
                print()
                if self.awake and not self.quiet:
                    static_whisper(random.choice(STATIC_WHISPERS))
                continue
            cmd = line.strip()
            if not cmd:
                continue
            if cmd in ("exit", "quit", "logout"):
                self.farewell()
                return
            if cmd.startswith(":"):
                self.meta(cmd)
                continue
            code, tail = run(cmd)
            self.after(cmd, code, tail)


def setup_history() -> None:
    if not HAVE_READLINE:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if HISTORY_FILE.exists():
            readline.read_history_file(str(HISTORY_FILE))
        import atexit
        atexit.register(lambda: readline.write_history_file(str(HISTORY_FILE)))
    except Exception:
        pass


def main() -> None:
    setup_history()
    try:
        Possession().loop()
    except KeyboardInterrupt:
        print(RESET)


if __name__ == "__main__":
    main()
