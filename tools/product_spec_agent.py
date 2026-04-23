#!/usr/bin/env python3
import json
import os
import pathlib
import re
import sys
import urllib.request

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
GITHUB_EVENT_PATH = os.environ["GITHUB_EVENT_PATH"]

ROOT = pathlib.Path.cwd()
OUT_DIR = ROOT / ".ai" / "issues"

def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:60]

with open(GITHUB_EVENT_PATH, "r", encoding="utf-8") as f:
    event = json.load(f)

issue = event.get("issue", {})
issue_number = issue.get("number", "manual")
title = issue.get("title", "Demande sans titre")
body = issue.get("body", "")

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
    "model": "gpt-4.1",
    "input": prompt
}

req = urllib.request.Request(
    "https://api.openai.com/v1/responses",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    },
    method="POST",
)

with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode("utf-8"))

text = ""
for item in data.get("output", []):
    for content in item.get("content", []):
        if content.get("type") == "output_text":
            text += content.get("text", "")

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
