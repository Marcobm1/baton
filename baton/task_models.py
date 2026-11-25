"""Model pickers from the installed CLIs. Discovery never submits a user prompt."""
import difflib
import json
import os
import re
import subprocess
import time

from .i18n import STRINGS
from .ping import _NO_WINDOW, _clean_env, find_agy, find_claude, find_codex

_cache = {}

KNOWN_OPENCODE_GO_MODELS = (
    "opencode-go/beam-free",
    "opencode-go/claude-haiku-5-5",
    "opencode-go/deepseek-v4-pro",
    "opencode-go/deepseek-v4.1-flash",
    "opencode-go/gemini-4-argon",
    "opencode-go/glm-5.3",
    "opencode-go/glm-5.3-flash",
    "opencode-go/gpt-5.6-luna",
    "opencode-go/gpt-6-luna",
    "opencode-go/grok-4.7",
    "opencode-go/hungerg-free",
    "opencode-go/hy3",
    "opencode-go/hy4",
    "opencode-go/hy4-flash-free",
    "opencode-go/hy4-preview",
    "opencode-go/iceman-contributor-free",
    "opencode-go/kimi-k2.7-code",
    "opencode-go/kimi-k3",
    "opencode-go/kimi-k3.1-pro",
    "opencode-go/longcat-2.5",
    "opencode-go/mimo-v2.6-flash",
    "opencode-go/mimo-v2.6-pro",
    "opencode-go/minimax-m3",
    "opencode-go/minimax-m3.1-flash",
    "opencode-go/muse-spark-1.2-contributor",
    "opencode-go/muse-spark-1.3-contributor",
    "opencode-go/qwen3.7-plus",
    "opencode-go/qwen3.8-flash",
    "opencode-go/qwen3.8-max",
    "opencode-go/qwen4-max",
    "opencode-go/step-5-preview",
)


def opencode_groups(models: list[str], t: dict[str, str] | None = None,
                    active_families: set[str] | None = None) -> list[dict]:
    """Group OpenCode models by provider with clear labels for OpenCode Go vs pay-as-you-go."""
    labels = t or STRINGS["en"]
    active = {f.casefold() for f in (active_families or ()) if f}
    by_provider: dict[str, list[str]] = {}
    for model in models:
        provider = model.split("/", 1)[0] if "/" in model else model
        by_provider.setdefault(provider, []).append(model)
    order = sorted(
        by_provider,
        key=lambda p: (
            (0, 0) if p == "opencode-go"
            else (1, 0) if p == "opencode"
            else (2, 0 if any(f in p.casefold() for f in active) else 1)
        ),
    )
    groups = []
    for provider in order:
        if provider == "opencode-go":
            label = labels.get("oc_group_go", STRINGS["en"]["oc_group_go"])
        elif provider == "opencode":
            label = labels.get("oc_group_payg", STRINGS["en"]["oc_group_payg"])
        else:
            label = provider
        groups.append({
            "provider": provider,
            "label": label,
            "included": provider == "opencode-go",
            "payg": provider == "opencode",
            "models": by_provider[provider],
        })
    return groups


def _model_tokens(slug: str) -> list[str]:
    return re.findall(r"[a-z]+|\d+(?:\.\d+)?", slug.lower())


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    count = 0
    for x, y in zip(a, b):
        if x != y:
            break
        count += 1
    return count


def closest_opencode_go_model(model: str, models: list[str] | None = None) -> str:
    """Return the closest included `opencode-go/...` model for a given model name."""
    if not model:
        return ""
    if model.startswith("opencode-go/"):
        return model
    candidates = [m for m in (models or ()) if m.startswith("opencode-go/")]
    if not candidates:
        candidates = list(KNOWN_OPENCODE_GO_MODELS)
    target = model.split("/", 1)[-1].lower()
    target_tokens = _model_tokens(target)
    if not target_tokens:
        return candidates[0]

    def score(candidate: str) -> tuple:
        slug = candidate.split("/", 1)[-1].lower()
        cand_tokens = _model_tokens(slug)
        same_family = bool(cand_tokens and cand_tokens[0] == target_tokens[0])
        prefix_tokens = _common_prefix_len(target_tokens, cand_tokens)
        shared_tokens = len(set(target_tokens) & set(cand_tokens))
        char_prefix = len(os.path.commonprefix([target, slug]))
        ratio = difflib.SequenceMatcher(None, target, slug).ratio()
        return (slug == target, prefix_tokens, same_family, shared_tokens, char_prefix, ratio)

    return max(candidates, key=score)


