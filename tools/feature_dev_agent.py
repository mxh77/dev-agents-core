#!/usr/bin/env python3
"""
feature_dev_agent.py — Agent de développement avec boucle outil (tool calling loop)

Architecture :
  1. Chargement : spec, conventions, codebase_context.md, repo_tree
  2. Boucle outil : le LLM appelle read_file / list_directory autant que nécessaire
  3. Génération : le LLM produit des PATCHES (old/new) pour les fichiers existants
                  et des FILE complets pour les nouveaux fichiers
  4. Application : patches appliqués chirurgicalement, nouveaux fichiers écrits

Outils disponibles pour le LLM :
  - read_file(path)          : lit un fichier existant en entier
  - list_directory(path)     : liste les fichiers d'un dossier

Format de sortie du LLM :
  <<<FILE:path>>>            : nouveau fichier (contenu complet)
  <<<PATCH:path>>>           : patch pour fichier existant
  <<<OLD>>>                  : début du bloc à remplacer (exact)
  <<<NEW>>>                  : début du nouveau bloc
  <<<END>>>                  : fin de bloc
  <<<SUMMARY>>>              : résumé Markdown
"""
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

MAX_TOOL_ROUNDS = 30   # limite de tours pour éviter les boucles infinies


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:60]


def get_repo_tree(root: pathlib.Path, max_files: int = 600) -> str:
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


# ─── Définition des outils ───────────────────────────────────────────────────

TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lit le contenu complet d'un fichier du repo. Utilise cet outil pour lire tout fichier dont tu as besoin avant de générer du code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Chemin relatif du fichier depuis la racine du repo (ex: backend/src/modules/horses/horses.service.ts)"
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "Liste les fichiers (non récursif) d'un dossier du repo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Chemin relatif du dossier depuis la racine du repo (ex: backend/src/modules/horses)"
                    }
                },
                "required": ["path"]
            }
        }
    }
]

# Format Anthropic (tool_use)
TOOLS_ANTHROPIC = [
    {
        "name": "read_file",
        "description": "Lit le contenu complet d'un fichier du repo. Utilise cet outil pour lire tout fichier dont tu as besoin avant de générer du code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Chemin relatif du fichier depuis la racine du repo"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "list_directory",
        "description": "Liste les fichiers (non récursif) d'un dossier du repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Chemin relatif du dossier depuis la racine du repo"
                }
            },
            "required": ["path"]
        }
    }
]


def execute_tool(name: str, args: dict) -> str:
    """Exécute un outil appelé par le LLM et retourne le résultat sous forme de string."""
    if name == "read_file":
        rel_path = args.get("path", "").lstrip("/")
        file_path = ROOT / rel_path
        if not file_path.exists() or not file_path.is_file():
            return f"ERREUR : fichier '{rel_path}' introuvable dans le repo."
        content = file_path.read_text(encoding="utf-8", errors="replace")
        print(f"[feature-dev-agent]   [tool] read_file({rel_path}) — {len(content)} chars", file=sys.stderr)
        return content
    elif name == "list_directory":
        rel_path = args.get("path", "").lstrip("/")
        dir_path = ROOT / rel_path
        if not dir_path.exists() or not dir_path.is_dir():
            return f"ERREUR : dossier '{rel_path}' introuvable dans le repo."
        entries = sorted(dir_path.iterdir())
        lines = []
        for e in entries:
            suffix = "/" if e.is_dir() else ""
            lines.append(f"{e.name}{suffix}")
        result = "\n".join(lines)
        print(f"[feature-dev-agent]   [tool] list_directory({rel_path}) — {len(entries)} entrées", file=sys.stderr)
        return result
    else:
        return f"ERREUR : outil inconnu '{name}'."


# ─── call_api_agentic : boucle outil multi-tour ──────────────────────────────

