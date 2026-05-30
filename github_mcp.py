import os
import json
import logging
import base64
import re
import asyncio
import time
from typing import Optional

import httpx
from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("github-analyzer")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if not GITHUB_TOKEN:
    raise RuntimeError("环境变量 GITHUB_TOKEN 未设置，请 export GITHUB_TOKEN=your_token")

BASE_URL = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

mcp = FastMCP("GithubAnalyzer")

# ── 模块级常量：依赖文件列表 ──
DEP_FILES = [
    "package.json", "package-lock.json",
    "requirements.txt", "requirements-dev.txt",
    "go.mod", "go.sum",
    "Gemfile", "Gemfile.lock",
    "Cargo.toml", "Cargo.lock",
    "pyproject.toml",
    "pom.xml", "build.gradle", "build.gradle.kts",
    "composer.json",
    "CMakeLists.txt",
]

# ── 模块级常量：目录角色映射（保留供内部使用） ──
DIR_ROLES = {
    "core": "核心逻辑", "services": "服务层", "src": "源码目录",
    "lib": "库目录", "packages": "包目录", "app": "应用主目录",
    "internal": "内部实现", "pkg": "公共包", "cmd": "命令行入口",
    "handlers": "处理器", "routes": "路由定义", "models": "数据模型",
    "middleware": "中间件", "controllers": "控制器", "views": "视图",
    "modules": "模块", "components": "组件",
    "tests": "测试", "__tests__": "测试", "test": "测试",
    "docs": "文档", "doc": "文档",
    "examples": "示例", "samples": "示例",
    "scripts": "脚本", "ci": "CI配置", ".github": "GitHub Actions",
    "config": "配置文件", "configs": "配置文件", "utils": "工具函数",
}


async def _github_get(endpoint: str, retries: int = 3) -> dict:
    """统一的 GitHub API GET 请求，支持 429 重试，返回 JSON 或抛出异常"""
    url = f"{BASE_URL}{endpoint}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(retries):
            start = time.monotonic()
            logger.info(f"→ GET {endpoint}")
            r = await client.get(url, headers=HEADERS)
            elapsed = time.monotonic() - start
            logger.info(f"← GET {endpoint} ({r.status_code}, {elapsed:.2f}s)")

            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                logger.warning(f"触发二级限流，等待 {wait}s 后重试 ({attempt+1}/{retries})")
                await asyncio.sleep(wait)
                continue

            if r.status_code == 404:
                raise ValueError(f"仓库不存在或路径无效: {endpoint}")
            if r.status_code == 403 and "rate limit" in r.text.lower():
                raise RuntimeError("GitHub API 频率限制，请稍后重试或检查 Token")
            if r.status_code == 401:
                raise RuntimeError("GitHub Token 无效或未授权")
            r.raise_for_status()
            return r.json()

    raise RuntimeError(f"API 请求失败，已达最大重试次数: {endpoint}")


def _error_response(msg: str) -> str:
    """统一的错误返回格式，LLM 可通过 success 字段程序化判断"""
    return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


def _success_response(data: dict) -> str:
    """统一的成功返回格式"""
    return json.dumps({"success": True, **data}, ensure_ascii=False, indent=2)


def _safe_loads(s: str) -> dict:
    """安全解析 JSON 字符串，解析失败返回错误字典（不抛异常）"""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {"error": "子工具返回异常", "raw": s[:500]}



@mcp.tool(description="获取 GitHub 仓库基本信息（名称、描述、Star、语言、协议等）")
async def get_repo_overview(owner: str, repo: str) -> str:
    try:
        data = await _github_get(f"/repos/{owner}/{repo}")
        overview = {
            "full_name": data.get("full_name"),
            "description": data.get("description"),
            "homepage": data.get("homepage"),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "language": data.get("language"),
            "license": data.get("license", {}).get("spdx_id") if data.get("license") else None,
            "created_at": data.get("created_at"),
            "updated_at": data.get("pushed_at"),
            "default_branch": data.get("default_branch"),
        }
        return _success_response(overview)
    except Exception as e:
        return _error_response(f"获取仓库概览失败: {str(e)}")


