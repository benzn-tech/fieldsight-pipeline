"""Deployed config -> the eval process's env (spec §2.1.2, §7; review S7).

Every NON-secret variable of the deployed `${P}-ask-voice-worker` and `${P}-rag-search`
is copied (the modules read dozens at import; an allowlist by name would miss one).
Secrets are refused by a suffix-anchored deny pattern -- `_TOKEN$`, so ASK_ANSWER_TOKENS
and LLM_ANSWER_TOKEN_CEILING survive -- and exactly three are injected by name. Values
are never printed, logged or written: the fingerprint says `{name: present}` only.
"""
import re
from urllib.parse import urlparse

DENY = re.compile(r"(?:API_KEY|_KEY|SECRET|PASSWORD|_TOKEN|CREDENTIALS?)$", re.I)
SECRETS = ("CORROBORATION_API_KEY", "DASHSCOPE_API_KEY")   # + QWEN_API_KEY iff qwen
FINGERPRINT_VALUES = ("ASK_JUDGE_MODEL", "ASK_JUDGE_FALLBACKS", "ASK_JUDGE_PROVIDER",
                      "ASK_JUDGE_REASONING", "ENABLE_RERANK", "RERANK_CANDIDATE_K",
                      "RERANK_KEYWORD_CANDIDATES", "PROJECT_DOCUMENTS", "SEARCH_INDEX_READ",
                      "LLM_PROVIDER", "SERVER_BUILD", "ASK_ANSWER_LANGUAGE")
RDS_DATA_HOST = "rds-data.ap-southeast-2.amazonaws.com"
STS_HOSTS = "sts.*.amazonaws.com"


def function_names(prefix):
    return {"worker": prefix + "-ask-voice-worker", "rag": prefix + "-rag-search"}


def fetch_configs(lambda_client, prefix):
    """(copied non-secret env, the injected secrets, fingerprint). Read-only:
    `get_function_configuration` twice. The caller injects both dicts and drops them."""
    names = function_names(prefix)
    got = {k: lambda_client.get_function_configuration(FunctionName=n) for k, n in names.items()}
    variables = {k: dict((c.get("Environment") or {}).get("Variables") or {})
                 for k, c in got.items()}
    merged = {**variables["worker"], **variables["rag"]}   # rag-search owns its own flags
    copied = {k: v for k, v in merged.items() if not DENY.search(k)}
    wanted = SECRETS + (("QWEN_API_KEY",) if copied.get("LLM_PROVIDER") == "qwen" else ())
    secrets = {k: variables["worker"][k] for k in wanted if variables["worker"].get(k)}
    missing = [k for k in wanted if k not in secrets]
    if missing:
        raise SystemExit("deployed worker config has no %s" % ", ".join(missing))
    provider = copied.get("LLM_PROVIDER", "anthropic")
    fp = {
        "secrets": {k: k in secrets for k in SECRETS + ("QWEN_API_KEY",)},
        "values": {k: copied.get(k) for k in FINGERPRINT_VALUES},
        "qwen_base_host": urlparse(copied.get("QWEN_BASE_URL") or "").hostname,
        "deployed": {names[k]: {"CodeSha256": c.get("CodeSha256"),
                                "LastModified": c.get("LastModified"),
                                "SERVER_BUILD": variables[k].get("SERVER_BUILD")}
                     for k, c in got.items()},
        "fallback_synthesis": ("qwen" if provider == "qwen" else
                               "%s: no key injected -> expect +no_synthesis" % provider),
    }
    del variables, merged, got
    return copied, secrets, fp


def allowed_hosts(values, *, judge_url, dashscope_urls):
    """The urllib3 allowlist, derived from the resolved config (review S7), never
    hard-coded: the judge host, DashScope (embed/rerank), the qwen fallback host only
    when that is the provider, the Data API and STS (review S5)."""
    hosts = {urlparse(judge_url).hostname, RDS_DATA_HOST, STS_HOSTS}
    hosts |= {urlparse(u).hostname for u in dashscope_urls}
    if values.get("LLM_PROVIDER") == "qwen" and values.get("QWEN_BASE_URL"):
        hosts.add(urlparse(values["QWEN_BASE_URL"]).hostname)
    return {h for h in hosts if h}
