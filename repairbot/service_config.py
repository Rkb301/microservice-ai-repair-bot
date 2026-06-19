"""Service configuration loading."""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import ServiceConfig, ServicesConfig


def load_services_config(path: str | Path) -> ServicesConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return ServicesConfig.model_validate(raw)


def get_service_config(config: ServicesConfig, service_name: str) -> ServiceConfig:
    service = config.by_name().get(service_name)
    if not service:
        raise ValueError(f"Service {service_name!r} was not found in services config")
    return service


def resolve_service_repo_path(
    *,
    repo_root: str | Path,
    service: ServiceConfig,
    services_config_path: str | Path | None = None,
) -> Path:
    repo = Path(repo_root).resolve()
    if not service.repo_path:
        return repo
    raw_path = Path(service.repo_path)
    if raw_path.is_absolute():
        resolved = raw_path.resolve()
    else:
        config_base = Path(services_config_path).resolve().parent if services_config_path else repo
        resolved = (config_base / raw_path).resolve()
    try:
        resolved.relative_to(repo)
    except ValueError as exc:
        raise ValueError(
            f"Service {service.name!r} repo_path escapes repository: {service.repo_path}"
        ) from exc
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(
            f"Service {service.name!r} repo_path does not exist: {resolved}"
        )
    return resolved


def parse_log_mappings(values: list[str] | None) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError("--logs entries must look like service-name=path/to/log")
        service, path = value.split("=", 1)
        service = service.strip()
        path = path.strip()
        if not service or not path:
            raise ValueError("--logs entries must include both service and path")
        mappings[service] = path
    return mappings


def resolve_service_logs(
    *,
    services_config: ServicesConfig,
    logs_dir: str | Path | None = None,
    explicit_logs: dict[str, str] | None = None,
) -> dict[str, Path]:
    explicit_logs = explicit_logs or {}
    resolved: dict[str, Path] = {}
    base = Path(logs_dir).resolve() if logs_dir else None
    for service in services_config.services:
        if service.name in explicit_logs:
            resolved[service.name] = Path(explicit_logs[service.name]).resolve()
        elif service.log_file:
            candidate = Path(service.log_file)
            resolved[service.name] = (base / candidate.name).resolve() if base else candidate.resolve()
        elif base:
            resolved[service.name] = (base / f"{service.name}.log").resolve()
    return resolved


def detect_test_command(repo_path: str | Path, service: ServiceConfig | None = None) -> str:
    if service and service.test_cmd:
        return service.test_cmd
    repo = Path(repo_path)
    if (repo / "pom.xml").exists():
        return "mvn test"
    if (repo / "gradlew").exists():
        return ".\\gradlew test" if _is_windows() else "./gradlew test"
    if (repo / "build.gradle").exists() or (repo / "build.gradle.kts").exists():
        return "gradle test"
    return "mvn test"


def _is_windows() -> bool:
    return "\\" in str(Path.cwd())
