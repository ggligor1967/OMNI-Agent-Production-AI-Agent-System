# Deployment Prechecks

## Required Before Any Future Deployment

- [ ] Confirm target hosting provider and environment.
- [ ] Confirm selected topology and reverse-proxy / TLS owner.
- [ ] Confirm Python/runtime image version.
- [ ] Confirm approved production environment variables are injected securely.
- [ ] Confirm database target and migration plan.
- [ ] Confirm sandbox backend decision.
- [ ] Confirm observability exporter/backend decision.
- [ ] Confirm rollback artifact and rollback owner.

## Commands

Verification commands only; these commands do not deploy or mutate production resources.

```bash
python tools/check_documentation_consistency.py --root .
```

```bash
python -m compileall agent main.py
```

```bash
pytest tests/ -q
```

```bash
ruff check .
```

```bash
coverage erase
```

```bash
coverage run -m pytest tests/
```

```bash
coverage report --show-missing
```

```bash
bandit -ll -ii -c bandit.yaml main.py agent/core.py agent/auth.py agent/model_registry.py agent/model_router.py agent/multi_model_client.py agent/ollama_client.py agent/memory.py agent/rag.py agent/cache.py agent/pipeline.py agent/tools_registry.py agent/sandbox.py agent/workflow.py agent/streaming.py agent/dashboard.py agent/multimodal.py agent/config.py
```

```bash
pip-audit --cache-dir "$(pwd)/.pip-audit-cache" --progress-spinner off
```
