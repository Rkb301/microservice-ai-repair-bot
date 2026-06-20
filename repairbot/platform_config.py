"""Safe, layered YAML configuration for external repair targets."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from string import Formatter
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .architecture import MVP_MODEL


PROJECT_CONFIG_NAMES = (
    "repairbot.yaml",
    ".repairbot.yaml",
    "repairbot.yml",
    ".repairbot.yml",
)
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "repairbot.default.yaml"
MAX_CONFIG_BYTES = 128_000
SECRET_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|password|passwd|secret|credential|private[_-]?key)"
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"-{5}BEGIN [A-Z ]*PRIVATE KEY-{5}"),
)
ALLOWED_TEMPLATE_FIELDS = {
    "service_name",
    "error_name",
    "description",
    "proposed_solution",
    "selected_hypothesis",
    "repo_url",
    "target_branch",
}


class ConfigSafetyError(ValueError):
    """Raised when untrusted config contains a secret or unsafe value."""


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = MVP_MODEL
    allowed_models: list[str] = Field(default_factory=lambda: [MVP_MODEL])
    max_hypotheses: int = Field(default=5, ge=1, le=20)
    max_attempts: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def selected_model_is_allowed(self, info: ValidationInfo) -> "ModelConfig":
        if not self.allowed_models:
            raise ValueError("model.allowed_models must contain at least one model")
        partial_layer = bool(info.context and info.context.get("partial_layer"))
        if partial_layer and "allowed_models" not in self.model_fields_set:
            return self
        if self.model not in self.allowed_models:
            raise ValueError("model.model must be present in model.allowed_models")
        if ":" not in self.model or self.model.split(":", 1)[0] != self.provider:
            raise ValueError("model.model must use the configured provider prefix")
        return self


class JiraConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["fake", "real"] = "fake"
    project_key: str = "DEMO"
    issue_type: str = "Bug"
    title_template: str = "[{service_name}] {error_name}"
    description_template: str = "{description}"
    solution_template: str = "{proposed_solution}"
    update_pr_link: bool = True

    @field_validator("project_key")
    @classmethod
    def valid_project_key(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,19}", cleaned):
            raise ValueError("jira.project_key must be 2-20 letters, digits, or underscores")
        return cleaned

    @field_validator("issue_type")
    @classmethod
    def valid_issue_type(cls, value: str) -> str:
        cleaned = value.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]{0,39}", cleaned):
            raise ValueError("jira.issue_type contains unsupported characters")
        return cleaned

    @field_validator("title_template", "description_template", "solution_template")
    @classmethod
    def safe_template(cls, value: str) -> str:
        _validate_template(value)
        return value


class GitHubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["fake", "real"] = "fake"
    default_base_branch: str = "main"
    create_pull_request: bool = True

    @field_validator("default_base_branch")
    @classmethod
    def safe_branch(cls, value: str) -> str:
        branch = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch:
            raise ValueError("github.default_base_branch is not a safe branch name")
        return branch


class RepairBehaviorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_plan_approval: bool = True
    dry_run_default: bool = True
    execution_mode: Literal["fake", "real"] = "fake"
    test_command: str = "mvn test"
    build_system: str = "maven"
    language: str = "java"
    module_path: str | None = None

    @field_validator("test_command")
    @classmethod
    def reviewed_test_command(cls, value: str) -> str:
        return validate_test_command(value)

    @field_validator("build_system", "language")
    @classmethod
    def simple_name(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9._-]+", cleaned):
            raise ValueError("build/language values contain unsupported characters")
        return cleaned

    @field_validator("module_path")
    @classmethod
    def safe_module_path(cls, value: str | None) -> str | None:
        return normalize_module_path(value)


class LangSmithConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    project: str = "microservice-ai-repair-bot"
    tags: list[str] = Field(default_factory=lambda: ["mvp-review"])

    @field_validator("project")
    @classmethod
    def safe_project(cls, value: str) -> str:
        cleaned = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._ -]{1,80}", cleaned):
            raise ValueError("langsmith.project contains unsupported characters")
        return cleaned

    @field_validator("tags")
    @classmethod
    def safe_tags(cls, values: list[str]) -> list[str]:
        if len(values) > 20:
            raise ValueError("langsmith.tags supports at most 20 tags")
        cleaned: list[str] = []
        for value in values:
            tag = value.strip()
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", tag):
                raise ValueError("langsmith.tags contains an unsafe tag")
            cleaned.append(tag)
        return cleaned


class RepairBotProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    service_name: str | None = None
    model: ModelConfig = Field(default_factory=ModelConfig)
    jira: JiraConfig = Field(default_factory=JiraConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    repair: RepairBehaviorConfig = Field(default_factory=RepairBehaviorConfig)
    langsmith: LangSmithConfig = Field(default_factory=LangSmithConfig)

    @field_validator("service_name")
    @classmethod
    def safe_service_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", cleaned):
            raise ValueError("service_name contains unsupported characters")
        return cleaned


EffectiveRepairConfig = RepairBotProjectConfig


def load_project_config(
    repo_path: str | Path,
    module_path: str | None = None,
) -> RepairBotProjectConfig | None:
    path = find_project_config_path(repo_path, module_path)
    return load_config_file(path) if path else None


def find_project_config_path(
    repo_path: str | Path,
    module_path: str | None = None,
) -> Path | None:
    root = Path(repo_path).resolve()
    if not root.is_dir():
        raise ValueError("Repository path does not exist")
    module = normalize_module_path(module_path)
    search_roots = [(root / module).resolve()] if module else [root]
    if search_roots[0] != root:
        search_roots.append(root)
    for search_root in search_roots:
        if not search_root.is_relative_to(root):
            raise ConfigSafetyError("Configured module path escapes the repository")
        for name in PROJECT_CONFIG_NAMES:
            candidate = search_root / name
            if candidate.is_file():
                resolved = candidate.resolve()
                if not resolved.is_relative_to(root):
                    raise ConfigSafetyError("RepairBot config path escapes the repository")
                return resolved
    return None


def load_config_file(path: str | Path) -> RepairBotProjectConfig:
    config_path = Path(path).resolve()
    if config_path.suffix.lower() not in {".yaml", ".yml"}:
        raise ConfigSafetyError("RepairBot config must be a YAML file")
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise ConfigSafetyError("RepairBot config file is too large")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("RepairBot config root must be a mapping")
    reject_secret_looking_config(raw)
    return RepairBotProjectConfig.model_validate(raw, context={"partial_layer": True})


def merge_effective_config(
    operator_request: BaseModel | Mapping[str, Any] | None,
    repo_config: RepairBotProjectConfig | None,
    default_config: RepairBotProjectConfig | None,
    env_settings: Any | None,
) -> EffectiveRepairConfig:
    merged = RepairBotProjectConfig().model_dump(mode="python")
    _deep_merge(merged, _env_layer(env_settings))
    if default_config is not None:
        _deep_merge(merged, default_config.model_dump(mode="python", exclude_unset=True))
    if repo_config is not None:
        _deep_merge(merged, repo_config.model_dump(mode="python", exclude_unset=True))
    _deep_merge(merged, _operator_layer(operator_request))
    reject_secret_looking_config(merged)
    return RepairBotProjectConfig.model_validate(merged)


def render_config_template(template: str, values: Mapping[str, Any]) -> str:
    _validate_template(template)
    safe_values = {key: str(values.get(key) or "") for key in ALLOWED_TEMPLATE_FIELDS}
    return template.format_map(safe_values)


def normalize_module_path(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.replace("\\", "/").strip("/")
    if (
        normalized == ".."
        or normalized.startswith("../")
        or "/../" in normalized
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise ConfigSafetyError("module_path must stay inside the cloned repository")
    return normalized


def validate_test_command(value: str) -> str:
    command = " ".join(value.strip().split())
    if not command:
        raise ValueError("repair.test_command must not be empty")
    if any(marker in command for marker in (";", "&&", "||", "|", ">", "<", "`", "$(", "\n", "\r")):
        raise ConfigSafetyError("repair.test_command contains shell control operators")
    lowered = command.lower().replace("\\", "/")
    allowed_prefixes = (
        "mvn test",
        "mvn -q test",
        "./mvnw test",
        "mvnw.cmd test",
        "gradle test",
        "./gradlew test",
        "gradlew.bat test",
        "pytest",
        "python -m pytest",
    )
    if not any(lowered == prefix or lowered.startswith(f"{prefix} ") for prefix in allowed_prefixes):
        raise ConfigSafetyError("repair.test_command is outside the reviewed test-command allowlist")
    return command


def reject_secret_looking_config(value: Any, *, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_PATTERN.search(key_text):
                raise ConfigSafetyError(f"Prohibited secret-looking config key at {path}.{key_text}")
            reject_secret_looking_config(item, path=f"{path}.{key_text}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            reject_secret_looking_config(item, path=f"{path}[{index}]")
        return
    if not isinstance(value, str):
        return
    if any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS):
        raise ConfigSafetyError(f"Prohibited secret-looking value at {path}")
    parsed = urlsplit(value)
    if parsed.scheme and (parsed.username or parsed.password):
        raise ConfigSafetyError(f"Credential-bearing URL is prohibited at {path}")


def _operator_layer(request: BaseModel | Mapping[str, Any] | None) -> dict[str, Any]:
    if request is None:
        return {}
    if isinstance(request, BaseModel):
        raw = request.model_dump(exclude_none=True)
    else:
        raw = {key: value for key, value in request.items() if value is not None}
    layer: dict[str, Any] = {}
    if "service_name" in raw:
        layer["service_name"] = raw["service_name"]
    model = raw.get("model_name") or raw.get("model")
    if isinstance(model, str):
        layer.setdefault("model", {})["model"] = model
    mappings = {
        "repo_branch": ("github", "default_base_branch"),
        "github_mode": ("github", "mode"),
        "test_cmd": ("repair", "test_command"),
        "require_approval": ("repair", "require_plan_approval"),
        "execution_mode": ("repair", "execution_mode"),
        "dry_run": ("repair", "dry_run_default"),
        "module_path": ("repair", "module_path"),
        "language": ("repair", "language"),
        "build_system": ("repair", "build_system"),
        "jira_project": ("jira", "project_key"),
    }
    for source, (section, target) in mappings.items():
        if source in raw:
            layer.setdefault(section, {})[target] = raw[source]
    return layer


def _env_layer(settings: Any | None) -> dict[str, Any]:
    if settings is None:
        return {}
    layer: dict[str, Any] = {}
    model = getattr(settings, "model", None)
    provider = getattr(settings, "provider", None)
    if model:
        layer["model"] = {
            "model": model,
            "provider": provider or model.split(":", 1)[0],
            "allowed_models": [model],
            "max_hypotheses": getattr(settings, "max_hypotheses", 5),
            "max_attempts": getattr(settings, "max_fix_attempts", 2),
        }
    project = getattr(settings, "langsmith_project", None)
    if project:
        layer["langsmith"] = {
            "project": project,
            "enabled": bool(getattr(settings, "langsmith_tracing", False)),
        }
    jira_project = getattr(settings, "jira_project_key", None)
    if jira_project:
        layer["jira"] = {"project_key": jira_project}
    return layer


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _validate_template(template: str) -> None:
    if len(template) > 4000:
        raise ValueError("Jira template is too long")
    try:
        parsed = Formatter().parse(template)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if field_name not in ALLOWED_TEMPLATE_FIELDS:
                raise ValueError(f"Unsupported Jira template field: {field_name}")
            if format_spec or conversion:
                raise ValueError("Jira templates do not support conversions or format specs")
    except (ValueError, KeyError) as exc:
        raise ValueError("Invalid Jira template") from exc