@mcp.tool(description="分析仓库的技术栈（语言比例、关键依赖文件列表）")
async def get_tech_stack(owner: str, repo: str) -> str:
    try:
        # 语言统计（百分比）
        lang_data = await _github_get(f"/repos/{owner}/{repo}/languages")
        total = sum(lang_data.values())
        languages = {k: round(v / total * 100, 1) for k, v in lang_data.items()} if total > 0 else {}

        # 依赖文件列表（只返回文件名）
        dependencies = []
        for fname in DEP_FILES:
            try:
                await _github_get(f"/repos/{owner}/{repo}/contents/{fname}")
                dependencies.append(fname)
            except Exception:
                pass

        result = {
            "languages": languages,
            "dependencies": dependencies,
        }
        return _success_response(result)
    except Exception as e:
        return _error_response(f"获取技术栈失败: {str(e)}")


@mcp.tool(description="获取并分析 GitHub 项目的 README 内容")
async def get_readme(owner: str, repo: str) -> str:
    try:
        data = await _github_get(f"/repos/{owner}/{repo}/readme")
        content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        # 只返回纯文本内容
        return _success_response({"content": content[:10000]})
    except Exception as e:
        return _error_response(f"获取 README 失败: {str(e)}")


@mcp.tool(description="获取仓库的目录结构，可指定最大深度（默认 1）")
async def get_directory_structure(owner: str, repo: str, max_depth: int = 1) -> str:
    try:
        repo_data = await _github_get(f"/repos/{owner}/{repo}")
        default_branch = repo_data["default_branch"]
        branch_data = await _github_get(f"/repos/{owner}/{repo}/branches/{default_branch}")
        sha = branch_data["commit"]["commit"]["tree"]["sha"]
        tree_data = await _github_get(f"/repos/{owner}/{repo}/git/trees/{sha}?recursive=1")
        entries = tree_data["tree"]

        # 构建目录树字典
        tree = {}
        for entry in entries:
            path = entry["path"]
            parts = path.split("/")
            if len(parts) > max_depth:
                continue
            current = tree
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            name = parts[-1]
            if entry["type"] == "tree":
                if name not in current:
                    current[name] = {}
            else:
                if name not in current:
                    current[name] = None

        def render(d, prefix=""):
            lines = []
            items = sorted(d.items())
            for i, (name, children) in enumerate(items):
                is_last = i == len(items) - 1
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{name}")
                if isinstance(children, dict):
                    extension = "    " if is_last else "│   "
                    lines.extend(render(children, prefix + extension))
            return lines

        def render_flat(d: dict, prefix="") -> list[str]:
            paths = []
            for name, children in d.items():
                full = f"{prefix}/{name}" if prefix else name
                paths.append(full)
                if isinstance(children, dict):
                    paths.extend(render_flat(children, full))
            return paths

        result_lines = [f"{owner}/{repo} (max_depth={max_depth})"] + render(tree)
        return _success_response({
            "tree_text": "\n".join(result_lines),
            "flat_paths": render_flat(tree),
        })
    except Exception as e:
        return _error_response(f"获取目录结构失败: {str(e)}")


@mcp.tool(description="识别仓库的核心模块/包（根据常见项目结构推断）")
async def get_key_modules(owner: str, repo: str) -> str:
    try:
        repo_data = await _github_get(f"/repos/{owner}/{repo}")
        branch = repo_data["default_branch"]
        branch_info = await _github_get(f"/repos/{owner}/{repo}/branches/{branch}")
        tree_sha = branch_info["commit"]["commit"]["tree"]["sha"]
        tree_data = await _github_get(f"/repos/{owner}/{repo}/git/trees/{tree_sha}")
        top_level = [item for item in tree_data["tree"] if item["type"] == "tree"]
        # 返回模块路径列表
        modules = [entry["path"] for entry in top_level if entry["path"] in DIR_ROLES]
        return _success_response({"key_modules": modules})
    except Exception as e:
        return _error_response(f"获取核心模块失败: {str(e)}")


