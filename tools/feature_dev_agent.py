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
AI_DIR = ROOT / ".ai"

IGNORE_DIRS = {".git", ".ai", "node_modules", "__pycache__", "dist", "build", "dev-agents-core", ".venv", "venv"}


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:60]


def get_repo_tree(root: pathlib.Path, max_files: int = 300) -> str:
    lines = []
    for p in sorted(root.rglob("*")):
        if any(part in IGNORE_DIRS or part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.is_file():
            lines.append(str(p.relative_to(root)))
            if len(lines) >= max_files:
                lines.append("... (truncated)")
                break
    return "\n".join(lines)


with open(GITHUB_EVENT_PATH, "r", encoding="utf-8") as f:
    event = json.load(f)

issue = event.get("issue", {})
issue_number = issue.get("number", "manual")
title = issue.get("title", "Feature sans titre")

slug = slugify(title)

# Locate spec — try canonical path first, then scan
spec_path = AI_DIR / "issues" / f"{issue_number}-{slug}" / "spec.md"
if not spec_path.exists():
    candidates = sorted((AI_DIR / "issues").glob(f"{issue_number}-*/spec.md"))
    if not candidates:
        print(f"[feature-dev-agent] Aucune spec trouvée pour l'issue #{issue_number}.", file=sys.stderr)
        print("Assurez-vous que product-spec-agent a été exécuté en premier.", file=sys.stderr)
        sys.exit(1)
    spec_path = candidates[0]

spec_content = spec_path.read_text(encoding="utf-8")
repo_tree = get_repo_tree(ROOT)

prompt = f"""Tu es un agent de développement senior. Tu reçois une spec technique et la structure du repo.
Tu dois implémenter la feature décrite, en générant les fichiers nécessaires.

Issue #{issue_number} — {title}

== SPEC ==
{spec_content}

== STRUCTURE DU REPO ==
{repo_tree}

Ta réponse doit être un objet JSON valide avec exactement deux clés :
- "files": liste d'objets {{"path": "chemin/relatif/au/repo", "content": "contenu complet du fichier"}}
- "summary": string Markdown décrivant ce qui a été implémenté

Règles strictes :
- Génère uniquement les fichiers nécessaires à l'implémentation (nouveaux ou modifiés)
- Les chemins sont relatifs à la racine du repo
- Le contenu de chaque fichier est complet (pas de placeholders, pas de "...")
- Respecte la structure et les conventions du repo existant
- Pas de markdown autour du JSON, uniquement le JSON brut
"""

payload = {
    "model": "gpt-4.1",
    "input": prompt,
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

raw = ""
for item in data.get("output", []):
    for content in item.get("content", []):
        if content.get("type") == "output_text":
            raw += content.get("text", "")

if not raw.strip():
    print("[feature-dev-agent] Aucune sortie texte renvoyée par l'API OpenAI.", file=sys.stderr)
    sys.exit(1)

# Strip potential markdown code fence
raw = raw.strip()
if raw.startswith("```"):
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw.strip())

try:
    result = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"[feature-dev-agent] Réponse JSON invalide : {e}", file=sys.stderr)
    print(raw[:500], file=sys.stderr)
    sys.exit(1)

files = result.get("files", [])
summary = result.get("summary", "Implémentation générée par feature-dev-agent.")

if not files:
    print("[feature-dev-agent] Aucun fichier généré.", file=sys.stderr)
    sys.exit(1)

generated_paths = []
for file_def in files:
    rel_path = file_def.get("path", "").lstrip("/")
    file_content = file_def.get("content", "")
    if not rel_path:
        continue
    target = ROOT / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(file_content, encoding="utf-8")
    generated_paths.append(rel_path)
    print(f"  [+] {rel_path}")

print(f"\n[feature-dev-agent] {len(generated_paths)} fichier(s) généré(s).")

# Write PR body
files_list = "\n".join(f"- `{p}`" for p in generated_paths)
pr_body = f"""## Feature Dev Agent

Issue source: #{issue_number} — {title}

### Fichiers générés / modifiés
{files_list}

### Résumé de l'implémentation
{summary}

### Prochaine étape
Revue humaine du code avant merge.
"""
(AI_DIR / "pr_body.md").write_text(pr_body, encoding="utf-8")

branch_name = f"feature-dev/issue-{issue_number}-{slugify(title)[:30]}"
(AI_DIR / "branch_name.txt").write_text(branch_name, encoding="utf-8")
print(branch_name)
