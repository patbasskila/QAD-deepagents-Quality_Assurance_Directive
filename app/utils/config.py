# app/utils/config.py
import os
from dataclasses import dataclass
from typing import Optional, Any, Dict

from dotenv import load_dotenv


def _truthy(v: Optional[str]) -> bool:
    if v is None:
        return False
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _coerce_int(v: Any, default: int) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _coerce_float(v: Any, default: float) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _coerce_opt_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        return int(v)
    except Exception:
        return None


@dataclass(frozen=True)
class Settings:
    # ----------------------------
    # LLM mode (offline dev vs corp)
    # ----------------------------
    llm_mode: str  # "mock" or "azure"
    llm_timeout_seconds: float
    llm_max_retries: int
    llm_use_http2: bool
    llm_mock_seed: int

    # ----------------------------
    # UI behavior
    # ----------------------------
    ui_allow_debug_downloads: bool  # UI toggle visibility

    # ----------------------------
    # Server behavior (security)
    # ----------------------------
    server_allow_debug_downloads: bool  # server-side enforcement for debug artifacts

    # ----------------------------
    # Azure OpenAI (required only in azure mode)
    # ----------------------------
    openai_api_key: str
    azure_openai_endpoint: str
    azure_openai_api_version: str
    azure_openai_deployment: str

    # App
    app_env: str
    app_host: str
    app_port: int
    log_level: str

    # ----------------------------
    # Embeddings (RAG)
    # ----------------------------
    embeddings_provider: str  # "huggingface" | "local"
    hf_token: str
    hf_embed_model: str
    hf_embed_endpoint: str

    local_embed_model: str
    local_embed_device: str
    local_embed_backend: str
    local_embed_cache_dir: str
    local_embed_local_files_only: bool

    # Runtime
    tmp_dir: str
    max_upload_mb: int

    # OCR
    ocr_enabled: bool
    tesseract_cmd: Optional[str]

    # Email (Section 5)
    email_enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_use_tls: bool
    smtp_username: str
    smtp_password: str
    smtp_from: str
    email_attach_debug_artifacts: bool
    email_max_attachment_mb: int

    # Cleanup (Section 5)
    job_retention_days: int

    # ----------------------------
    # DeepAgents (Section 6.9)
    # ----------------------------
    deepagents_preset: str  # local_prototype | enterprise_demo | custom
    deepagents_planner_enabled: bool
    deepagents_planner_temperature: float
    deepagents_temperature: float
    deepagents_areas_cap: Optional[int]
    deepagents_repair_enabled: bool
    deepagents_quality_enabled: bool

    deepagents_export_sort_desc: bool
    deepagents_export_drop_bad: bool
    deepagents_export_drop_empty: bool
    deepagents_export_min_score: int
    deepagents_export_max_checks: Optional[int]


