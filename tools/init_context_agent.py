#!/usr/bin/env python3
"""
init_context_agent.py — Génère .ai/codebase_context.md

Phase d'initialisation unique (ou à rejouer après des changements majeurs).
Lit les fichiers structurels clés du projet et demande au LLM de produire
un document de contexte global : modules, types, routes, schéma DB, hooks, API...

Ce fichier est ensuite commité et injecté dans chaque run de feature_dev_agent.py,
évitant de relire individuellement des dizaines de fichiers à chaque feature.

Usage : python3 dev-agents-core/tools/init_context_agent.py
Env   : AI_MODEL, OPENAI_API_KEY / ANTHROPIC_API_KEY / DEEPSEEK_API_KEY
"""
import json
import os
import pathlib
import re
import sys
import urllib.request

AI_MODEL = os.environ.get("AI_MODEL", "gpt-4.1")
ROOT = pathlib.Path.cwd()
AI_DIR = ROOT / ".ai"
OUTPUT_PATH = AI_DIR / "codebase_context.md"

IGNORE_DIRS = {".git", ".ai", "node_modules", "__pycache__", "dist", "build",
               "dev-agents-core", ".venv", "venv", "backups", "coverage", ".turbo"}

# Extensions de fichiers sources à indexer
SOURCE_EXTS = {".ts", ".tsx", ".js", ".mjs", ".py", ".prisma", ".json", ".yaml", ".yml", ".md"}

# Fichiers de config/structure importants à lire en entier
STRUCTURAL_PATTERNS = [
    "prisma/schema.prisma",
    "package.json",
    "pnpm-workspace.yaml",
    "turbo.json",
    "tsconfig*.json",
    "src/index.ts",
    "src/app.ts",
    "src/router.tsx",
    "src/types/**/*.ts",
    "src/schemas/**/*.ts",
    "shared/src/**/*.ts",
]

# Dossiers dont on lit en entier tous les fichiers (contenu complet)
FULL_READ_DIRS = [
    "shared/src",
    "backend/prisma",
    "backend/src/types",
]

# Dossiers dont on lit les N premières lignes (imports + exports + signatures)
SKELETON_DIRS = [
    "backend/src/modules",
    "frontend/web/src/api",
    "frontend/web/src/hooks",
    "frontend/web/src/pages",
    "frontend/web/src/components",
]
SKELETON_LINES = 80   # premières lignes par fichier skeleton
MAX_FULL_CHARS = 400000   # budget total pour les fichiers complets
MAX_SKELETON_FILES = 150  # limite de fichiers skeleton pour ne pas exploser


def get_repo_tree(root: pathlib.Path) -> str:
    lines = []
    for p in sorted(root.rglob("*")):
        if any(part in IGNORE_DIRS or part.startswith(".") for part in p.relative_to(root).parts):
            continue
        if p.is_file() and p.suffix in SOURCE_EXTS:
            lines.append(str(p.relative_to(root)))
    return "\n".join(lines)


def call_api(prompt: str, model: str) -> str:
    if model.startswith("claude-"):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            print("[init-context] ANTHROPIC_API_KEY manquant.", file=sys.stderr)
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
            print("[init-context] DEEPSEEK_API_KEY manquant.", file=sys.stderr)
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
            print("[init-context] OPENAI_API_KEY manquant.", file=sys.stderr)
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


# ─── Collecte des fichiers ────────────────────────────────────────────────────

def should_ignore(path: pathlib.Path) -> bool:
    return any(part in IGNORE_DIRS or part.startswith(".") for part in path.parts)


def collect_full_files() -> str:
    """Lit en entier les fichiers des dossiers structurels (shared, prisma, types)."""
    sections = []
    budget = MAX_FULL_CHARS
    for dir_pattern in FULL_READ_DIRS:
        dir_path = ROOT / dir_pattern
        if not dir_path.exists():
            continue
        for fpath in sorted(dir_path.rglob("*")):
            if not fpath.is_file() or fpath.suffix not in SOURCE_EXTS:
                continue
            if should_ignore(fpath.relative_to(ROOT)):
                continue
            if budget <= 0:
                break
            content = fpath.read_text(encoding="utf-8", errors="replace")
            rel = str(fpath.relative_to(ROOT))
            if len(content) > budget:
                content = content[:budget] + f"\n... [tronqué]"
                budget = 0
            else:
                budget -= len(content)
            sections.append(f"### {rel}\n```\n{content}\n```")
            print(f"  [full] {rel} ({len(content)} chars)", file=sys.stderr)
    return "\n\n".join(sections)