# Only Gemini models are offered through agy: its Claude and GPT models have a
# separate allowance and are already reachable through their own CLIs.
GEMINI_MODEL = re.compile(r"gemini-4-[\w.-]+|gemini-3\.8-flash(?:-[\w.-]+)?|gemini-pro-orchestrator[\w.-]*")
GEMINI_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def parse_agy_models(text: str) -> dict:
    """`agy models` lines ("gemini-4-neon-high<TAB>Gemini 4 Neon (High)") as
    {base model: {"label", "efforts"}}, the same shape as the other catalogs.

    agy lists one slug per model and thinking level; its own `--model` and
    `--effort` flags take them apart (`--model gemini-4-neon --effort high`),
    as its ACP server does (`model` + `thought_level`). A slug without a level
    suffix (the Orchestrators) is a model with no effort choice.
    """
    models: dict = {}
    for line in text.splitlines():
        slug, _, label = line.strip().partition("	")
        if not GEMINI_MODEL.fullmatch(slug):
            continue
        base, _, level = slug.rpartition("-")
        if level in GEMINI_EFFORTS and base:
            item = models.setdefault(base, {"label": re.sub(r"\s*\([^)]*\)$", "", label.strip()) or base,
                                            "efforts": []})
            item["efforts"].append(level)
        else:
            models.setdefault(slug, {"label": label.strip() or slug, "efforts": []})
    for item in models.values():
        item["efforts"].sort(key=GEMINI_EFFORTS.index)
    return models


def catalog(agent: str) -> dict:
    now = time.monotonic()
    expires, previous = _cache.get(agent, (0, {}))
    if now < expires:
        return previous
    models = {}
    if agent == "gemini":
        exe = find_agy()
        if not exe:
            return {}
        try:
            result = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                                    env=_clean_env(), **_NO_WINDOW)
            models = parse_agy_models(result.stdout)
        except (OSError, subprocess.SubprocessError):
            models = {}
        _cache[agent] = (now + (600 if models else 30), models)
        return models
    cmd = find_claude() if agent == "claude-code" else find_codex()
    if not cmd:
        return {}
    try:
        args = [cmd, "debug", "models"]
        request = ""
        if agent == "claude-code":
            args = [cmd, "-p", "--input-format", "stream-json", "--output-format",
                    "stream-json", "--verbose", "--no-session-persistence"]
            request = json.dumps({"type": "control_request", "request_id": "baton",
                                  "request": {"subtype": "initialize"}}) + "\n"
        result = subprocess.run(args, input=request, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15,
                                env=_clean_env(), check=True, **_NO_WINDOW)
        if agent == "codex":
            rows = [(m["slug"], m["display_name"], [v["effort"] for v in m["supported_reasoning_levels"]])
                    for m in json.loads(result.stdout)["models"] if m.get("visibility") == "list"]
        else:
            rows = []
            for line in result.stdout.splitlines():
                message = json.loads(line)
                response = message.get("response", {})
                if message.get("type") == "control_response" and response.get("request_id") == "baton":
                    rows = [(m["value"], m["displayName"], m.get("supportedEffortLevels") or [])
                            for m in response["response"]["models"] if m["value"] != "default"]
                    break
        for value, label, efforts in rows:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
                models[value] = {"label": label, "efforts": [e for e in efforts if re.fullmatch("[a-z]+", e)]}
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError):
        pass
    _cache[agent] = (now + (600 if models else 30), models)
    return models
