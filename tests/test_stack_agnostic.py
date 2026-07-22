"""Tests for stack-agnostic detection, scaffolding, and filtering."""

from __future__ import annotations

from pathlib import Path

from uiforgemax.graphify.stack import detect_stack
from uiforgemax.pipeline.target_sanitize import (
    is_implementation_path,
    is_non_implementation_path,
)
from uiforgemax.pipeline.implement import _generic_create


# --- Stack detection ---

def test_detect_empty(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    result = detect_stack(d)
    assert result["primary"] == "greenfield"
    assert result["empty"] is True


def test_detect_react(tmp_path):
    d = tmp_path / "react-app"
    d.mkdir()
    (d / "App.tsx").write_text("import React from 'react';")
    result = detect_stack(d)
    assert result["react"] is True
    assert "react" in result["kinds"]


def test_detect_angular(tmp_path):
    d = tmp_path / "angular-app"
    d.mkdir()
    (d / "angular.json").write_text("{}")
    result = detect_stack(d)
    assert result["angular"] is True
    assert "angular" in result["kinds"]


def test_detect_angular_by_decorator(tmp_path):
    d = tmp_path / "ng-app"
    d.mkdir()
    (d / "app.component.ts").write_text("import { Component } from '@angular/core';")
    result = detect_stack(d)
    assert result["angular"] is True


def test_detect_vue(tmp_path):
    d = tmp_path / "vue-app"
    d.mkdir()
    (d / "App.vue").write_text("<template><div>Hello</div></template>")
    result = detect_stack(d)
    assert result["vue"] is True
    assert "vue" in result["kinds"]


def test_detect_svelte(tmp_path):
    d = tmp_path / "svelte-app"
    d.mkdir()
    (d / "App.svelte").write_text("<div>Hello</div>")
    result = detect_stack(d)
    assert result["svelte"] is True


def test_detect_django(tmp_path):
    d = tmp_path / "django-app"
    d.mkdir()
    (d / "views.py").write_text("from django.http import HttpResponse")
    result = detect_stack(d)
    assert result["django"] is True
    assert "django" in result["kinds"]


def test_detect_spring(tmp_path):
    d = tmp_path / "spring-app"
    d.mkdir()
    (d / "Application.java").write_text("@SpringBootApplication public class App {}")
    result = detect_stack(d)
    assert result["spring"] is True


def test_detect_dotnet(tmp_path):
    d = tmp_path / "dotnet-app"
    d.mkdir()
    (d / "MyApp.csproj").write_text("<Project></Project>")
    result = detect_stack(d)
    assert result["dotnet"] is True


def test_detect_golang(tmp_path):
    d = tmp_path / "go-app"
    d.mkdir()
    (d / "go.mod").write_text("module example.com/app")
    result = detect_stack(d)
    assert result["golang"] is True


def test_detect_rust(tmp_path):
    d = tmp_path / "rust-app"
    d.mkdir()
    (d / "Cargo.toml").write_text("[package]\nname = \"myapp\"")
    result = detect_stack(d)
    assert result["rust"] is True


def test_detect_ruby(tmp_path):
    d = tmp_path / "rails-app"
    d.mkdir()
    (d / "Gemfile").write_text("gem 'rails'")
    result = detect_stack(d)
    assert result["ruby"] is True


def test_detect_openfin(tmp_path):
    d = tmp_path / "openfin-app"
    d.mkdir()
    (d / "app.ts").write_text("import { fin } from 'openfin';")
    result = detect_stack(d)
    assert result["openfin"] is True


def test_detect_multi_stack(tmp_path):
    d = tmp_path / "fullstack"
    d.mkdir()
    (d / "main.py").write_text("from fastapi import FastAPI")
    (d / "App.tsx").write_text("import React from 'react';")
    result = detect_stack(d)
    assert result["fastapi"] is True
    assert result["react"] is True
    assert len(result["kinds"]) >= 2


def test_detect_standard_fallback(tmp_path):
    d = tmp_path / "unknown"
    d.mkdir()
    (d / "main.c").write_text("#include <stdio.h>")
    result = detect_stack(d)
    assert result["primary"] == "standard"


# --- Implementation path detection (multi-language) ---

def test_impl_path_python():
    assert is_implementation_path("src/auth/handler.py") is True


def test_impl_path_go():
    assert is_implementation_path("cmd/server/main.go") is True


def test_impl_path_rust():
    assert is_implementation_path("src/lib.rs") is True


def test_impl_path_java():
    assert is_implementation_path("src/main/java/App.java") is True


def test_impl_path_csharp():
    assert is_implementation_path("Controllers/HomeController.cs") is True


def test_impl_path_ruby():
    assert is_implementation_path("app/controllers/users_controller.rb") is True


def test_impl_path_php():
    assert is_implementation_path("src/Controller/ApiController.php") is True


def test_impl_path_kotlin():
    assert is_implementation_path("src/main/kotlin/App.kt") is True


def test_impl_path_vue():
    assert is_implementation_path("src/components/Header.vue") is True


def test_impl_path_svelte():
    assert is_implementation_path("src/routes/+page.svelte") is True


def test_non_impl_config_files():
    configs = [
        "pyproject.toml", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
        "pom.xml", "build.gradle", "Gemfile", "composer.json",
        "angular.json", "nuxt.config.ts", "svelte.config.js",
        "Dockerfile", "docker-compose.yml", "Makefile",
    ]
    for f in configs:
        assert is_non_implementation_path(f), f"Expected {f} to be non-impl"


# --- Generic scaffold supports all languages ---

def test_generic_create_vue():
    action = {"path": "src/components/Header.vue", "purpose": "Header component"}
    result = _generic_create(action)
    assert "<template>" in result
    assert "<script" in result
    assert "Header" in result


def test_generic_create_svelte():
    action = {"path": "src/routes/Page.svelte", "purpose": "Page component"}
    result = _generic_create(action)
    assert "<script" in result
    assert "Page" in result


def test_generic_create_python():
    action = {"path": "src/handler.py", "purpose": "Request handler"}
    result = _generic_create(action)
    assert '"""' in result


def test_generic_create_go():
    action = {"path": "cmd/server/main.go", "purpose": "Server entry"}
    result = _generic_create(action)
    assert "package" in result


def test_generic_create_rust():
    action = {"path": "src/lib.rs", "purpose": "Library"}
    result = _generic_create(action)
    assert "// Library" in result


def test_generic_create_java():
    action = {"path": "src/App.java", "purpose": "Main app"}
    result = _generic_create(action)
    assert "//" in result


def test_generic_create_csharp():
    action = {"path": "Program.cs", "purpose": "Entry point"}
    result = _generic_create(action)
    assert "//" in result


def test_generic_create_ruby():
    action = {"path": "app.rb", "purpose": "App module"}
    result = _generic_create(action)
    assert "#" in result


def test_generic_create_php():
    action = {"path": "index.php", "purpose": "Entry point"}
    result = _generic_create(action)
    assert "<?php" in result


def test_generic_create_scss():
    action = {"path": "styles/main.scss", "purpose": "Main styles"}
    result = _generic_create(action)
    assert "/*" in result


def test_generic_create_tsx():
    action = {"path": "src/App.tsx", "purpose": "React app"}
    result = _generic_create(action)
    assert "export function" in result
    assert "return" in result