def collect_skeleton_files() -> str:
    """Lit les N premières lignes des fichiers des dossiers fonctionnels."""
    sections = []
    count = 0
    for dir_pattern in SKELETON_DIRS:
        dir_path = ROOT / dir_pattern
        if not dir_path.exists():
            continue
        for fpath in sorted(dir_path.rglob("*")):
            if count >= MAX_SKELETON_FILES:
                break
            if not fpath.is_file() or fpath.suffix not in {".ts", ".tsx", ".js"}:
                continue
            if should_ignore(fpath.relative_to(ROOT)):
                continue
            lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            total = len(lines)
            skeleton = "\n".join(lines[:SKELETON_LINES])
            suffix = f"\n... [{total - SKELETON_LINES} lignes supplémentaires]" if total > SKELETON_LINES else ""
            rel = str(fpath.relative_to(ROOT))
            sections.append(f"### {rel} ({total} lignes)\n```\n{skeleton}{suffix}\n```")
            count += 1
    print(f"  [skeleton] {count} fichiers indexés", file=sys.stderr)
    return "\n\n".join(sections)


# ─── Génération du contexte ───────────────────────────────────────────────────

print("[init-context] Collecte des fichiers...", file=sys.stderr)
repo_tree = get_repo_tree(ROOT)
full_files = collect_full_files()
skeleton_files = collect_skeleton_files()

# Conventions projet
conventions_path = ROOT / ".github" / "copilot-instructions.md"
conventions = conventions_path.read_text(encoding="utf-8") if conventions_path.exists() else ""

print(f"[init-context] Construction du prompt ({len(full_files) + len(skeleton_files)} chars de code)...", file=sys.stderr)

prompt = f"""Tu es un architecte senior. Tu vas analyser le codebase d'un projet et produire un document de contexte global en Markdown.
Ce document sera injecté dans chaque session d'un agent de développement automatique pour lui donner une compréhension immédiate de l'architecture sans qu'il ait à relire tous les fichiers.

== CONVENTIONS PROJET ==
{conventions}

== STRUCTURE COMPLÈTE DU REPO (fichiers sources) ==
{repo_tree}

== FICHIERS STRUCTURELS (contenu complet) ==
{full_files}

== FICHIERS FONCTIONNELS (squelettes : imports + exports + signatures) ==
{skeleton_files}

== DOCUMENT À PRODUIRE ==
Génère un document Markdown structuré avec les sections suivantes :

## 1. Architecture globale
Décris l'organisation du monorepo (packages, responsabilités).

## 2. Schéma de base de données
Liste toutes les tables Prisma avec leurs champs principaux et relations.

## 3. Modules backend
Pour chaque module (finances, horses, stables, users, auth...) :
- Nom du module
- Routes exposées (méthode + path + rôle requis)
- Fonctions principales du service

## 4. API frontend
Pour chaque fichier `src/api/*.ts` : fonctions exportées + endpoint appelé.

## 5. Hooks React Query
Pour chaque hook (`useQuery` / `useMutation`) : nom, mutation ou query, invalidations.

## 6. Types et schémas partagés
Types TypeScript et schémas Zod clés exportés depuis `@cavalcade/shared`.

## 7. Conventions de nommage et patterns
Patterns récurrents à respecter (nommage fichiers, structure composants, gestion erreurs...).

## 8. Points d'attention
Conventions critiques, pièges connus, règles à ne jamais violer.

Sois précis, exhaustif et factuel. Pas de code inventé — uniquement ce qui existe dans les fichiers fournis.
"""

print(f"[init-context] Appel API ({AI_MODEL}, prompt ~{len(prompt)} chars)...", file=sys.stderr)
context_doc = call_api(prompt, AI_MODEL)

if not context_doc.strip():
    print("[init-context] Réponse vide de l'API.", file=sys.stderr)
    sys.exit(1)

# ─── Écriture du fichier de contexte ─────────────────────────────────────────

AI_DIR.mkdir(parents=True, exist_ok=True)
header = f"""<!-- AUTO-GENERATED by init_context_agent.py — NE PAS MODIFIER MANUELLEMENT -->
<!-- Régénérer avec : python3 dev-agents-core/tools/init_context_agent.py -->
<!-- Modèle : {AI_MODEL} -->

"""
OUTPUT_PATH.write_text(header + context_doc, encoding="utf-8")
print(f"[init-context] Contexte généré : {OUTPUT_PATH} ({len(context_doc)} chars)", file=sys.stderr)
print(str(OUTPUT_PATH.relative_to(ROOT)))
