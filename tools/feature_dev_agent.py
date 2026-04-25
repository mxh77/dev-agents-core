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

# Charger le contexte global du codebase (généré par init_context_agent.py)
context_doc_path = AI_DIR / "codebase_context.md"
if context_doc_path.exists():
    codebase_context_content = context_doc_path.read_text(encoding="utf-8")
    codebase_context_section = f"""== CONTEXTE GLOBAL DU CODEBASE ==
(Généré par init_context_agent — architecture, modules, types, routes, hooks, schéma DB)

{codebase_context_content}

"""
    print("[feature-dev-agent] Contexte global codebase chargé (.ai/codebase_context.md).", file=sys.stderr)
else:
    codebase_context_section = ""
    print("[feature-dev-agent] ⚠ Pas de .ai/codebase_context.md — contexte global absent (lancer init-context).", file=sys.stderr)

# ─── Passe 1 : identifier les fichiers à lire ────────────────────────────────
# Si .ai/codebase_context.md existe → seulement files_to_modify (contenu complet).
# Sinon → stratégie 2 niveaux : files_to_modify + files_for_context (squelettes).
#
# Budget :
#   Fenêtre LLM ≈ 128k tokens ≈ 512k chars.
#   Spec + conventions + contexte global + repo tree ≈ ~80k chars → il reste ~430k pour les fichiers.
BUDGET_MODIFY_CHARS = 400000   # budget total pour les fichiers à modifier (contenu complet)
BUDGET_CONTEXT_LINES = 100     # nb de lignes max par fichier de contexte (imports + signatures)

if codebase_context_section:
    # Contexte global disponible : on ne demande que les fichiers à modifier
    scan_prompt = f"""Tu es un agent de développement senior.
Tu vas implémenter une feature dans un projet. Le contexte global du codebase te sera fourni séparément.
Identifie uniquement les fichiers existants que tu devras MODIFIER pour implémenter la feature.

Issue #{issue_number} — {title}

== SPEC ==
{spec_content}

== STRUCTURE DU REPO ==
{repo_tree}

Réponds UNIQUEMENT avec un objet JSON :
{{
  "files_to_modify": ["chemin/relatif/fichier_a_modifier_1", ...]
}}

- "files_to_modify" : fichiers existants que tu MODIFIERAS directement (contenu complet fourni)
- N'inclus PAS les fichiers de contexte — ils sont déjà inclus dans le contexte global du codebase
- Chemins relatifs à la racine du repo, sans slash initial
- Pas de nouveaux fichiers (seulement des fichiers existants à modifier)
- Pas de markdown, uniquement le JSON brut
"""
else:
    # Pas de contexte global : stratégie 2 niveaux
    scan_prompt = f"""Tu es un agent de développement senior.
Tu vas implémenter une feature dans un projet. Avant de coder, identifie les fichiers dont tu as besoin.

Issue #{issue_number} — {title}

== SPEC ==
{spec_content}

== STRUCTURE DU REPO ==
{repo_tree}

Réponds UNIQUEMENT avec un objet JSON ayant deux clés :
{{
  "files_to_modify": ["chemin/relatif/fichier_a_modifier_1", ...],
  "files_for_context": ["chemin/relatif/fichier_contexte_1", ...]
}}

- "files_to_modify" : fichiers existants que tu devras MODIFIER (contenu complet fourni)
- "files_for_context" : autres fichiers que tu consultes pour comprendre les types, les routes déjà existantes, les hooks, etc. (squelette fourni : imports + signatures)
- Chemins relatifs à la racine du repo, sans slash initial
- Pas de nouveaux fichiers dans ces listes (seulement des fichiers existants)
- Pas de markdown, uniquement le JSON brut
"""

print("[feature-dev-agent] Passe 1 : identification des fichiers à lire...", file=sys.stderr)
scan_raw = strip_code_fence(call_api(scan_prompt, AI_MODEL))