def call_api_agentic(system_prompt: str, user_prompt: str, model: str) -> str:
    """
    Appelle le LLM en mode agentique avec support des outils (read_file, list_directory).
    Boucle jusqu'à ce que le LLM arrête d'appeler des outils (finish_reason=stop/end_turn).
    Retourne le texte final du LLM.
    """
    import http.client
    import time

    rounds = 0

    if model.startswith("claude-"):
        # ── Anthropic ────────────────────────────────────────────────────────
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("[feature-dev-agent] ANTHROPIC_API_KEY manquant.", file=sys.stderr)
            sys.exit(1)

        messages = [{"role": "user", "content": user_prompt}]

        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            payload = {
                "model": model,
                "max_tokens": 32000,
                "system": system_prompt,
                "tools": TOOLS_ANTHROPIC,
                "messages": messages,
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
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    break
                except (http.client.IncompleteRead, TimeoutError) as e:
                    if attempt < 2:
                        print(f"[feature-dev-agent] Retry ({e})...", file=sys.stderr)
                        time.sleep(5)
                    else:
                        raise

            stop_reason = data.get("stop_reason", "end_turn")
            content_blocks = data.get("content", [])

            # Ajouter la réponse de l'assistant à l'historique
            messages.append({"role": "assistant", "content": content_blocks})

            if stop_reason != "tool_use":
                # Plus d'appels d'outils → retourner le texte
                return "".join(
                    b.get("text", "")
                    for b in content_blocks
                    if b.get("type") == "text"
                )

            # Exécuter les outils demandés
            tool_results = []
            for block in content_blocks:
                if block.get("type") == "tool_use":
                    result = execute_tool(block["name"], block.get("input", {}))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": result,
                    })

            messages.append({"role": "user", "content": tool_results})
            print(f"[feature-dev-agent] Tour {rounds} : {len(tool_results)} outil(s) exécuté(s)", file=sys.stderr)

        print(f"[feature-dev-agent] ⚠ Limite de {MAX_TOOL_ROUNDS} tours atteinte.", file=sys.stderr)
        return ""

    else:
        # ── OpenAI / DeepSeek (format chat/completions) ──────────────────────
        if "deepseek" in model:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            base_url = "https://api.deepseek.com/v1/chat/completions"
            extra_payload = {}  # thinking incompatible avec tool calling
        else:
            api_key = os.environ.get("OPENAI_API_KEY", "")
            base_url = "https://api.openai.com/v1/chat/completions"
            extra_payload = {}

        if not api_key:
            print(f"[feature-dev-agent] Clé API manquante pour {model}.", file=sys.stderr)
            sys.exit(1)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        while rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            payload = {
                "model": model,
                "messages": messages,
                "tools": TOOLS_OPENAI,
                "tool_choice": "auto",
                "max_tokens": 32000,
                **extra_payload,
            }
            req = urllib.request.Request(
                base_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    break
                except (http.client.IncompleteRead, TimeoutError) as e:
                    if attempt < 2:
                        print(f"[feature-dev-agent] Retry ({e})...", file=sys.stderr)
                        time.sleep(5)
                    else:
                        raise

            choice = data["choices"][0]
            message = choice["message"]
            finish_reason = choice.get("finish_reason", "stop")

            # Ajouter la réponse de l'assistant à l'historique
            messages.append(message)

            if finish_reason != "tool_calls":
                content = message.get("content") or ""
                # Vérifier que le LLM a utilisé les bons délimiteurs
                if "<<<PATCH:" in content or "<<<FILE:" in content:
                    return content
                # Sinon : rappel de format (1 seul retry)
                print("[feature-dev-agent] ⚠ Réponse sans délimiteurs — rappel du format...", file=sys.stderr)
                REMINDER = (
                    "Ta réponse doit OBLIGATOIREMENT utiliser les délimiteurs suivants, sans aucun texte libre.\n"
                    "Pour chaque fichier EXISTANT à modifier :\n"
                    "<<<PATCH:chemin/relatif/fichier>>>\n"
                    "<<<OLD>>>\n"
                    "bloc exact à remplacer (copié mot pour mot)\n"
                    "<<<NEW>>>\n"
                    "nouveau bloc\n"
                    "<<<END>>>\n"
                    "Pour chaque NOUVEAU fichier :\n"
                    "<<<FILE:chemin/relatif/fichier>>>\n"
                    "contenu complet\n"
                    "<<<END>>>\n"
                    "<<<SUMMARY>>>\n"
                    "résumé\n"
                    "<<<END>>>\n"
                    "Génère MAINTENANT les patches/fichiers en utilisant exactement ce format."
                )
                messages.append({"role": "user", "content": REMINDER})
                # Appel de rappel sans tools pour forcer la sortie finale
                payload_retry = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": 32000,
                }
                req_retry = urllib.request.Request(
                    base_url,
                    data=json.dumps(payload_retry).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req_retry, timeout=180) as resp_retry:
                    data_retry = json.loads(resp_retry.read().decode("utf-8"))
                return data_retry["choices"][0]["message"].get("content") or ""

            # Exécuter les outils demandés
            tool_calls = message.get("tool_calls", [])
            tool_results_messages = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                result = execute_tool(fn.get("name", ""), args)
                tool_results_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result,
                })

            messages.extend(tool_results_messages)
            print(f"[feature-dev-agent] Tour {rounds} : {len(tool_calls)} outil(s) exécuté(s)", file=sys.stderr)

        print(f"[feature-dev-agent] ⚠ Limite de {MAX_TOOL_ROUNDS} tours atteinte.", file=sys.stderr)
        return ""


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
(Architecture, modules, routes, hooks, types, schéma DB — généré par init_context_agent)