@mcp.tool(description="推断项目的架构模式（单体、微服务、插件化等）")
async def get_architecture_analysis(owner: str, repo: str) -> str:
    try:
        repo_data = await _github_get(f"/repos/{owner}/{repo}")
        branch = repo_data["default_branch"]
        branch_info = await _github_get(f"/repos/{owner}/{repo}/branches/{branch}")
        tree_sha = branch_info["commit"]["commit"]["tree"]["sha"]
        tree_data = await _github_get(f"/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1")
        entries = tree_data["tree"]

        top_dirs = {
            e["path"]
            for e in entries
            if e["type"] == "tree"
            and "/" not in e["path"]
        }

        has_docker_compose = any("docker-compose" in e["path"] for e in entries)
        has_dockerfiles = any(e["path"].endswith("Dockerfile") for e in entries)
        has_microservices = any("microservice" in e["path"].lower() for e in entries) or \
                            any("services/" in e["path"] for e in entries if e["type"] == "tree")
        has_plugins = any("plugin" in e["path"].lower() for e in entries if e["type"] == "tree") or \
                      any("plugins/" in e["path"] for e in entries)
        language = repo_data.get("language", "").lower()

        if len(top_dirs) >= 5 and any(x in top_dirs for x in ("libs", "packages", "modules")):
            arch = "Monorepo 模块化架构"
        elif has_microservices:
            arch = "微服务架构"
        elif has_plugins:
            arch = "插件化架构"
        elif has_dockerfiles or has_docker_compose:
            arch = "容器化应用"
        elif language in ("python", "javascript", "typescript", "ruby"):
            arch = "典型 MVC/模块化单体"
        else:
            arch = "通用单体架构"

        # 只返回字符串
        return _success_response({"architecture": arch})
    except Exception as e:
        return _error_response(f"架构分析失败: {str(e)}")


@mcp.tool(description="识别仓库的入口文件（自动检测 main.py、index.js 等，并识别主入口）")
async def get_entry_points(owner: str, repo: str) -> str:
    """返回主入口和辅助入口，格式：{"main": "path", "others": ["path1", "path2"]}"""
    # 入口文件名模式
    ENTRY_NAMES = {
        "main.py", "cli.py", "__main__.py", "app.py", "run.py", "server.py", "manage.py",
        "index.js", "index.ts", "main.js", "main.ts", "server.js", "server.ts", "app.js", "app.ts",
        "index.jsx", "index.tsx", "main.jsx", "main.tsx",
        "main.go",
        "Application.java", "Main.java", "App.java",
        "main.rs",
    }
    try:
        entries = await _get_tree_entries(owner, repo)
        entry_files = [e["path"] for e in entries if e["type"] == "blob" and e["path"].rsplit("/", 1)[-1] in ENTRY_NAMES]
        if not entry_files:
            return _success_response({"main": None, "others": []})

        # 常见主入口优先级
        priority_names = ["main.py", "app.py", "server.py", "index.js", "main.go", "Application.java"]
        main = None
        for p in entry_files:
            basename = p.rsplit("/", 1)[-1]
            if basename in priority_names:
                main = p
                break
        if main is None:
            main = entry_files[0]
        others = [p for p in entry_files if p != main]
        return _success_response({"main": main, "others": others})
    except Exception as e:
        return _error_response(f"获取入口文件失败: {str(e)}")


@mcp.tool(description="根据技术栈和项目结构生成学习路线建议")
async def get_learning_roadmap(owner: str, repo: str) -> str:
    try:
        overview_resp = await get_repo_overview(owner, repo)
        tech_resp = await get_tech_stack(owner, repo)
        overview = _safe_loads(overview_resp)
        tech = _safe_loads(tech_resp)

        lang = overview.get("language", "")
        deps = tech.get("dependencies", [])

        steps = ["1. 阅读项目 README 和文档，了解目标功能"]

        if "python" in lang.lower():
            steps.append("2. 学习 Python 基础及项目使用的框架（如 Flask/FastAPI 等）")
        elif "javascript" in lang.lower() or "typescript" in lang.lower():
            steps.append("2. 掌握 JavaScript/TypeScript 及项目使用的前端/后端框架")
        elif "go" in lang.lower():
            steps.append("2. 学习 Go 语言基础及项目使用的库")
        else:
            steps.append(f"2. 熟悉 {lang} 语言及项目主要技术栈")

        if any("requirements.txt" in f for f in deps):
            steps.append("3. 安装 Python 依赖：pip install -r requirements.txt")
        if any("package.json" in f for f in deps):
            steps.append("3. 安装 Node 依赖：npm install 或 yarn")
        if any("go.mod" in f for f in deps):
            steps.append("3. 拉取 Go 依赖：go mod download")

        steps.append("4. 阅读核心模块代码，理解整体结构（可使用 get_key_modules 和 get_directory_structure）")
        steps.append("5. 尝试运行测试：通常为 pytest、npm test 或 go test")
        steps.append("6. 尝试贡献一个小修复或文档改进")
        steps.append("7. 深入学习架构设计和核心算法")

        roadmap_text = "\n".join(steps)
        return _success_response({"roadmap": roadmap_text})
    except Exception as e:
        return _error_response(f"生成学习路线失败: {str(e)}")


