"""按扩展名识别语言，并定出仓库的主语言（SPEC 7.4.4）。

主语言用来决定提示词里的身份（"Go 面试官"而不是一律"Java 面试官"），
也写进画像，页面据此说明用的是哪种分析方式。
"""

from __future__ import annotations

from collections.abc import Mapping

LANG_BY_EXT = {
    ".java": "java",
    ".kt": "kotlin",
    ".scala": "scala",
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".dart": "dart",
    ".xml": "xml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".properties": "properties",
    ".sql": "sql",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".gradle": "gradle",
    ".json": "json",
    ".sh": "shell",
}

# 主语言只在编程语言里选：配置、文档、标记语言的文件数再多也不算
CODE_LANGS = {
    "java",
    "kotlin",
    "scala",
    "python",
    "javascript",
    "typescript",
    "vue",
    "go",
    "rust",
    "c",
    "cpp",
    "csharp",
    "ruby",
    "php",
    "swift",
    "dart",
}

LANG_NAMES = {
    "java": "Java",
    "kotlin": "Kotlin",
    "scala": "Scala",
    "python": "Python",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "vue": "Vue",
    "go": "Go",
    "rust": "Rust",
    "c": "C",
    "cpp": "C++",
    "csharp": "C#",
    "ruby": "Ruby",
    "php": "PHP",
    "swift": "Swift",
    "dart": "Dart",
}


def primary_language(lang_stats: Mapping[str, int]) -> str | None:
    """文件数最多的编程语言；一个编程语言文件都没有时返回 None。"""
    code = [(n, lang) for lang, n in lang_stats.items() if lang in CODE_LANGS and n > 0]
    if not code:
        return None
    # 文件数相同时按名字排，保证同一个仓库每次结果一样
    return sorted(code, key=lambda x: (-x[0], x[1]))[0][1]


def display_name(lang: str | None) -> str:
    """提示词里用的语言名；不知道主语言时返回空串，提示词就只说"技术面试官"。"""
    if not lang:
        return ""
    return LANG_NAMES.get(lang, lang)


def interviewer_title(lang: str | None) -> str:
    """出题提示词里的身份："资深 Go 面试官"；不知道主语言时只说"资深技术面试官"。"""
    name = display_name(lang)
    return f"资深 {name} 面试官" if name else "资深技术面试官"
