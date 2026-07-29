"""Unified LLM provider config — supports Groq and OpenAI interchangeably.

.env / env vars:
  LLM_PROVIDER   groq | openai   (default: groq — backward compatible)
  LLM_MODEL      model name      (default: per provider)
  GROQ_API_KEY
  OPENAI_API_KEY

CLI flags --llm-provider / --llm-model override env values per run.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_DEFAULTS: dict[str, str] = {
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-4o",
}
_BASE_URLS: dict[str, str] = {
    "groq": "https://api.groq.com/openai/v1/chat/completions",
    "openai": "https://api.openai.com/v1/chat/completions",
}
# Groq is behind Cloudflare; the python-groq UA bypasses the WAF block.
_USER_AGENTS: dict[str, str] = {
    "groq": "python-groq/0.9.0",
    "openai": "python-openai/1.0.0",
}


@dataclass
class LLMConfig:
    provider: str   # "groq" | "openai"
    model: str
    api_key: str


def _read_env_files(*env_files: str | Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for f in env_files:
        p = Path(f)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip("\"'")
            if k and k not in env:
                env[k] = v
    return env


def get_llm_config(
    *env_files: str | Path,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> LLMConfig:
    """Build LLMConfig: CLI override > os.environ > .env files > defaults."""
    file_env = _read_env_files(*env_files)

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or file_env.get(key, default)

    provider = (provider_override or get("LLM_PROVIDER", "groq")).lower()
    if provider not in _DEFAULTS:
        raise ValueError(f"Unknown LLM_PROVIDER '{provider}'; supported: {', '.join(_DEFAULTS)}")

    model = model_override or get("LLM_MODEL", _DEFAULTS[provider])
    api_key = get("GROQ_API_KEY") if provider == "groq" else get("OPENAI_API_KEY")

    return LLMConfig(provider=provider, model=model, api_key=api_key)


def make_lc_model(cfg: LLMConfig):
    """Return a LangChain chat model (ChatGroq or ChatOpenAI) for use in agent.py."""
    if cfg.provider == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(model=cfg.model, api_key=cfg.api_key, temperature=0.2)
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, temperature=0.2)


def call_llm_api(prompt: str, cfg: LLMConfig) -> dict:
    """POST to chat completions in JSON mode via raw urllib (Groq or OpenAI).

    Same wire format for both providers — only the base URL, auth header, and
    User-Agent differ. Raises RuntimeError on any failure.
    """
    url = _BASE_URLS[cfg.provider]
    body = json.dumps({
        "model": cfg.model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "max_tokens": 8192,
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {cfg.api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", _USER_AGENTS[cfg.provider])
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        raise RuntimeError(f"{cfg.provider.upper()} LLM call failed: {e}") from e