@mcp.tool(description="综合分析仓库的项目结构（项目类型推断、入口文件、核心模块识别）")
async def analyze_project_structure(owner: str, repo: str) -> str:
    """返回 project_type, entry_files, core_modules"""
    try:
        entries = await _get_tree_entries(owner, repo)
        all_blob_paths = {e["path"] for e in entries if e["type"] == "blob"}
        all_tree_paths = {e["path"] for e in entries if e["type"] == "tree"}
        top_level_dirs = sorted(p for p in all_tree_paths if "/" not in p)
        all_basenames = {p.rsplit("/", 1)[-1] for p in all_blob_paths}

        # 获取依赖文件内容
        dep_contents = {}
        for fname in DEP_FILES:
            if fname in all_blob_paths:
                try:
                    data = await _github_get(f"/repos/{owner}/{repo}/contents/{fname}")
                    raw = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
                    dep_contents[fname] = raw
                except Exception:
                    pass

        def _has_dep(fname: str, keyword: str) -> bool:
            return keyword.lower() in dep_contents.get(fname, "").lower()

        def _has_dir(prefix: str) -> bool:
            return any(p == prefix or p.startswith(prefix + "/") for p in all_tree_paths)

        packages_subdirs = {p for p in all_tree_paths if p.startswith("packages/") and p.count("/") == 1}
        project_type = None

        if "lerna.json" in all_blob_paths or len(packages_subdirs) >= 2 or _has_dep("package.json", '"workspaces"'):
            project_type = "Monorepo 模块化项目"
        elif _has_dep("pom.xml", "spring-boot") or _has_dep("build.gradle", "spring-boot") or "Application.java" in all_basenames:
            project_type = "Spring Boot 应用"
        elif "next.config.js" in all_blob_paths or "next.config.ts" in all_blob_paths or ((_has_dir("pages") or _has_dir("app")) and _has_dep("package.json", "next")):
            project_type = "Next.js 应用"
        elif "src/App.jsx" in all_blob_paths or "src/App.tsx" in all_blob_paths or _has_dep("package.json", "react"):
            project_type = "React 应用"
        elif "src/App.vue" in all_blob_paths or _has_dep("package.json", "vue"):
            project_type = "Vue 应用"
        elif _has_dep("package.json", "express"):
            project_type = "Express 应用"
        elif "manage.py" in all_basenames and any(n in all_basenames for n in ("settings.py", "urls.py", "wsgi.py", "asgi.py")):
            project_type = "Django Web 应用"
        elif any(n in all_basenames for n in ("main.py", "server.py")) and (_has_dep("requirements.txt", "fastapi") or _has_dep("pyproject.toml", "fastapi")):
            project_type = "FastAPI Web 应用"
        elif "app.py" in all_basenames and (_has_dep("requirements.txt", "flask") or _has_dep("pyproject.toml", "flask")):
            project_type = "Flask Web 应用"
        elif "go.mod" in all_basenames and "main.go" in all_basenames:
            project_type = "Go 服务"
        elif "Cargo.toml" in all_basenames and "main.rs" in all_basenames:
            project_type = "Rust 服务"
        elif "setup.py" in all_basenames or "pyproject.toml" in all_basenames:
            project_type = "Python CLI / 库项目"
        elif "index.html" in all_basenames:
            project_type = "静态站点"
        else:
            project_type = "通用项目"

        # 入口文件识别
        ENTRY_NAMES = {
            "main.py", "cli.py", "__main__.py", "app.py", "run.py", "server.py", "manage.py",
            "index.js", "index.ts", "main.js", "main.ts", "server.js", "server.ts", "app.js", "app.ts",
            "index.jsx", "index.tsx", "main.jsx", "main.tsx",
            "main.go", "Application.java", "Main.java", "App.java", "main.rs",
        }
        entry_files = sorted([e["path"] for e in entries if e["type"] == "blob" and e["path"].rsplit("/", 1)[-1] in ENTRY_NAMES])

        # 核心模块识别
        KNOWN_SRC_DIRS = {
            "src", "lib", "packages", "app", "internal", "pkg", "core", "services", "cmd",
            "modules", "components", "handlers", "routes", "models", "utils",
        }
        core_modules = [d for d in top_level_dirs if d in KNOWN_SRC_DIRS]
        SOURCE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".vue"}
        for d in top_level_dirs:
            if d in core_modules:
                continue
            has_src = any(p.startswith(d + "/") and os.path.splitext(p)[1].lower() in SOURCE_EXTS for p in all_blob_paths)
            if has_src:
                core_modules.append(d)
        core_modules = sorted(set(core_modules))

        result = {
            "project_type": project_type,
            "entry_files": entry_files,
            "core_modules": core_modules,
        }
        return _success_response(result)
    except Exception as e:
        return _error_response(f"项目结构分析失败: {str(e)}")


