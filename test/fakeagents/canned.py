"""Canned agent replies for the fake agents (no subscription spent): moments for a moments
prompt, post drafts for a draft prompt, as a ```json block the way a real agent answers."""
import json, re


def reply(p):
    if "Find 3-8 MOMENTS" in p:
        m = re.search(r'FOOTAGE: ".*?" — .*?, (\d+):(\d+\.\d)', p)
        d = int(m.group(1)) * 60 + float(m.group(2)) if m else 30
        out = [{"start": 1.0, "end": round(d * .4, 1), "title": "Two hours every night", "hook": "I used to spend two hours every night", "pillar": "time back", "why": "Concrete pain.", "angle": "past-self", "kind": "talking"},
               {"start": round(d * .45, 1), "end": round(d * .8, 1), "title": "Never leaves my phone", "hook": "the audio never leaves my phone", "pillar": "privacy", "why": "Trust.", "angle": "everyday-stakes", "kind": "talking"}]
    else:
        ns = re.findall(r"^#(\d+) ", p, re.M)
        out = {"posts": [{"n": int(n), "short": f"Hook {n}\nline", "x": f"X post {n}", "linkedin": f"LinkedIn {n}", "title": f"Title {n}"} for n in ns],
               "newsletter": "Newsletter.", "ideas": ["a", "b", "c"]}
    return "```json\n" + json.dumps(out) + "\n```"


def record(argv, prompt):
    """$FAKE_AGENT_LOG: one JSON line per call (argv, cwd, prompt head, $STUDIO_AGENT_MODEL)."""
    import os
    log = os.environ.get("FAKE_AGENT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"argv": argv, "cwd": os.getcwd(), "prompt": prompt[:80],
                                "model_env": os.environ.get("STUDIO_AGENT_MODEL")}) + "\n")
