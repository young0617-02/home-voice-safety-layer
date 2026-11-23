# mypy 基线

> 2026-11-03 更新：**0 errors**（此前基线为 33 errors / 12 files）。
> 本文件记录环境、修复项与复跑命令；mypy 已于 2026-11-04 接入 CI 阻断门禁（`ci.yml` 的 `Types` 步骤），本地等价命令 0 errors，GitHub CI 实跑尚未观察到。

## 环境与命令

| 项 | 值 |
|---|---|
| mypy 版本 | `mypy 2.4.1 (compiled: yes)` |
| Python | 3.12.8（项目 `requires-python >= 3.11`） |
| 命令 | `.venv/Scripts/python.exe -m mypy hvsl pipeline custom_components` |
| 退出码 | `0` |
| 配置 | `[tool.mypy] exclude = ["^custom_components/home_voice_safety/vendor/"]` |

> 用 `.venv` 解释器而不是 `uv run mypy`：`uv run` 会同步默认依赖，可能把 `asr` extras（faster-whisper/av/opencc）卸载。

## 结果

```text
Success: no issues found in 37 source files
```

## 修复项

### 1. 可选依赖/宿主库缺 stub（21 条 → overrides）

`pyproject.toml` 新增：

```toml
[[tool.mypy.overrides]]
module = ["homeassistant.*", "faster_whisper.*", "fastapi.*", "uvicorn.*", "opencc.*"]
ignore_missing_imports = true
```

这些是运行时可选依赖或 HA 宿主库（无 py.typed），不影响本项目类型契约。

### 2. 业务类型错误（12 条 → 已修）

| 文件 | 错误 | 修法 |
|---|---|---|
| `pipeline/_asr_worker.py:138` | 生成器元素 `float \| Any \| str` | `str(seg["text"])` |
| `pipeline/_asr_worker.py:158` | `TextIO` 无 `reconfigure` | `getattr(sys.stdout, "reconfigure", None)` 后调用 |
| `hvsl/config.py:100` | `load_dotenv` 恒真判断 | `if callable(load_dotenv)` |
| `hvsl/config.py:114-116` | `str \| None` 传给 `str \| Path` | `get(...) or s.xxx` |
| `hvsl/idempotency.py:103` | 返回 `object` 但声明 `GuardResult` | `cast(GuardResult, ...)` |
| `hvsl/engine.py:299/482` | `_first_risk` 返回 `ResolvedStep \| None` | `assert step is not None` |
| `hvsl/engine.py:656` | `str` 赋给 `Literal[...]` | `cast(Any, state)` |
| `custom_components/.../__init__.py:165` | `engine` 可能 `None` | `if engine is not None:` |
| `custom_components/.../coordinator.py:199/214` | `self.engine` 可能 `None` | `assert self.engine is not None` |
| `custom_components/.../config_flow.py:293` | HA `ConfigFlow` 子类 `domain` 关键字（缺 stub） | `# type: ignore[call-arg]` |

> `engine` 在 `async_setup()` 后必然非 None；`assert` 只是把这一不变量写给 mypy，运行期不会改变行为。

## 未做

- mypy **已接入 CI gate**（2026-11-04：`ci.yml` 增加 `Types` 步骤 `uv run mypy hvsl pipeline custom_components`，与 ruff/pytest 同一个 job）；本地 `.venv` 等价命令 0 errors，**GitHub CI 实跑尚未观察**。
- 未安装 `pytest-homeassistant-custom-component` / HA 官方类型包；HA 模块走 overrides。
- 3.11 仍未本地实跑（CI matrix 覆盖）。

## 复跑

```bash
.venv/Scripts/python.exe -m mypy hvsl pipeline custom_components
```