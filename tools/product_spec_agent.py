#!/usr/bin/env python3
import json
import os
import pathlib
import re
import sys
import urllib.request

GITHUB_EVENT_PATH = os.environ["GITHUB_EVENT_PATH"]
AI_MODEL = os.environ.get("AI_MODEL", "gpt-4.1")

ROOT = pathlib.Path.cwd()
OUT_DIR = ROOT / ".ai" / "issues"

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:60]

with open(GITHUB_EVENT_PATH, "r", encoding="utf-8") as f:
    event = json.load(f)

issue = event.get("issue", {})

# Support workflow_dispatch: read issue_number from inputs, then fetch from GitHub API
if not issue:
    inputs = event.get("inputs") or event.get("client_payload", {})
    dispatch_issue_number = inputs.get("issue_number") if inputs else None
    if dispatch_issue_number:
        github_token = os.environ.get("GITHUB_TOKEN", "")
        github_repository = os.environ.get("GITHUB_REPOSITORY", "")
        if github_token and github_repository:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{github_repository}/issues/{dispatch_issue_number}",
                headers={
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            try:
                with urllib.request.urlopen(req) as resp:
                    issue = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                print(f"[product-spec-agent] Impossible de récupérer l'issue #{dispatch_issue_number}: {e}", file=sys.stderr)

issue_number = issue.get("number", "manual")
title = issue.get("title", "Demande sans titre")
body = issue.get("body", "") or ""

prompt = f"""
Tu es un agent de cadrage produit et technique.

Projet: dépôt GitHub
Issue #{issue_number}
Titre: {title}

Description utilisateur:
{body}

Retourne uniquement du Markdown structuré avec les sections suivantes :
# Résumé
# Objectif métier
# Périmètre inclus
# Hors périmètre
# Impacts techniques
# Proposition d'implémentation
# Risques
# Tests à prévoir
# Checklist de validation

Sois concret, orienté exécution, sans blabla.
"""

payload = {
    "model": AI_MODEL,
    "input": prompt
}

def call_api(prompt: str, model: str) -> str:
    if model.startswith("claude-"):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("ANTHROPIC_API_KEY manquant pour le modèle Claude.", file=sys.stderr)
            sys.exit(1)
        payload = {
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
    elif "deepseek" in model:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            print("DEEPSEEK_API_KEY manquant pour le modèle DeepSeek.", file=sys.stderr)
            sys.exit(1)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            print("OPENAI_API_KEY manquant pour le modèle OpenAI.", file=sys.stderr)
            sys.exit(1)
        payload = {
            "model": model,
            "input": prompt,
        }
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(
            content.get("text", "")
            for item in data.get("output", [])
            for content in item.get("content", [])
            if content.get("type") == "output_text"
        )

text = call_api(prompt, AI_MODEL)

if not text.strip():
    print("Aucune sortie texte renvoyée par l'API OpenAI.", file=sys.stderr)
    sys.exit(1)

issue_dir = OUT_DIR / f"{issue_number}-{slugify(title)}"
issue_dir.mkdir(parents=True, exist_ok=True)

spec_path = issue_dir / "spec.md"
spec_path.write_text(text.strip() + "\n", encoding="utf-8")

pr_body = f"""## Product Spec Agent

Issue source: #{issue_number}

### Contenu généré
- spec structurée
- plan d'implémentation
- checklist de validation

### Fichier ajouté
- `{spec_path.relative_to(ROOT)}`

### Prochaine étape
Validation humaine du cadrage avant passage à un agent de dev auto.
"""
(ROOT / ".ai" / "pr_body.md").write_text(pr_body, encoding="utf-8")

branch_name = f"product-spec-agent/issue-{issue_number}-{slugify(title)[:30]}"
(ROOT / ".ai" / "branch_name.txt").write_text(branch_name, encoding="utf-8")
print(branch_name)
