from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
import sys
import threading
from collections.abc import Callable
from types import ModuleType
from typing import Any


_POST_IMPORT_LOCK = threading.RLock()
_POST_IMPORT_HOOKS: dict[str, "PostImportHook"] = {}


class _CallbackLoader(importlib.abc.Loader):
    def __init__(self, loader: importlib.abc.Loader, hook: "PostImportHook"):
        self._loader = loader
        self._hook = hook

    def create_module(self, spec):
        creator = getattr(self._loader, "create_module", None)
        return creator(spec) if creator is not None else None

    def exec_module(self, module: ModuleType) -> None:
        self._loader.exec_module(module)
        self._hook.complete(module)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


class PostImportHook(importlib.abc.MetaPathFinder):
    def __init__(self, module_name: str, callback: Callable[[ModuleType], Any]):
        self.module_name = module_name
        self._callback = callback
        self._active = True

    def find_spec(self, fullname, path=None, target=None):
        if not self._active or fullname != self.module_name:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _CallbackLoader(spec.loader, self)
        return spec

    def complete(self, module: ModuleType) -> None:
        try:
            self._callback(module)
        except Exception:
            if os.environ.get("UDFJIT_DIAGNOSTICS", "off") != "off":
                raise
            # Bootstrap is fail-open by contract. The adapter emits details once it
            # is importable; a bootstrap failure must not fail `import daft`.
            self.uninstall()
        else:
            self.uninstall()

    def uninstall(self) -> None:
        with _POST_IMPORT_LOCK:
            if self in sys.meta_path:
                sys.meta_path.remove(self)
            if _POST_IMPORT_HOOKS.get(self.module_name) is self:
                _POST_IMPORT_HOOKS.pop(self.module_name, None)
            self._active = False


def install_post_import_hook(
    module_name: str, callback: Callable[[ModuleType], Any]
) -> PostImportHook:
    """Install one exact-name callback without importing the target eagerly."""

    with _POST_IMPORT_LOCK:
        existing = _POST_IMPORT_HOOKS.get(module_name)
        if existing is not None:
            return existing
        hook = PostImportHook(module_name, callback)
        module = sys.modules.get(module_name)
        if module is not None:
            hook._active = False
        else:
            _POST_IMPORT_HOOKS[module_name] = hook
            sys.meta_path.insert(0, hook)
            return hook
    hook.complete(module)
    return hook


def bootstrap_from_environment() -> PostImportHook | None:
    """Lightweight `.pth` entry: never import Daft/Ray in off mode or eagerly."""

    if os.environ.get("UDFJIT_MODE", "off") == "off":
        return None

    def install(module: ModuleType) -> None:
        from python_udf_jit.integration.daft_ray.control import (
            install_default_daft_hooks,
        )

        result = install_default_daft_hooks(module)
        if (
            os.environ.get("UDFJIT_DIAGNOSTICS", "off") != "off"
            and result.status.value not in {"installed", "already_installed"}
        ):
            raise RuntimeError(
                f"diagnostic_bootstrap_failed:{result.reason}"
            )

    return install_post_import_hook("daft", install)