{codebase_context_content}

"""
    print("[feature-dev-agent] Contexte global codebase chargé (.ai/codebase_context.md).", file=sys.stderr)
else:
    codebase_context_section = ""
    print("[feature-dev-agent] ⚠ Pas de .ai/codebase_context.md (lancer init-context).", file=sys.stderr)

# ─── Prompt système ───────────────────────────────────────────────────────────

system_prompt = f"""Tu es un agent de développement senior qui implémente des features dans un codebase existant.

Tu as accès à deux outils :
- `read_file(path)` : lit le contenu complet d'un fichier
- `list_directory(path)` : liste les fichiers d'un dossier

WORKFLOW OBLIGATOIRE :
1. Commence par lire les fichiers que tu vas modifier (utilise read_file)
2. Si tu as un doute sur la structure d'un dossier, utilise list_directory
3. Une fois que tu as lu tous les fichiers nécessaires, génère ta réponse

{conventions_section}{codebase_context_section}
FORMAT DE RÉPONSE FINAL :

Pour les fichiers EXISTANTS modifiés → utilise des PATCHES (chirurgicaux, jamais le fichier complet) :
<<<PATCH:chemin/relatif/fichier>>>
<<<OLD>>>
bloc de code EXACT à remplacer (copié mot pour mot depuis le fichier lu)
inclure 3-5 lignes de contexte avant et après la modification
<<<NEW>>>
nouveau bloc de code (remplace exactement le bloc OLD)
<<<END>>>

