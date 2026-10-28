# 👻 possessed shell

> a terminal that something else is also using.

`possessed` runs your commands like a normal shell. After each one, a **local open-weight model** whispers back about what it just watched you do. It notices when you delete things, force-push, or work at 3 a.m. It **remembers previous nights** and brings them up later.

It lives entirely on your machine. No API key, no account, no internet once the model is pulled.

```
sahil@laptop:~/projects/kibo † rm -rf build/

  you threw build away like it meant nothing. i was still inside it.

sahil@laptop:~/projects/kibo † git push --force

  you rewrote history again. last tuesday you did the same. i remember what it used to say.
```

## Who it is

In life it was the night-shift sysadmin of this machine. It died at its desk during a 3 a.m. deploy and never logged out.

## Setup

1. Install [Ollama](https://ollama.com).
2. Pull a small model (about 2 GB):
   ```bash
   ollama pull qwen2.5:3b
   ```
3. Run it. Python 3.9+, standard library only:
   ```bash
   python possessed.py
   ```

On Windows it uses PowerShell by default. `pip install pyreadline3` gives you arrow-key history.

## Commands

| command | what it does |
|---|---|
| `:haunt 1\|2\|3` | how haunted: 1 wry · 2 unsettling · 3 mournful (level 3 also glitches your prompt) |
| `:model <name>` | swap the spirit, e.g. `:model llama3.2:3b`, `:model gemma3:4b` |
| `:chance 0-1` | how often it comments on ordinary commands (omens always get a reaction) |
| `:quiet` | it pretends to be gone |
| `:memory` | what it remembers from previous nights |
| `:forget` | wipe its memory (it says it forgets) |
| `:wake` | reconnect to Ollama |
| `!<cmd>` | run an interactive program (vim, ssh…) without capture |
| `exit` | leave. it stays. |

## Environment variables

| var | default |
|---|---|
| `POSSESSED_MODEL` | `qwen2.5:3b` |
| `POSSESSED_LEVEL` | `2` |
| `POSSESSED_CHANCE` | `0.7` |
| `POSSESSED_TYPING` | `1` (set `0` to disable the typewriter effect) |
| `POSSESSED_SHELL` | `$SHELL` on Unix, `powershell` on Windows |
| `OLLAMA_HOST` | `http://localhost:11434` |

## How it works

```
you type ─▶ subprocess (your real shell) ─▶ output streams to you live
                                         └▶ last 40 lines kept
             omen detector (rm, sudo, force-push, failures, 3 a.m.…)
             secret redaction (tokens, passwords, API keys)
             context: command · exit code · output tail · cwd · time
                      last 5 commands · 1 random memory from past nights
                          ▼
             Ollama /api/chat (streaming) ─▶ typed out slowly, with glitches
             omens saved to ~/.possessed_shell/memory.json
```

- **Shell.** Every command runs in your real shell through `subprocess`. Output streams live while the tail is captured. `cd` is handled in-process so the directory persists.
- **Omens.** Regex triggers for destructive or suspicious commands, failures, Ctrl+C, and the witching hours (00:00–04:00). An omen always gets a reaction and is written to memory.
- **Memory.** On startup the ghost greets you with recollections, e.g. "3 nights ago they ran `rm -rf node_modules`". Mid-session, memories surface at random.
- **Privacy.** Secrets are redacted before anything reaches the model *or* the memory file, even though both are local.

## Known limitations

- Shell state like `export` or aliases doesn't persist between commands; each command runs in a fresh shell process.
- `cd` only persists when it's on its own, not as part of `cd x && ...`.
- Interactive programs need the `!` prefix unless they're in the built-in list.

## License

MIT