files_to_modify = []
files_for_context = []
try:
    scan_result = json.loads(scan_raw)
    files_to_modify = scan_result.get("files_to_modify", [])
    files_for_context = [] if codebase_context_section else scan_result.get("files_for_context", [])
    # Fallback : ancien format files_to_read
    if not files_to_modify and not files_for_context:
        files_to_modify = scan_result.get("files_to_read", [])
    print(f"[feature-dev-agent]   À modifier  : {files_to_modify}", file=sys.stderr)
    if files_for_context:
        print(f"[feature-dev-agent]   Contexte    : {files_for_context}", file=sys.stderr)
except json.JSONDecodeError:
    print("[feature-dev-agent] Passe 1 : JSON invalide, on continue sans lire de fichiers existants.", file=sys.stderr)


def read_file_full(rel_path: str, budget: int) -> tuple[str, int]:
    """Lit un fichier en entier, dans la limite du budget. Retourne (contenu, chars_utilisés)."""
    file_path = ROOT / rel_path.lstrip("/")
    if not file_path.exists() or not file_path.is_file():
        print(f"[feature-dev-agent]   ⚠ Fichier non trouvé, ignoré : {rel_path}", file=sys.stderr)
        return "", 0
    content = file_path.read_text(encoding="utf-8", errors="replace")
    total_chars = len(content)
    if total_chars > budget:
        content = content[:budget] + f"\n... [TRONQUÉ — {total_chars - budget} chars supplémentaires non affichés, budget épuisé]"
        print(f"[feature-dev-agent]   ✓ {rel_path} ({total_chars} chars, tronqué à {budget})", file=sys.stderr)
        return content, budget
    print(f"[feature-dev-agent]   ✓ {rel_path} ({total_chars} chars)", file=sys.stderr)
    return content, total_chars


def read_file_skeleton(rel_path: str, max_lines: int) -> str:
    """Lit les N premières lignes d'un fichier (imports, exports, signatures)."""
    file_path = ROOT / rel_path.lstrip("/")
    if not file_path.exists() or not file_path.is_file():
        print(f"[feature-dev-agent]   ⚠ Fichier contexte non trouvé, ignoré : {rel_path}", file=sys.stderr)
        return ""
    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    total_lines = len(lines)
    skeleton = "\n".join(lines[:max_lines])
    suffix = f"\n... [{total_lines - max_lines} lignes supplémentaires non affichées]" if total_lines > max_lines else ""
    print(f"[feature-dev-agent]   ~ {rel_path} (squelette {min(max_lines, total_lines)}/{total_lines} lignes)", file=sys.stderr)
    return skeleton + suffix


# Lecture des fichiers à modifier (contenu complet)
existing_files_section = ""
loaded = []
budget_remaining = BUDGET_MODIFY_CHARS

for rel_path in files_to_modify:
    if budget_remaining <= 0:
        print(f"[feature-dev-agent]   ⚠ Budget épuisé, fichier ignoré : {rel_path}", file=sys.stderr)
        continue
    content, used = read_file_full(rel_path, budget_remaining)
    if content:
        loaded.append(rel_path)
        existing_files_section += f"\n--- FICHIER À MODIFIER (contenu complet) : {rel_path} ---\n{content}\n"
        budget_remaining -= used

# Lecture des fichiers de contexte (squelette)
context_section = ""
for rel_path in files_for_context:
    # Éviter les doublons avec files_to_modify
    if rel_path in files_to_modify:
        continue
    skeleton = read_file_skeleton(rel_path, BUDGET_CONTEXT_LINES)
    if skeleton:
        context_section += f"\n--- FICHIER CONTEXTE (squelette {BUDGET_CONTEXT_LINES} lignes) : {rel_path} ---\n{skeleton}\n"

