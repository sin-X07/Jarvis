---
name: github-trending-summary
description: 每天早上总结 GitHub 热榜：挑 3-5 个相关的仓库，讲清功能和创新点
---

# GitHub Trending Summary

当用户说"今天热榜有什么"、"总结一下热榜"时，按这个流程走。

## 步骤

1. 用 `fetch_url` 抓 GitHub Trending 页（比如
   `https://github.com/trending?since=daily`），或用 `web_search`
   搜"GitHub trending 今天"。
2. 挑 3-5 个和计算机专业相关的仓库，优先挑和用户手头项目
   （比如 agent、游戏、工具）沾边的。
3. 每个仓库写三行：是干什么的、创新点在哪、和用户有什么关系。
   不要凑数：没有值得说的就只写 1-2 个，或者直接说今天没什么。
4. 记住：7 天内总结过的仓库不要重复（先调 `recall_facts`
   查一下，或者问用户）。

## 注意

- 只总结公开信息，不要编 star 数、不要编功能。
- 输出用中文。