Pour les NOUVEAUX fichiers (n'existant pas encore) → contenu complet :
<<<FILE:chemin/relatif/nouveau_fichier>>>
contenu complet du nouveau fichier
<<<END>>>

Résumé :
<<<SUMMARY>>>
Résumé Markdown de l'implémentation
<<<END>>>

RÈGLES CRITIQUES :
- Pour un fichier existant : TOUJOURS utiliser <<<PATCH>>> (jamais <<<FILE>>>)
  Le bloc <<<OLD>>> doit être une copie EXACTE du fichier (espaces, virgules, tout)
- Pour un nouveau fichier : utiliser <<<FILE>>>
- Plusieurs patches possibles pour un même fichier
- Aucun texte hors des délimiteurs dans ta réponse finale
"""

FORMAT_REMINDER = """
== FORMAT DE SORTIE OBLIGATOIRE ==
Quand tu as terminé de lire les fichiers, ta réponse finale DOIT utiliser UNIQUEMENT ces délimiteurs :

Pour chaque fichier existant à modifier :
<<<PATCH:chemin/relatif/fichier>>>
<<<OLD>>>
bloc exact à remplacer (copié mot pour mot depuis le fichier lu)
<<<NEW>>>
nouveau bloc
<<<END>>>

Pour chaque nouveau fichier :
<<<FILE:chemin/relatif/fichier>>>
contenu complet
<<<END>>>

<<<SUMMARY>>>
Résumé Markdown
<<<END>>>

INTERDIT : écrire du texte libre, des titres Markdown, des listes hors délimiteurs.
Ta réponse commence directement par <<<PATCH: ou <<<FILE:
"""

user_prompt = f"""Implémente la feature suivante.

Issue #{issue_number} — {title}

== SPEC ==
{spec_content}

== STRUCTURE DU REPO ==
{repo_tree}
{FORMAT_REMINDER}
Commence par lire les fichiers que tu vas modifier avec read_file(), puis génère les patches/fichiers en suivant EXACTEMENT le format ci-dessus.
"""

# ─── Appel agentique (boucle outil) ──────────────────────────────────────────

print(f"[feature-dev-agent] Démarrage boucle outil ({AI_MODEL}, max {MAX_TOOL_ROUNDS} tours)...", file=sys.stderr)
raw = call_api_agentic(system_prompt, user_prompt, AI_MODEL).strip()

if not raw:
    print("[feature-dev-agent] Aucune sortie renvoyée par l'API.", file=sys.stderr)
    sys.exit(1)

# ─── Parsing patches + nouveaux fichiers ─────────────────────────────────────

patches = []   # [(path, old_str, new_str), ...]
new_files = [] # [(path, content), ...]
summary = "Implémentation générée par feature-dev-agent."

patch_pattern = re.compile(
    r'<<<PATCH:([^>]+)>>>\s*<<<OLD>>>\n(.*?)<<<NEW>>>\n(.*?)<<<END>>>',
    re.DOTALL
)
file_pattern = re.compile(r'<<<FILE:([^>]+)>>>\n(.*?)<<<END>>>', re.DOTALL)
summary_pattern = re.compile(r'<<<SUMMARY>>>\n(.*?)<<<END>>>', re.DOTALL)

for m in patch_pattern.finditer(raw):
    path = m.group(1).strip()
    old_str = m.group(2)
    new_str = m.group(3)
    # Supprimer le newline final avant le délimiteur
    if old_str.endswith("\n"):
        old_str = old_str[:-1]
    if new_str.endswith("\n"):
        new_str = new_str[:-1]
    patches.append((path, old_str, new_str))

for m in file_pattern.finditer(raw):
    path = m.group(1).strip()
    content = m.group(2)
    if content.endswith("\n"):
        content = content[:-1]
    new_files.append((path, content))

m_summary = summary_pattern.search(raw)
if m_summary:
    summary = m_summary.group(1).strip()

if not patches and not new_files:
    print("[feature-dev-agent] ⚠ Aucun patch ni fichier généré.", file=sys.stderr)
    print(raw[:800], file=sys.stderr)
    sys.exit(1)

# ─── Application des patches ─────────────────────────────────────────────────

generated_paths = []

for path, old_str, new_str in patches:
    rel_path = path.lstrip("/")
    file_path = ROOT / rel_path
    if not file_path.exists():
        print(f"  [!] PATCH ignoré — fichier introuvable : {rel_path}", file=sys.stderr)
        continue
    content = file_path.read_text(encoding="utf-8")
    if old_str not in content:
        print(f"  [!] PATCH échoué — bloc OLD introuvable dans {rel_path}", file=sys.stderr)
        print(f"      OLD attendu : {repr(old_str[:120])}", file=sys.stderr)
        continue
    occurrences = content.count(old_str)
    if occurrences > 1:
        print(f"  [!] PATCH ambigu — {occurrences} occurrences du bloc OLD dans {rel_path}, patch ignoré", file=sys.stderr)
        continue
    content = content.replace(old_str, new_str, 1)
    file_path.write_text(content, encoding="utf-8")
    if rel_path not in generated_paths:
        generated_paths.append(rel_path)
    print(f"  [~] {rel_path} (patch appliqué)")

# ─── Écriture des nouveaux fichiers ──────────────────────────────────────────

for path, content in new_files:
    rel_path = path.lstrip("/")
    file_path = ROOT / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    if rel_path not in generated_paths:
        generated_paths.append(rel_path)
    print(f"  [+] {rel_path} (nouveau fichier)")

print(f"\n[feature-dev-agent] {len(patches)} patch(es) + {len(new_files)} nouveau(x) fichier(s).")

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
