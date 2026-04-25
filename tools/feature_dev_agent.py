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


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    return text


def call_api(prompt: str, model: str) -> str:
    if model.startswith("claude-"):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("[feature-dev-agent] ANTHROPIC_API_KEY manquant pour le modèle Claude.", file=sys.stderr)
            sys.exit(1)
        payload = {
            "model": model,
            "max_tokens": 16000,
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
            print("[feature-dev-agent] DEEPSEEK_API_KEY manquant pour le modèle DeepSeek.", file=sys.stderr)
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
            print("[feature-dev-agent] OPENAI_API_KEY manquant pour le modèle OpenAI.", file=sys.stderr)
            sys.exit(1)
        payload = {
            "model": model,
            "input": prompt,
            "max_output_tokens": 32000,
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
                print(f"[feature-dev-agent] Impossible de récupérer l'issue #{dispatch_issue_number}: {e}", file=sys.stderr)

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

# Charger les conventions du projet si disponibles
conventions_path = ROOT / ".github" / "copilot-instructions.md"
if conventions_path.exists():
    conventions_content = conventions_path.read_text(encoding="utf-8")
    conventions_section = f"""== CONVENTIONS OBLIGATOIRES DU PROJET ==
(Source : .github/copilot-instructions.md — à respecter STRICTEMENT)

{conventions_content}

"""
else:
    conventions_section = ""

# ─── Passe 1 : identifier les fichiers existants à lire ──────────────────────
# On demande à l'IA quels fichiers elle a besoin de lire avant de coder

PASS1_MAX_CHARS = 4000  # Limite du contenu de chaque fichier existant injecté

scan_prompt = f"""Tu es un agent de développement senior.
Tu vas implémenter une feature dans un projet. Avant de coder, tu dois identifier quels fichiers existants tu as besoin de lire intégralement pour éviter de perdre du code existant.

Issue #{issue_number} — {title}

== SPEC ==
{spec_content}

== STRUCTURE DU REPO ==
{repo_tree}

Réponds UNIQUEMENT avec un objet JSON : {{"files_to_read": ["chemin/relatif/1", "chemin/relatif/2", ...]}}
- Liste les fichiers existants que tu devras MODIFIER (pas les nouveaux fichiers à créer)
- Chemins relatifs à la racine du repo
- Maximum 10 fichiers
- Pas de markdown, uniquement le JSON brut
"""

print("[feature-dev-agent] Passe 1 : identification des fichiers existants à lire...", file=sys.stderr)
scan_raw = strip_code_fence(call_api(scan_prompt, AI_MODEL))

files_to_read = []
try:
    scan_result = json.loads(scan_raw)
    files_to_read = scan_result.get("files_to_read", [])
except json.JSONDecodeError:
    print(f"[feature-dev-agent] Passe 1 : JSON invalide, on continue sans lire de fichiers existants.", file=sys.stderr)

# Lire les fichiers existants depuis le repo cloné (le runner a accès au codebase complet)
existing_files_section = ""
loaded = []
for rel_path in files_to_read[:10]:
    file_path = ROOT / rel_path.lstrip("/")
    if not file_path.exists() or not file_path.is_file():
        print(f"[feature-dev-agent] Fichier non trouvé, ignoré : {rel_path}", file=sys.stderr)
        continue
    content = file_path.read_text(encoding="utf-8", errors="replace")
    # Limiter la taille pour ne pas exploser le contexte
    total_chars = len(content)
    if total_chars > PASS1_MAX_CHARS:
        content = content[:PASS1_MAX_CHARS] + f"\n... [TRONQUÉ à {PASS1_MAX_CHARS} chars — {total_chars - PASS1_MAX_CHARS} chars supplémentaires non affichés]"
    loaded.append(rel_path)
    existing_files_section += f"\n--- FICHIER EXISTANT : {rel_path} ---\n{content}\n"

if loaded:
    print(f"[feature-dev-agent] Passe 1 : {len(loaded)} fichier(s) chargé(s) : {', '.join(loaded)}", file=sys.stderr)
    existing_files_inject = f"""
== FICHIERS EXISTANTS À MODIFIER ==
ATTENTION : Ces fichiers contiennent du code existant que tu DOIS conserver intégralement.
Tu ne supprimes AUCUNE fonction, route, modèle ou import existant — tu AJOUTES seulement ce que la spec demande.
{existing_files_section}
"""
else:
    existing_files_inject = ""

# ─── Passe 2 : génération du code avec contexte complet ──────────────────────

prompt = f"""Tu es un agent de développement senior. Tu reçois une spec technique, la structure du repo et le contenu intégral des fichiers que tu dois modifier.
Tu dois implémenter la feature décrite en générant les fichiers nécessaires.

Issue #{issue_number} — {title}

{conventions_section}{existing_files_inject}== SPEC ==
{spec_content}

== STRUCTURE DU REPO ==
{repo_tree}

Ta réponse doit être un objet JSON valide avec exactement deux clés :
- "files": liste d'objets {{"path": "chemin/relatif/au/repo", "content": "contenu complet du fichier"}}
- "summary": string Markdown décrivant ce qui a été implémenté

Règles strictes :
- Génère uniquement les fichiers nécessaires à l'implémentation (nouveaux ou modifiés)
- Les chemins sont relatifs à la racine du repo
- Le contenu de chaque fichier est complet (pas de placeholders, pas de "...", pas de commentaires "reste du code")
- Pour les fichiers existants fournis ci-dessus : conserve TOUT le code existant, ajoute uniquement ce que la spec demande
- Respecte IMPÉRATIVEMENT les conventions listées ci-dessus
- Pas de markdown autour du JSON, uniquement le JSON brut
"""

print(f"[feature-dev-agent] Passe 2 : génération du code (prompt ~{len(prompt)} chars)...", file=sys.stderr)
raw = strip_code_fence(call_api(prompt, AI_MODEL))

if not raw.strip():
    print("[feature-dev-agent] Aucune sortie texte renvoyée par l'API.", file=sys.stderr)
    sys.exit(1)

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