_SETTINGS: Optional[Settings] = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is not None:
        return _SETTINGS

    load_dotenv(override=False)

    def getenv(name: str, default: Optional[str] = None) -> str:
        val = os.environ.get(name, default)
        return str(val) if val is not None else ""

    # ---- LLM mode ----
    llm_mode = (getenv("LLM_MODE", "mock") or "mock").strip().lower()
    if llm_mode not in {"mock", "azure"}:
        raise RuntimeError("LLM_MODE must be 'mock' or 'azure'.")

    llm_timeout_seconds = float(getenv("LLM_TIMEOUT_SECONDS", "90"))
    llm_max_retries = int(getenv("LLM_MAX_RETRIES", "0"))
    llm_use_http2 = _truthy(getenv("LLM_USE_HTTP2", "false"))
    llm_mock_seed = int(getenv("LLM_MOCK_SEED", "7"))

    # ---- UI flags ----
    ui_allow_debug_downloads = _truthy(getenv("UI_ALLOW_DEBUG_DOWNLOADS", "false"))

    # ---- Server debug gating (important for production) ----
    server_allow_debug_downloads = _truthy(getenv("SERVER_ALLOW_DEBUG_DOWNLOADS", "false"))

    # ----------------------------
    # Embeddings provider
    # ----------------------------
    embeddings_provider = (getenv("EMBEDDINGS_PROVIDER", "huggingface") or "huggingface").strip().lower()
    if embeddings_provider not in {"huggingface", "hf", "remote", "local", "offline"}:
        raise RuntimeError("EMBEDDINGS_PROVIDER must be one of: huggingface, local")

    # Hugging Face (remote)
    hf_token = getenv("HF_TOKEN", "")
    hf_embed_model = getenv("HF_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    hf_embed_endpoint = getenv("HF_EMBED_ENDPOINT", "")

    # Local embeddings (optional)
    local_embed_model = getenv("LOCAL_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    local_embed_device = getenv("LOCAL_EMBED_DEVICE", "cpu")
    local_embed_backend = getenv("LOCAL_EMBED_BACKEND", "auto")
    local_embed_cache_dir = getenv("LOCAL_EMBED_CACHE_DIR", "")
    local_embed_local_files_only = _truthy(getenv("LOCAL_EMBED_LOCAL_FILES_ONLY", "false"))

    # ---- Azure vars ----
    openai_api_key = getenv("OPENAI_API_KEY", "")
    azure_openai_endpoint = getenv("AZURE_OPENAI_ENDPOINT", "")
    azure_openai_api_version = getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")
    azure_openai_deployment = getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")

    if llm_mode == "azure":
        if not openai_api_key.strip():
            raise RuntimeError("Missing required environment variable: OPENAI_API_KEY (azure mode)")
        if not azure_openai_endpoint.strip():
            raise RuntimeError("Missing required environment variable: AZURE_OPENAI_ENDPOINT (azure mode)")

    # ---- OCR ----
    ocr_enabled = _truthy(getenv("OCR_ENABLED", "true"))
    tesseract_cmd = (lambda x: x if x.strip() else None)(getenv("TESSERACT_CMD", ""))

    # ---- Email (Section 5) ----
    email_enabled = _truthy(getenv("EMAIL_ENABLED", "false"))
    smtp_host = getenv("SMTP_HOST", "smtp.example.com")
    smtp_port = int(getenv("SMTP_PORT", "25"))
    smtp_use_tls = _truthy(getenv("SMTP_USE_TLS", "false"))
    smtp_username = getenv("SMTP_USERNAME", "")
    smtp_password = getenv("SMTP_PASSWORD", "")
    smtp_from = getenv("SMTP_FROM", "noreply@example.com")
    email_attach_debug_artifacts = _truthy(getenv("EMAIL_ATTACH_DEBUG_ARTIFACTS", "false"))
    email_max_attachment_mb = int(getenv("EMAIL_MAX_ATTACHMENT_MB", "15"))

    # ---- Cleanup ----
    job_retention_days = int(getenv("JOB_RETENTION_DAYS", "7"))

    # ----------------------------
    # DeepAgents preset + overrides (Section 6.9)
    # ----------------------------
    deepagents_preset = (getenv("DEEPAGENTS_PRESET", "local_prototype") or "local_prototype").strip().lower()
    if deepagents_preset not in {"local_prototype", "enterprise_demo", "custom"}:
        raise RuntimeError("DEEPAGENTS_PRESET must be one of: local_prototype, enterprise_demo, custom")

    # Preset defaults (secure + demo-friendly)
    preset_defaults: Dict[str, Any]
    if deepagents_preset == "enterprise_demo":
        preset_defaults = {
            "deepagents_planner_enabled": True,
            "deepagents_planner_temperature": 0.0,
            "deepagents_temperature": 0.0,
            "deepagents_areas_cap": 8,
            "deepagents_repair_enabled": True,
            "deepagents_quality_enabled": True,
            "deepagents_export_sort_desc": True,
            "deepagents_export_drop_bad": True,
            "deepagents_export_drop_empty": True,
            "deepagents_export_min_score": 55,
            "deepagents_export_max_checks": 60,
        }
    else:
        # local_prototype (and custom default base)
        preset_defaults = {
            "deepagents_planner_enabled": False,  # local dev = deterministic default plan
            "deepagents_planner_temperature": 0.0,
            "deepagents_temperature": 0.0,
            "deepagents_areas_cap": None,
            "deepagents_repair_enabled": False,   # keep behavior predictable for offline dev
            "deepagents_quality_enabled": True,
            "deepagents_export_sort_desc": True,
            "deepagents_export_drop_bad": False,
            "deepagents_export_drop_empty": False,
            "deepagents_export_min_score": 0,
            "deepagents_export_max_checks": None,
        }

    # Apply overrides only if env var is present (not just default)
    env = os.environ

    def _override_bool(name: str, current: bool) -> bool:
        return _coerce_bool(env.get(name), current) if name in env else current

    def _override_float(name: str, current: float) -> float:
        return _coerce_float(env.get(name), current) if name in env else current

    def _override_int(name: str, current: int) -> int:
        return _coerce_int(env.get(name), current) if name in env else current

    def _override_opt_int(name: str, current: Optional[int]) -> Optional[int]:
        return _coerce_opt_int(env.get(name)) if name in env else current

    deepagents_planner_enabled = _override_bool("DEEPAGENTS_PLANNER_ENABLED", bool(preset_defaults["deepagents_planner_enabled"]))
    deepagents_planner_temperature = _override_float("DEEPAGENTS_PLANNER_TEMPERATURE", float(preset_defaults["deepagents_planner_temperature"]))
    deepagents_temperature = _override_float("DEEPAGENTS_TEMPERATURE", float(preset_defaults["deepagents_temperature"]))
    deepagents_areas_cap = _override_opt_int("DEEPAGENTS_AREAS_CAP", preset_defaults["deepagents_areas_cap"])
    deepagents_repair_enabled = _override_bool("DEEPAGENTS_REPAIR_ENABLED", bool(preset_defaults["deepagents_repair_enabled"]))
    deepagents_quality_enabled = _override_bool("DEEPAGENTS_QUALITY_ENABLED", bool(preset_defaults["deepagents_quality_enabled"]))

    deepagents_export_sort_desc = _override_bool("DEEPAGENTS_EXPORT_SORT_DESC", bool(preset_defaults["deepagents_export_sort_desc"]))
    deepagents_export_drop_bad = _override_bool("DEEPAGENTS_EXPORT_DROP_BAD", bool(preset_defaults["deepagents_export_drop_bad"]))
    deepagents_export_drop_empty = _override_bool("DEEPAGENTS_EXPORT_DROP_EMPTY", bool(preset_defaults["deepagents_export_drop_empty"]))
    deepagents_export_min_score = _override_int("DEEPAGENTS_EXPORT_MIN_SCORE", int(preset_defaults["deepagents_export_min_score"]))
    deepagents_export_max_checks = _override_opt_int("DEEPAGENTS_EXPORT_MAX_CHECKS", preset_defaults["deepagents_export_max_checks"])

    _SETTINGS = Settings(
        llm_mode=llm_mode,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_max_retries=llm_max_retries,
        llm_use_http2=llm_use_http2,
        llm_mock_seed=llm_mock_seed,

        ui_allow_debug_downloads=ui_allow_debug_downloads,
        server_allow_debug_downloads=server_allow_debug_downloads,

        openai_api_key=openai_api_key,
        azure_openai_endpoint=azure_openai_endpoint,
        azure_openai_api_version=azure_openai_api_version,
        azure_openai_deployment=azure_openai_deployment,

        app_env=getenv("APP_ENV", "local"),
        app_host=getenv("APP_HOST", "0.0.0.0"),
        app_port=int(getenv("APP_PORT", "8000")),
        log_level=getenv("LOG_LEVEL", "INFO"),

        embeddings_provider=embeddings_provider,
        hf_token=hf_token,
        hf_embed_model=hf_embed_model,
        hf_embed_endpoint=hf_embed_endpoint,
        local_embed_model=local_embed_model,
        local_embed_device=local_embed_device,
        local_embed_backend=local_embed_backend,
        local_embed_cache_dir=local_embed_cache_dir,
        local_embed_local_files_only=local_embed_local_files_only,

        tmp_dir=getenv("TMP_DIR", "tmp"),
        max_upload_mb=int(getenv("MAX_UPLOAD_MB", "40")),

        ocr_enabled=ocr_enabled,
        tesseract_cmd=tesseract_cmd,

        email_enabled=email_enabled,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_use_tls=smtp_use_tls,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_from=smtp_from,
        email_attach_debug_artifacts=email_attach_debug_artifacts,
        email_max_attachment_mb=email_max_attachment_mb,

        job_retention_days=job_retention_days,

        deepagents_preset=deepagents_preset,
        deepagents_planner_enabled=deepagents_planner_enabled,
        deepagents_planner_temperature=deepagents_planner_temperature,
        deepagents_temperature=deepagents_temperature,
        deepagents_areas_cap=deepagents_areas_cap,
        deepagents_repair_enabled=deepagents_repair_enabled,
        deepagents_quality_enabled=deepagents_quality_enabled,

        deepagents_export_sort_desc=deepagents_export_sort_desc,
        deepagents_export_drop_bad=deepagents_export_drop_bad,
        deepagents_export_drop_empty=deepagents_export_drop_empty,
        deepagents_export_min_score=deepagents_export_min_score,
        deepagents_export_max_checks=deepagents_export_max_checks,
    )
    return _SETTINGS
