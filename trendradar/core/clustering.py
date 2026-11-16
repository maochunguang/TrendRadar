# coding=utf-8
"""
跨平台同事件聚类

同一事件在多个平台（微博/知乎/头条等）上榜时，标题高度相似；
聚类后合并为一条推送条目（保留排名最高/出现次数最多的一条作为主体，
其余平台记入 also_in 列表展示为"同见：知乎、头条"），减少重复噪音。

算法：标题归一化（去标点/空白/大小写）→ 字符二元组（bigram）Jaccard 相似度
→ 并查集合并。O(n²) 对过滤后的条目规模（通常几十条）足够快。
"""

import re
from typing import Dict, List, Set

# 归一化：去除非文字字符（含空白、标点），仅保留中日文/字母/数字
_STRIP_RE = re.compile(r"[^\w]+")


def normalize_title(title: str) -> str:
    """标题归一化：小写化并去除空白/标点"""
    return _STRIP_RE.sub("", (title or "").lower())


def _bigrams(text: str) -> Set[str]:
    """字符二元组集合（单字符文本退化为单字符集合）"""
    if len(text) < 2:
        return set(text)
    return {text[i:i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """标题相似度（bigram Jaccard），0.0~1.0"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    union = ga | gb
    if not union:
        return 0.0
    return len(ga & gb) / len(union)


def cluster_titles(
    items: List[Dict],
    threshold: float = 0.6,
    title_key: str = "title",
) -> List[Dict]:
    """
    对条目列表做同事件聚类，返回合并后的新列表（不修改原条目）

    合并规则：
    - 主体条目 = count 最大（其次排名最靠前）的一条，保留其全部字段
    - 其余成员的平台名追加到主体的 also_in 列表（去重，不含主体平台）
    - 聚类前后条目总数减少，调用方应按需更新分组 count

    Args:
        items: 条目列表（title_data 字典）
        threshold: 相似度阈值（0~1）
        title_key: 标题字段名

    Returns:
        合并后的条目列表
    """
    n = len(items)
    if n <= 1 or threshold <= 0:
        return list(items)

    normalized = [normalize_title(item.get(title_key, "")) for item in items]

    # 并查集
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        if not normalized[i]:
            continue
        for j in range(i + 1, n):
            if not normalized[j]:
                continue
            if find(i) == find(j):
                continue
            if similarity(normalized[i], normalized[j]) >= threshold:
                union(i, j)

    clusters: Dict[int, List[int]] = {}
    for idx in range(n):
        clusters.setdefault(find(idx), []).append(idx)

    result: List[Dict] = []
    for members in clusters.values():
        if len(members) == 1:
            result.append(items[members[0]])
            continue

        # 主体：count 最大，其次 ranks 最小（排名越靠前越热）
        def primary_key(idx: int) -> tuple:
            item = items[idx]
            count = item.get("count", 1) if isinstance(item.get("count"), (int, float)) else 1
            ranks = item.get("ranks") or []
            min_rank = min((r for r in ranks if isinstance(r, (int, float))), default=9999)
            return (-count, min_rank)

        members_sorted = sorted(members, key=primary_key)
        head_idx = members_sorted[0]
        head = items[head_idx]
        merged = dict(head)

        also_in: List[str] = []
        for idx in members_sorted[1:]:
            item = items[idx]
            source = item.get("source_name", "")
            if source and source != head.get("source_name", "") and source not in also_in:
                also_in.append(source)
        if also_in:
            merged["also_in"] = also_in
        result.append(merged)

    return result


def apply_clustering_to_stats(stats: List[Dict], threshold: float = 0.6) -> int:
    """
    对热榜统计结构就地聚类（每个词组独立聚类），并更新分组 count

    Args:
        stats: [{"word": ..., "titles": [...], "count": N}, ...]
        threshold: 相似度阈值

    Returns:
        合并掉的条目总数
    """
    if not stats:
        return 0

    total_merged = 0
    for group in stats:
        titles = group.get("titles", [])
        if len(titles) <= 1:
            continue
        merged = cluster_titles(titles, threshold)
        removed = len(titles) - len(merged)
        if removed > 0:
            group["titles"] = merged
            group["count"] = len(merged)
            total_merged += removed

    return total_merged