if loaded:
    print(f"[feature-dev-agent] Passe 1 : {len(loaded)} fichier(s) complet(s) + {len(files_for_context)} contexte(s)", file=sys.stderr)
    existing_files_inject = f"""
== FICHIERS À MODIFIER (contenu complet — CONSERVER TOUT LE CODE EXISTANT) ==
RÈGLE ABSOLUE : Pour chaque fichier ci-dessous, tu dois conserver INTÉGRALEMENT le code existant.
Tu ne supprimes AUCUNE fonction, route, modèle Prisma, import ou export existant.
Tu AJOUTES uniquement ce que la spec demande, sans toucher au reste.
{existing_files_section}
"""
    if context_section:
        existing_files_inject += f"""
== FICHIERS DE CONTEXTE (squelette — pour comprendre les types, API, imports) ==
Ces fichiers ne seront pas modifiés directement, ils te donnent le contexte global du codebase.
{context_section}
"""
else:
    existing_files_inject = ""
    if context_section:
        existing_files_inject = f"""
== FICHIERS DE CONTEXTE (squelette — pour comprendre les types, API, imports) ==
{context_section}
"""

# ─── Passe 2 : génération du code avec contexte complet ──────────────────────

prompt = f"""Tu es un agent de développement senior. Tu reçois une spec technique, la structure du repo et le contenu intégral des fichiers que tu dois modifier.
Tu dois implémenter la feature décrite en générant les fichiers nécessaires.

Issue #{issue_number} — {title}

{conventions_section}{codebase_context_section}{existing_files_inject}== SPEC ==
{spec_content}

== STRUCTURE DU REPO ==
{repo_tree}

== FORMAT DE RÉPONSE OBLIGATOIRE ==
Réponds avec ce format exact (délimiteurs fixes, PAS de JSON) :

<<<FILE:chemin/relatif/fichier1>>>
contenu complet du fichier 1
<<<END>>>
<<<FILE:chemin/relatif/fichier2>>>
contenu complet du fichier 2
<<<END>>>
<<<SUMMARY>>>
Résumé Markdown de ce qui a été implémenté
<<<END>>>

Règles strictes :
- Génère uniquement les fichiers nécessaires à l'implémentation (nouveaux ou modifiés)
- Les chemins sont relatifs à la racine du repo
- Le contenu de chaque fichier est complet (pas de placeholders, pas de "...", pas de commentaires "reste du code")
- Pour les fichiers existants fournis ci-dessus : conserve TOUT le code existant, ajoute uniquement ce que la spec demande
- Respecte IMPÉRATIVEMENT les conventions listées ci-dessus
- Aucun texte avant le premier <<<FILE: ou après le dernier <<<END>>>
"""

print(f"[feature-dev-agent] Passe 2 : génération du code (prompt ~{len(prompt)} chars)...", file=sys.stderr)
raw = call_api(prompt, AI_MODEL).strip()

if not raw:
    print("[feature-dev-agent] Aucune sortie texte renvoyée par l'API.", file=sys.stderr)
    sys.exit(1)

# ─── Parsing du format délimiteur ────────────────────────────────────────────

files = []
summary = "Implémentation générée par feature-dev-agent."

file_pattern = re.compile(r'<<<FILE:([^>]+)>>>\n(.*?)<<<END>>>', re.DOTALL)
summary_pattern = re.compile(r'<<<SUMMARY>>>\n(.*?)<<<END>>>', re.DOTALL)

for m in file_pattern.finditer(raw):
    path = m.group(1).strip()
    content = m.group(2)
    # Supprimer un éventuel newline final ajouté par le modèle avant le délimiteur
    if content.endswith("\n"):
        content = content[:-1]
    files.append({"path": path, "content": content})

m_summary = summary_pattern.search(raw)
if m_summary:
    summary = m_summary.group(1).strip()

if not files:
    # Fallback : tenter un parsing JSON si le modèle a ignoré les consignes de format
    print("[feature-dev-agent] Format délimiteur non trouvé, tentative fallback JSON...", file=sys.stderr)
    raw_json = strip_code_fence(raw)
    try:
        result = json.loads(raw_json)
        files = result.get("files", [])
        summary = result.get("summary", summary)
    except json.JSONDecodeError as e:
        print(f"[feature-dev-agent] Échec parsing JSON fallback : {e}", file=sys.stderr)
        print(raw[:800], file=sys.stderr)
        sys.exit(1)

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