# 辅助函数
async def _get_tree_entries(owner: str, repo: str) -> list[dict]:
    repo_data = await _github_get(f"/repos/{owner}/{repo}")
    default_branch = repo_data["default_branch"]
    branch_data = await _github_get(f"/repos/{owner}/{repo}/branches/{default_branch}")
    tree_sha = branch_data["commit"]["commit"]["tree"]["sha"]
    tree_data = await _github_get(f"/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1")
    return tree_data["tree"]


@mcp.tool(description="搜索仓库中的代码关键字")
async def search_code(owner: str, repo: str, keyword: str, limit: int = 20) -> str:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{BASE_URL}/search/code",
                params={"q": f"{keyword} repo:{owner}/{repo}"},
                headers={**HEADERS, "Accept": "application/vnd.github.text-match+json"}
            )
            r.raise_for_status()
            data = r.json()
        matches = []
        for item in data.get("items", [])[:limit]:
            text_matches = item.get("text_matches", [])
            fragments = [tm.get("fragment", "") for tm in text_matches[:3]]
            matches.append({"file": item["path"], "fragments": fragments})
        return _success_response({"keyword": keyword, "matches": matches})
    except Exception as e:
        return _error_response(f"代码搜索失败: {e}")


@mcp.tool(description="获取指定文件源码内容")
async def get_file_content(owner: str, repo: str, path: str, start_line: int = 1, end_line: int = 200) -> str:
    try:
        data = await _github_get(f"/repos/{owner}/{repo}/contents/{path}")
        raw = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        lines = raw.split("\n")
        total_lines = len(lines)
        selected = lines[start_line - 1:end_line]
        return _success_response({
            "path": path,
            "total_lines": total_lines,
            "line_range": f"{start_line}-{end_line}",
            "content": "\n".join(selected),
        })
    except Exception as e:
        return _error_response(f"获取文件失败: {e}")


@mcp.tool(description="获取单个源文件的摘要（类列表、函数列表、导入模块列表）")
async def get_file_summary(owner: str, repo: str, path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    lang_map = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
                ".go": "go", ".rs": "rust", ".java": "java"}
    lang = lang_map.get(ext)
    if not lang:
        return _error_response(f"不支持的文件类型 ({ext})")
    try:
        data = await _github_get(f"/repos/{owner}/{repo}/contents/{path}")
        content = base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
        # 简单提取（使用预定义正则，细节略）
        class_pat = re.compile(r"^\s*class\s+(\w+)", re.MULTILINE)
        func_pat = re.compile(r"^\s*def\s+(\w+)", re.MULTILINE)
        import_pat = re.compile(r"^\s*(?:import\s+(\S+)|from\s+(\S+)\s+import)", re.MULTILINE)
        classes = list(dict.fromkeys(class_pat.findall(content)))[:50]
        functions = list(dict.fromkeys(func_pat.findall(content)))[:50]
        imports = []
        for m in import_pat.findall(content):
            imp = m[0] or m[1]
            if imp:
                imports.append(imp)
        imports = list(dict.fromkeys(imports))[:50]
        return _success_response({"classes": classes, "functions": functions, "imports": imports})
    except Exception as e:
        return _error_response(f"获取文件摘要失败: {e}")


@mcp.tool(description="对 GitHub 项目进行完整分析（聚合多个工具）")
async def analyze_repo(owner: str, repo: str) -> str:
    try:
        result = {
            "overview": _safe_loads(await get_repo_overview(owner, repo)),
            "readme": _safe_loads(await get_readme(owner, repo)),
            "tech_stack": _safe_loads(await get_tech_stack(owner, repo)),
            "directory_structure": _safe_loads(await get_directory_structure(owner, repo, max_depth=2)),
            "key_modules": _safe_loads(await get_key_modules(owner, repo)),
            "architecture": _safe_loads(await get_architecture_analysis(owner, repo)),
            "learning_roadmap": _safe_loads(await get_learning_roadmap(owner, repo)),
            "project_structure": _safe_loads(await analyze_project_structure(owner, repo)),
        }
        return _success_response(result)
    except Exception as e:
        return _error_response(f"完整分析失败: {e}")


if __name__ == "__main__":
    logger.info(f"启动 GithubAnalyzer MCP 服务，Token 已配置")
    mcp.run(transport="sse", port=8000)